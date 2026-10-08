"""Drive health and TrueNAS's own alerts, read over the TrueNAS API.

The fixtures are the shapes a real TrueNAS SCALE 25.10 box returned to a
Read-only Administrator key (names changed): pool.query with its topology,
disk.query, disk.temperatures and alert.list.
"""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from spark import credentials, truenas, truenas_health
from spark import db as D
from spark.main import create_app
from spark.models import AlertState, Device

from test_credentials import (DENIED, PASSWORD, FakeTrueNAS, _config, _row, _sent, _trusted,
                              flat, run)


def _disk(name: str, guid: str, *, status="ONLINE", r=0, w=0, c=0) -> dict:
    return {"type": "DISK", "name": guid, "path": f"/dev/disk/by-partuuid/{guid}", "guid": "1",
            "status": status, "disk": name, "device": f"{name}1", "unavail_disk": None,
            "stats": {"read_errors": r, "write_errors": w, "checksum_errors": c,
                      "timestamp": 1, "allocated": 0, "size": 0, "fragmentation": 0,
                      "self_healed": 0, "configured_ashift": 12, "logical_ashift": 9,
                      "physical_ashift": 12, "ops": [0] * 7, "bytes": [0] * 7},
            "children": []}


def _pools(**drive) -> list[dict]:
    """One RAIDZ2 pool of four; `drive` overrides sda's status and counts."""
    return [{
        "id": 1, "name": "tank", "guid": "1", "path": "/mnt/tank", "status": "ONLINE",
        "healthy": True, "warning": False, "status_code": "OK", "status_detail": None,
        "size": 47_000_000_000_000, "allocated": 17_000_000_000_000, "free": 30_000_000_000_000,
        "scan": {"function": "SCRUB", "state": "FINISHED", "start_time": {"$date": 1788073201000},
                 "end_time": {"$date": 1788092612000}, "percentage": 99.99, "errors": 0},
        "topology": {"data": [{
            "type": "RAIDZ2", "name": "raidz2-0", "path": None, "guid": "2", "status": "ONLINE",
            "stats": {"read_errors": 0, "write_errors": 0, "checksum_errors": 0},
            "unavail_disk": None,
            "children": [_disk("sdd", "11111111-0000-0000-0000-000000000004"),
                         _disk("sda", "11111111-0000-0000-0000-000000000001", **drive),
                         _disk("sdb", "11111111-0000-0000-0000-000000000002"),
                         _disk("sdc", "11111111-0000-0000-0000-000000000003")]}],
            "log": [], "cache": [], "spare": [], "special": [], "dedup": []},
    }]


DISKS = [
    {"name": n, "devname": n, "size": 12000138625024, "model": "MB012000JWDFD", "rotationrate": 7200,
     "type": "HDD", "bus": "SCSI", "pool": None, "serial": "SECRET-SERIAL", "identifier": "x",
     "lunid": "y", "zfs_guid": "z"} for n in ("sda", "sdc", "sdd", "sdb")
] + [{"name": "sde", "devname": "sde", "size": 30784094208, "model": "SanDisk_3.2Gen1",
      "rotationrate": None, "type": "HDD", "bus": "USB", "pool": None, "serial": "S2"}]

TEMPS = {"sdb": 42.0, "sda": 43.0, "sde": None, "sdd": 41.0, "sdc": 40.0}


def _alert(uuid: str, level: str, klass: str, text: str, *, dismissed=False) -> dict:
    return {"uuid": uuid, "id": uuid, "level": level, "klass": klass, "formatted": text,
            "text": "%s", "args": [], "dismissed": dismissed, "node": "A", "source": "",
            "key": "k", "datetime": {"$date": 1788092612000}, "last_occurrence": None,
            "mail": None, "one_shot": False}


USB = _alert("a-usb", "WARNING", "PoolUSBDisks",
             "'boot-pool' is consuming USB devices 'sde' which is not recommended.")
