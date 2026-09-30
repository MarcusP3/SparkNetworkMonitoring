"""Storage over SNMP: TrueNAS pools and drives, filesystems on anything,
and the alerts on them.

The TrueNAS readings are the ones a real box returned (TrueNAS SCALE 25.10,
2026-09-29): pools tank and boot-pool both ONLINE, tank's root dataset at
index 1 with 8344112829504 bytes used, drives sda-sdd at 41-43 C (reported in
thousandths). The pool table there has no sizes -- its big numbers are I/O
counters -- so space comes from the root dataset.
"""

from __future__ import annotations

import asyncio
import re
import tempfile
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from spark import db as D
from spark import storage
from spark.collectors.snmp import (
    DriveReading,
    FilesystemReading,
    PoolReading,
    SnmpCollector,
    SnmpCredential,
)
from spark.config import Config
from spark.main import create_app
from spark.models import AlertMute, Device, Notification, SnmpPoll, SnmpStorage, utcnow
from spark.snmp_config import ProfileInput, add_device, save_profile
from spark.vault import vault_for

PASSWORD = "correct horse battery"
TB = 10 ** 12


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# Parsing what agents send
# --------------------------------------------------------------------------


class FakeAgent(SnmpCollector):
    def __init__(self, tables):  # type: ignore[no-untyped-def]
        super().__init__("192.0.2.9")
        self.tables = tables
        self.gets: list[tuple[str, ...]] = []

    async def walk(self, base_oid, max_rows=4096):  # type: ignore[no-untyped-def]
        return dict(self.tables.get(base_oid, {}))

    async def get(self, *oids):  # type: ignore[no-untyped-def]
        self.gets.append(oids)
        out = {}
        for oid in oids:
            base, _, index = oid.rpartition(".")
            value = self.tables.get(base, {}).get(index)
            if value is not None:
                out[oid] = value
        return out


P = "1.3.6.1.4.1.50536.1.1.1.1"
DS = "1.3.6.1.4.1.50536.1.6.1.1"
HDD = "1.3.6.1.4.1.50536.3.1"

TRUENAS = {
    f"{P}.2": {"1": "tank", "2": "boot-pool"},
    f"{P}.3": {"1": "ONLINE", "2": "ONLINE"},
    f"{DS}.2": {"1": "tank", "2": "tank/vms", "3": "tank/media"},
    f"{DS}.3": {"1": 8344112829504, "2": 515017137984, "3": 7682956009344},
    f"{DS}.4": {"1": 1055887170496, "2": 1, "3": 1},
    f"{HDD}.2": {"1": "sdb", "2": "sda", "3": "sdd", "4": "sdc", "5": "nvme0"},
    f"{HDD}.3": {"1": 42000, "2": 43000, "3": 41000, "4": 41000, "5": 0},
}

HR = "1.3.6.1.2.1.25.2.3.1"
FIXED, RAM = "1.3.6.1.2.1.25.2.1.4", "1.3.6.1.2.1.25.2.1.2"
LINUX = {
    f"{HR}.2": {"1": RAM, "31": FIXED, "32": FIXED, "33": FIXED, "34": FIXED, "35": FIXED,
                "36": FIXED, "37": FIXED, "38": FIXED},
    f"{HR}.3": {"1": "Physical memory", "31": "/", "32": "/boot", "33": "/run",
                "34": "/snap/core22/1380", "35": "/srv/data", "36": "/mnt/bind-of-root",
                "37": "/var/lib/docker/overlay2/abc/merged", "38": "/tiny"},
    f"{HR}.4": {"1": 1024, "31": 4096, "32": 4096, "33": 4096, "34": 1024, "35": 65536,
                "36": 4096, "37": 4096, "38": 4096},
    f"{HR}.5": {"1": 4000000, "31": 10_000_000, "32": 250_000, "33": 100_000, "34": 76_000,
                "35": -94_967_296, "36": 10_000_000, "37": 10_000_000, "38": 10},
    f"{HR}.6": {"1": 3000000, "31": 9_200_000, "32": 50_000, "33": 1_000, "34": 76_000,
                "35": 1_000_000_000, "36": 9_200_000, "37": 9_200_000, "38": 1},
}