ALERTS = [
    USB,
    _alert("a-rep", "INFO", "ReplicationSuccess", 'Replication "tank/a - backup/a" succeeded.'),
    _alert("a-upd", "INFO", "HasUpdate", "A system update is available."),
    _alert("a-old", "CRITICAL", "SMART", "Device: /dev/sdb, old news.", dismissed=True),
]
SMART = _alert("a-smart", "CRITICAL", "SMART",
               "Device: /dev/sda [SAT], 8 Currently unreadable (pending) sectors.")


def real() -> dict:
    return {"pool.query": _pools(), "disk.query": copy.deepcopy(DISKS),
            "disk.temperatures": dict(TEMPS), "alert.list": copy.deepcopy(ALERTS)}


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


class TestParse:
    def test_the_real_shape(self):
        out = truenas.parse(pools=_pools(), disks=DISKS, temperatures=TEMPS, alerts=ALERTS)
        (pool,) = out["pools"]
        assert pool == {"name": "tank", "status": "ONLINE", "healthy": True,
                        "scrub": {"function": "SCRUB", "state": "FINISHED",
                                  "end": "2026-08-30T12:23:32+00:00", "errors": 0}}
        drives = {d["name"]: d for d in out["drives"]}
        assert sorted(drives) == ["sda", "sdb", "sdc", "sdd", "sde"]
        assert drives["sda"] == {
            "name": "sda", "model": "MB012000JWDFD", "type": "HDD", "bus": "SCSI",
            "size": 12000138625024, "pool": "tank", "group": "data", "vdev": "raidz2-0",
            "status": "ONLINE", "read_errors": 0, "write_errors": 0, "checksum_errors": 0,
            "celsius": 43.0}
        # disk.query's own "pool" is None on 25.10: the pool comes from the topology.
        assert drives["sde"]["pool"] is None and drives["sde"]["celsius"] is None
        assert "SECRET-SERIAL" not in repr(out), "serials are not kept"
        assert [(a["uuid"], a["level"]) for a in out["alerts"]] == [
            ("a-usb", "WARNING"), ("a-rep", "INFO"), ("a-upd", "INFO")]
        assert out["alerts"][0]["at"] == "2026-08-30T12:23:32+00:00"

    def test_unanswered_parts_are_none_not_empty(self):
        out = truenas.parse(pools=None, disks=None, temperatures=None, alerts=None)
        assert out == {"pools": None, "drives": None, "alerts": None}
        out = truenas.parse(pools=[], disks=[], temperatures={}, alerts=[])
        assert out == {"pools": [], "drives": [], "alerts": []}

    def test_a_missing_drive_and_odd_values(self):
        pools = _pools()
        leaf = pools[0]["topology"]["data"][0]["children"][1]
        leaf.update(disk=None, status="UNAVAIL")
        leaf["stats"].update(read_errors=True, write_errors=-3, checksum_errors="7")
        out = truenas.parse(pools=pools + ["junk", {"no": "name"}], disks=[None, {"x": 1}],
                            temperatures={"sdb": "hot"}, alerts=[None, {"level": "weird"}])
        drives = {d["name"]: d for d in out["drives"]}
        gone = drives["11111111-0000-0000-0000-000000000001"]
        assert gone["status"] == "UNAVAIL" and gone["pool"] == "tank"
        assert (gone["read_errors"], gone["write_errors"], gone["checksum_errors"]) == (0, 0, 0)
        assert drives["sdb"]["celsius"] is None and drives["sdb"]["model"] is None
        assert out["alerts"][0]["level"] == "INFO" and len(out["pools"]) == 1


# --------------------------------------------------------------------------
# Reading on a check, and the alerts
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def fake():
    server = FakeTrueNAS()
    server.start()
    return server


@pytest.fixture(autouse=True)
def _fresh(fake):
    fake.received.clear()
    fake.use("first")
    fake.answers = real()


@pytest.fixture
def db():
    config = _config()

    async def setup():
        D.init_engine(config)
        await D.init_db(config)
        async with D.session_scope() as s:
            s.add(Device(mac="aa:00:00:00:00:01", primary_ip="192.168.1.20", friendly_name="nas"))
    run(setup())
    yield config
    run(D.close_engine())


def check(db, ident):  # type: ignore[no-untyped-def]
    return run(credentials.check_one(db, ident))


def open_incidents():  # type: ignore[no-untyped-def]
    from spark.models import AlertIncident

    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            return [(r.key, r.title, r.closed_at is None, r.resolution) for r in
                    (await s.execute(select(AlertIncident).order_by(AlertIncident.id))).scalars()]
    return run(go())


def subjects():  # type: ignore[no-untyped-def]
    return [(kind, subject) for kind, subject, _ in run(_sent())]


class TestReading:
    def test_a_check_stores_what_it_read(self, db, fake):
        ident = run(_trusted(db, fake.host))
        row = run(_row(ident))
        assert [d["name"] for d in row.readings["drives"]] == ["sda", "sdb", "sdc", "sdd", "sde"]
        assert row.readings["pools"][0]["name"] == "tank" and row.readings["read_at"]

    def test_a_part_it_may_not_read_keeps_the_last(self, db, fake):
        ident = run(_trusted(db, fake.host))
        fake.answers["disk.temperatures"] = DENIED
        fake.answers["alert.list"] = DENIED
        assert check(db, ident) is True, "the check itself still works"
        row = run(_row(ident))
        assert row.readings["drives"][0]["celsius"] is None
        assert [a["uuid"] for a in row.readings["alerts"]] == ["a-usb", "a-rep", "a-upd"]

    def test_a_new_address_forgets_the_readings(self, db, fake):
        ident = run(_trusted(db, fake.host))

        async def move():  # type: ignore[no-untyped-def]
            from spark.vault import vault_for
            async with D.session_scope() as s:
                row = await s.get(credentials.ApiCredential, ident)
                await credentials.update(s, vault_for(db), row, name="truenas", device_id=1,
                                         host="truenas.lan", api_key="")
        run(move())
        assert run(_row(ident)).readings is None


class TestDriveAlerts:
    def test_errors_alert_once_and_clear(self, db, fake):
        ident = run(_trusted(db, fake.host))
        check(db, ident)
        assert [k for k, _ in subjects() if k.startswith("drive")] == []
        fake.answers["pool.query"] = _pools(c=3)
        check(db, ident)
        check(db, ident)
        sent = [x for x in run(_sent()) if x[0].startswith("drive")]
        assert [(k, s) for k, s, _ in sent] == [("drive_health_bad", "nas: drive sda has 3 errors")]
        assert sent[0][2] == ("`" + fake.host + "` — pool tank, raidz2-0: ONLINE; 0 read, 0 write, "
                              "3 checksum errors. MB012000JWDFD.")
        assert (f"apidrive:{ident}:sda", "nas: drive sda has 3 errors", True, None) in open_incidents()
        fake.answers["pool.query"] = _pools()
        check(db, ident)
        assert subjects()[-1] == ("drive_health_ok", "nas: drive sda is ONLINE with no errors")
        assert (f"apidrive:{ident}:sda", "nas: drive sda has 3 errors", False, "recovered") \
            in open_incidents()

    def test_not_online(self, db, fake):
        ident = run(_trusted(db, fake.host))
        fake.answers["pool.query"] = _pools(status="FAULTED", r=1)
        check(db, ident)
        assert ("drive_health_bad", "nas: drive sda is FAULTED") in subjects()

    def test_a_disk_outside_any_pool_never_alerts(self, db, fake):
        ident = run(_trusted(db, fake.host))
        check(db, ident)

        async def keys():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                return list((await s.execute(select(AlertState.key))).scalars())
        assert not [k for k in run(keys()) if k.endswith(":sde")]

    def test_a_failed_read_is_not_the_all_clear(self, db, fake):
        ident = run(_trusted(db, fake.host))
        fake.answers["pool.query"] = _pools(c=3)
        check(db, ident)
        fake.answers["pool.query"] = DENIED
        fake.answers["disk.query"] = DENIED
        check(db, ident)
        assert not [k for k, _ in subjects() if k == "drive_health_ok"]