class TestParsing:
    def test_truenas_pools_take_their_space_from_the_root_dataset(self):
        agent = FakeAgent(TRUENAS)
        assert run(agent.truenas_pools()) == [
            PoolReading("boot-pool", "ONLINE", None, None),
            PoolReading("tank", "ONLINE", 8344112829504, 8344112829504 + 1055887170496),
        ]
        assert agent.gets == [(f"{DS}.3.1", f"{DS}.4.1")], "one GET, root datasets only"

    def test_truenas_drives_in_degrees(self):
        assert run(FakeAgent(TRUENAS).truenas_drives()) == [
            DriveReading("sda", 43.0), DriveReading("sdb", 42.0),
            DriveReading("sdc", 41.0), DriveReading("sdd", 41.0),
        ], "and a drive reading 0 is left out"

    def test_not_truenas_is_nothing_and_asks_nothing_more(self):
        agent = FakeAgent(LINUX)
        assert run(agent.truenas_pools()) == [] and run(agent.truenas_drives()) == []
        assert agent.gets == []

    def test_filesystems(self):
        assert run(FakeAgent(LINUX).filesystems()) == [
            FilesystemReading("/", 10_000_000 * 4096, 9_200_000 * 4096),
            FilesystemReading("/boot", 250_000 * 4096, 50_000 * 4096),
            FilesystemReading("/srv/data", (4_200_000_000) * 65536, 1_000_000_000 * 65536),
        ], ("fixed disks only; /run, snaps and container layers out; the bind mount of / "
            "kept once; a negative size read as unsigned; a 40 KB mount is not a disk")

    def test_fmt_bytes(self):
        assert storage.fmt_bytes(8344112829504) == "8.3 TB"
        assert storage.fmt_bytes(512 * 10 ** 9) == "512 GB"
        assert storage.fmt_bytes(None) == "—" and storage.fmt_bytes(0) == "0 B"


# --------------------------------------------------------------------------
# Storing and alerting
# --------------------------------------------------------------------------


def _config() -> Config:
    tmp = Path(tempfile.mkdtemp(prefix="spark-storage-"))
    config = Config.model_validate({
        "app": {"data_dir": str(tmp / "data"), "log_level": "WARNING"},
        "network": {"subnets": []},
    })
    config.app.data_dir.mkdir(parents=True, exist_ok=True)
    return config


# 1 truenas (TrueNAS, polled as row 1), 2 a Linux server (row 2), 3 not answering (row 3).
@pytest.fixture
def db():
    config = _config()

    async def setup():
        D.init_engine(config)
        await D.init_db(config)
        async with D.session_scope() as s:
            s.add_all([Device(mac="aa:00:00:00:00:01", primary_ip="172.16.10.17", friendly_name="truenas"),
                       Device(mac="aa:00:00:00:00:02", primary_ip="172.16.10.18", friendly_name="linuxbox"),
                       Device(mac="aa:00:00:00:00:03", primary_ip="172.16.10.19", friendly_name="off")])
            await s.flush()
            profile = await save_profile(s, vault_for(config), ProfileInput(
                name="lab", version="v2c", community="x"))
            now = utcnow()
            for device_id, ok in ((1, True), (2, True), (3, False)):
                row = await add_device(s, device_id, profile.id)
                # 3 answered once, and has stopped: not worth 30 seconds.
                s.add(SnmpPoll(snmp_device_id=row.id, last_polled_at=now,
                               last_ok_at=now - timedelta(hours=0 if ok else 1),
                               last_error=None if ok else "No response."))
    run(setup())
    yield config
    run(D.close_engine())


def pool(pct: float | None = 50, health: str = "ONLINE") -> PoolReading:
    size = 10 * TB
    return PoolReading("tank", health, int(size * pct / 100) if pct is not None else None,
                       size if pct is not None else None)


async def read(row_id: int, *, at, pools=None, drives=None, filesystems=None) -> None:  # type: ignore[no-untyped-def]
    async with D.session_scope() as s:
        await storage.store(s, row_id, pools=pools, drives=drives, filesystems=filesystems)
        await storage.evaluate(s, row_id, at)


async def _sent() -> list[str]:
    async with D.session_scope() as s:
        return [n.subject for n in (await s.execute(
            select(Notification).order_by(Notification.id))).scalars()]


T0 = utcnow()


def at(minutes: int):  # type: ignore[no-untyped-def]
    return T0 + timedelta(minutes=minutes)


class TestAlerts:
    def test_a_pool_that_is_not_online_alerts_at_once_and_recovers(self, db):
        run(read(1, at=at(0), pools=[pool(health="DEGRADED")]))
        assert run(_sent()) == ["truenas: pool tank is DEGRADED"]
        run(read(1, at=at(5), pools=[pool(health="DEGRADED")]))

        async def incidents():  # type: ignore[no-untyped-def]
            from spark.models import AlertIncident
            async with D.session_scope() as s:
                return [(r.title, r.closed_at) for r in (await s.execute(select(AlertIncident))).scalars()]
        assert run(incidents()) == [("truenas: pool tank is DEGRADED", None)]
        run(read(1, at=at(10), pools=[pool()]))
        assert run(_sent())[1:] == ["truenas: pool tank is ONLINE again"]
        assert run(incidents()) == [("truenas: pool tank is DEGRADED", at(10))]

    def test_pool_space_needs_two_reads_and_clears_below_the_margin(self, db):
        run(read(1, at=at(0), pools=[pool(86)]))
        assert run(_sent()) == [], "one read over the line is not news"
        run(read(1, at=at(5), pools=[pool(86)]))
        assert run(_sent()) == ["truenas: pool tank is 86% full"]
        run(read(1, at=at(10), pools=[pool(82)]))
        assert len(run(_sent())) == 1, "82% is under 85 but inside the margin"
        run(read(1, at=at(15), pools=[pool(79)]))
        assert run(_sent())[1:] == ["truenas: pool tank is back to 79% full"]

    def test_a_disk_on_a_linux_server(self, db):
        fs = [FilesystemReading("/", 100 * 10 ** 9, 92 * 10 ** 9)]
        run(read(2, at=at(0), filesystems=fs))
        run(read(2, at=at(5), filesystems=fs))
        assert run(_sent()) == ["linuxbox: disk / is 92% full"]

    def test_a_hot_drive_must_stay_hot_for_its_minutes(self, db):
        for minute in (0, 5):
            run(read(1, at=at(minute), drives=[DriveReading("sda", 52)]))
        assert run(_sent()) == []
        run(read(1, at=at(10), drives=[DriveReading("sda", 52)]))
        assert run(_sent()) == ["truenas: drive sda is running hot (52°C)"]
        run(read(1, at=at(15), drives=[DriveReading("sda", 46)]))
        assert len(run(_sent())) == 1, "46 is inside the 5-degree margin"
        run(read(1, at=at(20), drives=[DriveReading("sda", 44)]))
        assert run(_sent())[1:] == ["truenas: drive sda is back to 44°C"]

    def test_a_rule_switched_off_and_a_muted_device_are_quiet(self, db):
        async def quiet():
            async with D.session_scope() as s:
                rules = await storage.snmp_alerts.load(s)
                rules["pool_health"] = False
                await storage.snmp_alerts.save_setting(s, "alert_rules", rules)
                s.add(AlertMute(device_id=2))
        run(quiet())
        run(read(1, at=at(0), pools=[pool(health="FAULTED")]))
        fs = [FilesystemReading("/", 100, 99)]
        run(read(2, at=at(0), filesystems=fs))
        run(read(2, at=at(5), filesystems=fs))
        assert run(_sent()) == []


class TestReading:
    def run_one(self, monkeypatch, db, row_id: int, *, pools, drives=(), fs=(), fails=False):  # type: ignore[no-untyped-def]
        asked: list[str] = []

        def answer(name, value):  # type: ignore[no-untyped-def]
            async def method(self):  # type: ignore[no-untyped-def]
                asked.append(name)
                if fails:
                    raise TimeoutError("no answer")
                return list(value)
            return method
        monkeypatch.setattr(SnmpCollector, "truenas_pools", answer("pools", pools))
        monkeypatch.setattr(SnmpCollector, "truenas_drives", answer("drives", drives))
        monkeypatch.setattr(SnmpCollector, "filesystems", answer("fs", fs))
        return run(storage.refresh_one(db, row_id)), asked

    async def _rows(self):  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            return sorted((r.snmp_device_id, r.kind, r.name) for r in
                          (await s.execute(select(SnmpStorage))).scalars())

    def test_truenas_gives_pools_and_drives_not_its_mounts(self, db, monkeypatch):
        ok, asked = self.run_one(monkeypatch, db, 1, pools=[pool()], drives=[DriveReading("sda", 40)])
        assert ok and asked == ["pools", "drives"]
        assert run(self._rows()) == [(1, "drive", "sda"), (1, "pool", "tank")]

    def test_anything_else_gives_filesystems(self, db, monkeypatch):
        ok, asked = self.run_one(monkeypatch, db, 2, pools=[],
                                 fs=[FilesystemReading("/", 10 ** 11, 10 ** 10)])
        assert ok and asked == ["pools", "fs"]
        assert run(self._rows()) == [(2, "fs", "/")]

    def test_no_answer_keeps_the_last_reading_and_asks_nothing_more(self, db, monkeypatch):
        self.run_one(monkeypatch, db, 1, pools=[pool()])
        ok, asked = self.run_one(monkeypatch, db, 1, pools=[pool()], fails=True)
        assert not ok and asked == ["pools"]
        assert run(self._rows()) == [(1, "pool", "tank")]

    def test_the_job_asks_only_devices_that_answer_their_polls(self, db, monkeypatch):
        seen: list[int] = []

        async def one(config, row_id):  # type: ignore[no-untyped-def]
            seen.append(row_id)
            return True
        monkeypatch.setattr(storage, "refresh_one", one)
        assert run(storage.refresh(db)) == 2 and seen == [1, 2]