class TestTrueNASAlerts:
    def test_warning_and_up_once_info_never(self, db, fake):
        ident = run(_trusted(db, fake.host))
        check(db, ident)
        check(db, ident)
        tn = [x for x in run(_sent()) if x[0].startswith("truenas_alert")]
        assert [(k, s) for k, s, _ in tn] == [(
            "truenas_alert_bad",
            "nas: 'boot-pool' is consuming USB devices 'sde' which is not recommended.")]
        assert tn[0][2] == f"`{fake.host}` — TrueNAS Warning (PoolUSBDisks)."

    def test_critical_is_bad_and_clearing_is_said(self, db, fake):
        from spark.models import Notification
        ident = run(_trusted(db, fake.host))
        fake.answers["alert.list"] = [SMART]
        check(db, ident)

        async def tones():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                return [n.tone for n in (await s.execute(select(Notification))).scalars()]
        assert run(tones())[-1] == "bad"
        fake.answers["alert.list"] = [{**SMART, "dismissed": True}]
        check(db, ident)
        assert subjects()[-1] == (
            "truenas_alert_ok",
            "nas: cleared in TrueNAS: Device: /dev/sda [SAT], 8 Currently unreadable (pending) sectors.")

    def test_an_unanswered_list_clears_nothing(self, db, fake):
        ident = run(_trusted(db, fake.host))
        fake.answers["alert.list"] = [SMART]
        check(db, ident)
        fake.answers["alert.list"] = DENIED
        check(db, ident)
        assert not [k for k, _ in subjects() if k == "truenas_alert_ok"]

    @pytest.mark.parametrize("how", ["rules off", "muted"])
    def test_quiet(self, db, fake, how):
        from spark import snmp_alerts
        from spark.db import save_setting
        from spark.web.routes_device_page import set_device_muted

        async def quiet():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                if how == "rules off":
                    await save_setting(s, snmp_alerts.SETTING, {
                        **await snmp_alerts.load(s), "drive_errors": False, "truenas_alerts": False})
                else:
                    await set_device_muted(s, 1, True)
        run(quiet())
        fake.answers["pool.query"] = _pools(c=3)
        ident = run(_trusted(db, fake.host))
        check(db, ident)
        assert not [k for k, _ in subjects() if k.startswith(("drive", "truenas"))]

    def test_removing_the_credential_forgets_them(self, db, fake):
        ident = run(_trusted(db, fake.host))
        fake.answers["pool.query"] = _pools(c=3)
        check(db, ident)

        async def keys():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                return list((await s.execute(select(AlertState.key))).scalars())
        assert any(k.startswith(f"apidrive:{ident}:") for k in run(keys()))
        assert any(k.startswith(f"tnalert:{ident}:") for k in run(keys()))

        async def remove():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                await credentials.forget_alert(s, ident)
        run(remove())
        assert run(keys()) == []
        assert open_incidents() and all(not is_open and why == "no longer watched"
                                        for _, _, is_open, why in open_incidents())


# --------------------------------------------------------------------------
# The Storage card
# --------------------------------------------------------------------------


@pytest.fixture
def site(db):
    with TestClient(create_app(db), follow_redirects=False) as client:
        client.post("/setup", data={"setup_code": client.app.state.setup_code, "username": "admin", "password": PASSWORD,
                                    "password_confirm": PASSWORD, "timezone": "UTC"})
        yield client