def test_a_real_agent_reports_its_root_filesystem():
    async def go():
        c = SnmpCollector("127.0.0.1", SnmpCredential(community="sparktest", port=11161,
                                                      timeout=1.0, retries=0))
        try:
            return await c.filesystems(), await c.truenas_pools()
        except Exception:  # noqa: BLE001
            return None, None
        finally:
            await c.close()
    filesystems, pools = run(go())
    if filesystems is None:
        pytest.skip("no agent on 127.0.0.1:11161 -- run tests/local_agent.sh start")
    assert pools == []
    root = next(f for f in filesystems if f.name == "/")
    assert 0 < root.used_bytes < root.size_bytes


# --------------------------------------------------------------------------
# The pages
# --------------------------------------------------------------------------


@pytest.fixture
def site(db):
    with TestClient(create_app(db), follow_redirects=False) as client:
        client.post("/setup", data={"setup_code": client.app.state.setup_code, "username": "admin", "password": PASSWORD,
                                    "password_confirm": PASSWORD, "timezone": "UTC"})
        yield client


def flat(html: str) -> str:
    return " ".join(html.split())


class TestPages:
    def test_the_device_page_shows_pools_and_drives(self, site):
        run(read(1, at=at(0), pools=[pool(86), PoolReading("boot-pool", "DEGRADED")],
                 drives=[DriveReading("sda", 43), DriveReading("sdb", 51)]))
        page = flat(site.get("/devices/1").text)
        assert '<section class="card" id="storage">' in page
        assert '<td>tank</td> <td><span class="pill ok">ONLINE</span></td>' in page
        assert "8.6 TB of 10.0 TB" in page and '<span class="warn-text small">(86%)</span>' in page
        assert '<span class="pill warn">DEGRADED</span>' in page
        assert "Health only (TrueNAS gives no space for this pool)" in page
        assert re.search(r'class="pill neutral" title="sda: 43°C">sda', page)
        assert re.search(r'class="pill warn" title="sdb: 51°C, at or over 50°C">sdb', page)

    def test_and_filesystems_elsewhere(self, site):
        run(read(2, at=at(0), filesystems=[FilesystemReading("/", 100 * 10 ** 9, 40 * 10 ** 9)]))
        page = flat(site.get("/devices/2").text)
        assert "<td><code>/</code></td> <td>40.0 GB of 100 GB" in page
        assert 'id="storage"' not in site.get("/devices/3").text, "nothing read, no card"

    def test_the_rules_are_in_settings(self, site):
        page = site.get("/settings/alerts").text
        for field, value in (("pool_space_percent", 85), ("disk_space_percent", 90),
                             ("drive_celsius", 50), ("drive_minutes", 10)):
            assert f'name="{field}"' in page and f'value="{value}"' in page, field
        assert 'name="pool_health" value="1" checked' in page


def test_the_job_is_scheduled_a_minute_after_start(db):
    from spark import scheduler as S

    async def body():
        S.start()
        try:
            assert S.schedule_storage(db)
            job = S.get_scheduler().get_job(storage.JOB_ID)
            first = (job.next_run_time - utcnow()).total_seconds()
            assert 45 < first <= 60
            assert job.trigger.interval == timedelta(minutes=5)
        finally:
            await S.shutdown()
    run(body())


def test_migration_14_from_a_version_13_database():
    from sqlalchemy import inspect, text

    async def shape(wind_back: bool):
        config = _config()
        D.init_engine(config)
        await D.init_db(config)
        if wind_back:
            async with D.session_scope() as s:
                await s.execute(text("DROP TABLE snmp_storage"))
                await D._set_version(s, 13)
            await D.close_engine()
            D.init_engine(config)
            await D.init_db(config)
        async with D.session_scope() as s:
            connection = await s.connection()
            columns = await connection.run_sync(
                lambda sync: sorted(c["name"] for c in inspect(sync).get_columns("snmp_storage")))
            version = await D._get_version(s)
            rules = await D.get_setting(s, "alert_rules")
        await D.close_engine()
        return columns, version, rules["pool_space_percent"], rules["drive_celsius"]

    assert run(shape(True)) == run(shape(False))
    assert run(shape(False))[1:] == (D.CURRENT_VERSION, 85, 50)
    assert D.CURRENT_VERSION >= 14