class TestCard:
    def test_without_snmp(self, site, db, fake):
        fake.answers["pool.query"] = _pools(c=3)
        fake.answers["disk.temperatures"] = {**TEMPS, "sdc": 55.0}
        run(_trusted(db, fake.host))
        page = flat(site.get("/devices/1").text)
        assert '<section class="card" id="storage">' in page
        assert "drive health from the TrueNAS API, last at" in page
        assert ('<td>tank</td> <td><span class="pill ok">ONLINE</span></td> '
                '<td class="muted small" colspan="3">Space is read over SNMP</td> '
                '<td class="small">Aug 30 · <span class="muted">0 errors</span></td>') in page
        assert ('<td><code>sda</code></td> <td class="muted small">MB012000JWDFD</td> '
                '<td class="num">12.0 TB</td> <td class="small">tank <span class="muted">· raidz2-0</span></td> '
                '<td><span class="pill ok">ONLINE</span></td> '
                '<td class="num"><span class="warn-text">0 / 0 / 3</span></td>') in page
        assert '<span class="warn-text">55°C</span>' in page
        assert '<td class="small"><span class="muted">not in a pool</span></td>' in page
        assert ('<li><span class="pill warn">warning</span> \'boot-pool\' is consuming USB devices '
                "'sde' which is not recommended.</li>") in page.replace("&#39;", "'")
        assert '<span class="pill neutral">info</span> A system update is available.' in page
        assert "SECRET-SERIAL" not in page

    def test_with_snmp_the_api_adds_to_the_same_card(self, site, db, fake):
        from datetime import timedelta

        from spark import storage
        from spark.collectors.snmp import DriveReading, PoolReading
        from spark.models import SnmpPoll, utcnow
        from spark.snmp_config import ProfileInput, add_device, save_profile
        from spark.vault import vault_for

        async def snmp():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                profile = await save_profile(s, vault_for(db), ProfileInput(
                    name="lab", version="v2c", community="x"))
                row = await add_device(s, 1, profile.id)
                s.add(SnmpPoll(snmp_device_id=row.id, last_polled_at=utcnow(),
                               last_ok_at=utcnow() - timedelta(seconds=5)))
                await s.flush()
                await storage.store(s, row.id, pools=[PoolReading("tank", "ONLINE", 5 * 10**12, 10**13)],
                                    drives=[DriveReading("sda", 43)], filesystems=[])
        run(snmp())
        run(_trusted(db, fake.host))
        page = flat(site.get("/devices/1").text)
        assert page.count('id="storage"') == 1
        assert "<th>Last scrub</th>" in page
        assert ('<td class="bar-col">' in page and
                '<td class="small">Aug 30 · <span class="muted">0 errors</span></td>' in page)
        assert "Space is read over SNMP" not in page, "tank is already in the SNMP rows"
        assert 'class="drive-temps"' not in page, "the API's drive table replaces the pills"
        assert '<td><code>sda</code></td>' in page

    def test_no_readings_no_card(self, site):
        assert 'id="storage"' not in site.get("/devices/1").text

    def test_the_rules_are_on_the_form(self, site):
        page = flat(site.get("/alerts/rules").text)
        assert 'name="drive_errors" value="1" checked' in page
        assert 'name="truenas_alerts" value="1" checked' in page


def test_migration_16_from_a_version_15_database():
    from sqlalchemy import inspect, text

    async def shape(wind_back: bool):  # type: ignore[no-untyped-def]
        config = _config()
        D.init_engine(config)
        await D.init_db(config)
        if wind_back:
            async with D.session_scope() as s:
                await s.execute(text("ALTER TABLE api_credential DROP COLUMN readings"))
                await D._set_version(s, 15)
            await D.close_engine()
            D.init_engine(config)
            await D.init_db(config)
        async with D.session_scope() as s:
            connection = await s.connection()
            columns = await connection.run_sync(
                lambda sync: sorted(c["name"] for c in inspect(sync).get_columns("api_credential")))
            version = await D._get_version(s)
        await D.close_engine()
        return columns, version

    assert run(shape(True)) == run(shape(False))
    assert "readings" in run(shape(False))[0]


def test_levels_and_health():
    assert truenas_health.forwarded("WARNING") and truenas_health.forwarded("EMERGENCY")
    assert not truenas_health.forwarded("NOTICE") and not truenas_health.forwarded(None)
    assert truenas_health.healthy({"status": "ONLINE"})
    assert not truenas_health.healthy({"status": "ONLINE", "write_errors": 1})
    assert not truenas_health.healthy({"status": "DEGRADED"})
