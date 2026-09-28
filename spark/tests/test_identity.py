"""SNMP identity: a polled device's own addresses and ARP table, and what
SPARK makes of them -- merge suggestions a person confirms, and MACs filled
in for devices across a router.

The network in the fixture: a firewall polled over SNMP, with its gateway on
four VLANs; SPARK found two of those gateways as devices of their own. A
server with VLAN interfaces on one NIC, seen by MAC on SPARK's own VLAN and by
IP alone on another. Hosts across the router with no MAC yet.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from spark import db as D
from spark import identity, merge
from spark.collectors.snmp import SnmpCollector, SnmpCredential, usable_ip, usable_mac
from spark.config import Config
from spark.main import create_app
from spark.models import Device, SnmpAddress, SnmpDevice, utcnow
from spark.snmp_config import ProfileInput, add_device, save_profile
from spark.vault import vault_for

PASSWORD = "correct horse battery"

FW_MAC = "aa:bb:cc:00:00:01"
SERVER_MAC = "aa:bb:cc:00:00:04"


def _config() -> Config:
    tmp = Path(tempfile.mkdtemp(prefix="spark-identity-"))
    config = Config.model_validate({
        "app": {"data_dir": str(tmp / "data"), "log_level": "WARNING"},
        "network": {"subnets": []},
    })
    config.app.data_dir.mkdir(parents=True, exist_ok=True)
    return config


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


# Device ids, in the order the fixture adds them.
FIREWALL, GW20, GW30, SERVER, SERVER20, HOST40, TWIN_A, TWIN_B, STRANGER, SKIPPED = range(1, 11)


@pytest.fixture
def db():
    config = _config()

    async def setup():
        D.init_engine(config)
        await D.init_db(config)
        async with D.session_scope() as s:
            s.add_all([
                Device(mac=FW_MAC, primary_ip="10.1.10.1", friendly_name="firewall"),
                Device(primary_ip="10.1.20.1"),                       # GW20: by IP
                Device(primary_ip="10.1.30.1", friendly_name="iot gw"),
                Device(mac=SERVER_MAC, primary_ip="10.1.10.20", friendly_name="nas"),
                Device(primary_ip="10.1.20.20"),                      # the nas on VLAN 20
                Device(primary_ip="10.1.40.5"),                       # HOST40
                Device(primary_ip="10.1.50.5"),                       # TWIN_A
                Device(primary_ip="10.1.50.6"),                       # TWIN_B
                # At a firewall address, but a different real device.
                Device(mac="aa:bb:cc:00:00:99", primary_ip="10.1.60.1"),
                Device(primary_ip="10.1.70.1", ignored=True),         # SKIPPED
            ])
            await s.flush()
            profile = await save_profile(s, vault_for(config), ProfileInput(
                name="lab", version="v2c", community="x"))
            await add_device(s, FIREWALL, profile.id)
            await identity.store(
                s, 1,
                own=["10.1.10.1", "10.1.20.1", "10.1.30.1", "10.1.60.1", "10.1.70.1"],
                arp={"10.1.10.20": SERVER_MAC, "10.1.20.20": SERVER_MAC,
                     "10.1.20.1": "aa:bb:cc:00:00:77",    # never used: own address
                     "10.1.40.5": "aa:bb:cc:00:00:40",
                     "10.1.50.5": "aa:bb:cc:00:00:50", "10.1.50.6": "aa:bb:cc:00:00:50"},
            )
    run(setup())
    yield config
    run(D.close_engine())


async def _suggestions():
    async with D.session_scope() as s:
        return [(x.keep.id, x.other.id, x.kind) for x in await identity.suggestions(s)]


async def _get(model, ident):  # type: ignore[no-untyped-def]
    async with D.session_scope() as s:
        return await s.get(model, ident)


async def _fill():
    async with D.session_scope() as s:
        return [d.id for d in await identity.fill_in_macs(s)]


class TestSuggestions:
    def test_what_is_suggested(self, db):
        assert sorted(run(_suggestions())) == sorted([
            (FIREWALL, GW20, "own"),        # its own address
            (FIREWALL, GW30, "own"),
            (SERVER, SERVER20, "arp"),      # same MAC as the nas
            (TWIN_A, TWIN_B, "arp"),        # one MAC nobody has, two addresses
        ])

    def test_left_out(self, db):
        others = {other for _, other, _ in run(_suggestions())}
        assert STRANGER not in others, "two different MACs are two devices (merge.py)"
        assert SKIPPED not in others, "ignored"
        assert HOST40 not in others, "its MAC is its own, not a duplicate"

    def test_nothing_is_merged_or_changed_by_suggesting(self, db):
        run(_suggestions())
        assert run(_get(Device, GW20)) is not None and run(_get(Device, GW20)).mac is None

    def test_a_dismissed_suggestion_stays_gone(self, db):
        async def dismiss():
            async with D.session_scope() as s:
                await identity.dismiss(s, FIREWALL, "10.1.20.1")
                await identity.dismiss(s, FIREWALL, "10.1.20.1")
                return (await D.get_setting(s, identity.DISMISSED))["pairs"]
        assert run(dismiss()) == [f"{FIREWALL}:10.1.20.1"], "once, not twice"
        assert (FIREWALL, GW20, "own") not in run(_suggestions())
        assert (FIREWALL, GW30, "own") in run(_suggestions())

    def test_two_polled_devices_are_not_suggested(self, db):
        async def poll_gw():
            async with D.session_scope() as s:
                profile_id = (await s.get(SnmpDevice, 1)).profile_id
                await add_device(s, GW20, profile_id)
        run(poll_gw())
        assert (FIREWALL, GW20, "own") not in run(_suggestions())

    def test_routers_that_disagree_about_an_address_are_not_believed(self, db):
        async def second_router():
            async with D.session_scope() as s:
                profile_id = (await s.get(SnmpDevice, 1)).profile_id
                row = await add_device(s, STRANGER, profile_id)
                await identity.store(s, row.id, own=[], arp={"10.1.20.20": "aa:bb:cc:00:00:22"})
        run(second_router())
        assert (SERVER, SERVER20, "arp") not in run(_suggestions())

    def test_no_reports_no_suggestions(self, db):
        async def clear():
            async with D.session_scope() as s:
                await s.execute(SnmpAddress.__table__.delete())
        run(clear())
        assert run(_suggestions()) == []

    def test_the_reasons_read_as_sentences(self, db):
        async def reasons():
            async with D.session_scope() as s:
                return {x.other.id: identity.why(x) for x in await identity.suggestions(s)}
        said = run(reasons())
        assert said[GW20] == "firewall reports 10.1.20.1 as one of its own addresses."
        assert said[SERVER20] == ("firewall's ARP table shows 10.1.20.20 and 10.1.10.20 "
                                  f"at the same MAC, {SERVER_MAC}.")


class TestFillingInMacs:
    def test_a_host_across_the_router_gets_its_mac(self, db):
        assert run(_fill()) == [HOST40]
        assert run(_get(Device, HOST40)).mac == "aa:bb:cc:00:00:40"

    def test_never_for_a_duplicate_or_a_mac_already_taken(self, db):
        run(_fill())
        assert run(_get(Device, GW20)).mac is None, "own address: awaiting a merge"
        assert run(_get(Device, SERVER20)).mac is None, "the nas has that MAC"
        assert run(_get(Device, TWIN_A)).mac is None and run(_get(Device, TWIN_B)).mac is None

    def test_the_merge_suggested_still_works_after(self, db):
        run(_fill())
        async def go():
            async with D.session_scope() as s:
                await merge.apply(s, FIREWALL, GW20)
        run(go())
        assert run(_get(Device, GW20)) is None


class TestStore:
    async def _rows(self, kind):  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            return sorted((r.ip, r.mac) for r in (await s.execute(
                select(SnmpAddress).where(SnmpAddress.kind == kind))).scalars())

    def test_an_answer_replaces_and_no_answer_keeps(self, db):
        async def again(own, arp):  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                await identity.store(s, 1, own=own, arp=arp)
        run(again(["10.1.10.1"], None))
        assert run(self._rows("own")) == [("10.1.10.1", None)]
        assert len(run(self._rows("arp"))) == 6, "the ARP walk failed: keep the last one"
        run(again(None, {}))
        assert run(self._rows("arp")) == [] and len(run(self._rows("own"))) == 1


# --------------------------------------------------------------------------
# The collector: parsing what agents send
# --------------------------------------------------------------------------


class FakeAgent(SnmpCollector):
    def __init__(self, tables):  # type: ignore[no-untyped-def]
        super().__init__("192.0.2.9")
        self.tables = tables

    async def walk(self, base_oid, max_rows=4096):  # type: ignore[no-untyped-def]
        return dict(self.tables.get(base_oid, {}))

    async def walk_raw(self, base_oid, max_rows=4096):  # type: ignore[no-untyped-def]
        return dict(self.tables.get(base_oid, {}))


MAC = bytes.fromhex("02fc00000005")


class TestParsing:
    def test_own_addresses_from_the_old_table(self):
        agent = FakeAgent({"1.3.6.1.2.1.4.20.1.1": {
            "127.0.0.1": "127.0.0.1", "10.1.20.1": "10.1.20.1", "10.1.3.1": "10.1.3.1",
            "169.254.1.1": "169.254.1.1"}})
        assert run(agent.own_addresses()) == ["10.1.3.1", "10.1.20.1"]

    def test_own_addresses_from_the_new_table_unicast_only(self):
        agent = FakeAgent({"1.3.6.1.2.1.4.34.1.4": {
            "1.4.10.1.20.1": 1, "1.4.10.1.20.255": 3, "1.4.127.0.0.1": 1,
            "2.16.254.128.0.0.0.0.0.0.0.0.0.0.0.0.0.1": 1}})
        assert run(agent.own_addresses()) == ["10.1.20.1"]

    def test_arp_from_the_old_table(self):
        agent = FakeAgent({
            "1.3.6.1.2.1.4.22.1.2": {
                "4.10.1.20.5": MAC, "4.10.1.20.6": MAC,       # 6 is marked invalid
                "4.10.1.20.7": b"",                          # incomplete
                "4.10.1.20.8": bytes.fromhex("ffffffffffff"),
                "4.10.1.20.9": bytes.fromhex("01005e000001"),  # multicast
                "4.10.1.20.10": bytes(6)},
            "1.3.6.1.2.1.4.22.1.4": {"4.10.1.20.5": 3, "4.10.1.20.6": 2},
        })
        assert run(agent.arp_table()) == {"10.1.20.5": "02:fc:00:00:00:05"}

    def test_arp_from_the_new_table(self):
        agent = FakeAgent({"1.3.6.1.2.1.4.35.1.4": {
            "4.1.4.10.1.20.5": MAC, "4.2.16.254.128.0.0.0.0.0.0.0.0.0.0.0.0.0.1": MAC}})
        assert run(agent.arp_table()) == {"10.1.20.5": "02:fc:00:00:00:05"}

    def test_usable(self):
        assert usable_ip("10.0.0.1") and not usable_ip("0.0.0.0") and not usable_ip("fe80::1")
        assert not usable_ip("255.255.255.255") and not usable_ip("224.0.0.5")
        assert usable_mac("02:fc:00:00:00:05") and not usable_mac("33:33:00:00:00:01")
        assert not usable_mac(None) and not usable_mac("00:00:00:00:00:00")


# --------------------------------------------------------------------------
# Against a real agent (tests/local_agent.sh)
# --------------------------------------------------------------------------


def _agent_up() -> bool:
    async def ask():
        c = SnmpCollector("127.0.0.1", SnmpCredential(community="sparktest", port=11161,
                                                      timeout=1.0, retries=0))
        try:
            return bool(await c.get("1.3.6.1.2.1.1.5.0"))
        except Exception:  # noqa: BLE001
            return False
        finally:
            await c.close()
    return run(ask())


needs_agent = pytest.mark.skipif(
    not _agent_up(), reason="no agent on 127.0.0.1:11161 -- run tests/local_agent.sh start"
)


@needs_agent
def test_a_real_agent_end_to_end():
    """Read a real agent, then find a device at one of its addresses suggested."""
    config = _config()

    async def go():
        c = SnmpCollector("127.0.0.1", SnmpCredential(community="sparktest", port=11161))
        try:
            own, arp = await c.own_addresses(), await c.arp_table()
        finally:
            await c.close()
        assert "127.0.0.1" not in own, "loopback identifies nothing"
        assert all(usable_ip(ip) and usable_mac(mac) for ip, mac in arp.items())
        if not own:
            pytest.skip("this agent reports no address but loopback")

        D.init_engine(config)
        await D.init_db(config)
        async with D.session_scope() as s:
            s.add_all([Device(mac=FW_MAC, primary_ip="127.0.0.1", friendly_name="lab"),
                       Device(primary_ip=own[0])])
            await s.flush()
            profile = await save_profile(s, vault_for(config), ProfileInput(
                name="lab", version="v2c", community="sparktest", port="11161"))
            await add_device(s, 1, profile.id)
        assert await identity.refresh(config) == 1
        async with D.session_scope() as s:
            stored = sorted(r.ip for r in (await s.execute(
                select(SnmpAddress).where(SnmpAddress.kind == "own"))).scalars())
            found = [(x.keep.id, x.other.id) for x in await identity.suggestions(s)]
        await D.close_engine()
        return own, stored, found
    own, stored, found = run(go())
    assert stored == sorted(own), "stored as the agent said it"
    assert found == [(1, 2)]


def test_a_device_that_does_not_answer_keeps_what_it_said(monkeypatch):
    """And is not asked a second time: one timeout per silent device, not two."""
    config = _config()
    asked_arp = []

    async def arp_table(self):  # type: ignore[no-untyped-def]
        asked_arp.append(self.host)
        return {}
    monkeypatch.setattr(SnmpCollector, "arp_table", arp_table)

    async def go():
        D.init_engine(config)
        await D.init_db(config)
        async with D.session_scope() as s:
            s.add(Device(primary_ip="127.0.0.1"))
            await s.flush()
            profile = await save_profile(s, vault_for(config), ProfileInput(
                name="dead", version="v2c", community="x", port="9"))
            await add_device(s, 1, profile.id)
            await identity.store(s, 1, own=["10.9.9.9"], arp={})
        identity.TIMEOUT = 0.3
        try:
            answered = await identity.refresh(config)
        finally:
            identity.TIMEOUT = 3.0
        async with D.session_scope() as s:
            rows = [r.ip for r in (await s.execute(select(SnmpAddress))).scalars()]
        await D.close_engine()
        return answered, rows
    assert run(go()) == (0, ["10.9.9.9"])
    assert asked_arp == []


# --------------------------------------------------------------------------
# The pages
# --------------------------------------------------------------------------


@pytest.fixture
def site(db):
    with TestClient(create_app(db), follow_redirects=False) as client:
        client.post("/setup", data={"username": "admin", "password": PASSWORD,
                                    "password_confirm": PASSWORD, "timezone": "UTC"})
        yield client


def flat(html: str) -> str:
    return " ".join(html.split())


class TestPages:
    def test_the_kept_device_lists_them(self, site):
        page = flat(site.get(f"/devices/{FIREWALL}").text)
        assert '<a href="#suggested">2 possible duplicates</a>' in page
        assert f'href="/devices/{FIREWALL}/merge?other={GW20}">Review merge…</a>' in page
        assert "firewall reports 10.1.30.1 as one of its own addresses." in page
        assert "Over SNMP it reports 5 addresses of its own and 6 ARP entries" in page

    def test_the_duplicate_says_what_it_is_part_of(self, site):
        page = flat(site.get(f"/devices/{SERVER20}").text)
        assert f'Looks like part of <a href="/devices/{SERVER}">nas</a>' in page
        assert f'href="/devices/{SERVER}/merge?other={SERVER20}">Review merge…</a>' in page

    def test_the_devices_page_lists_them_all(self, site):
        page = flat(site.get("/devices").text)
        assert '<section class="card suggest-card" id="suggested">' in page
        assert page.count("Review merge…") == 4

    def test_the_preview_says_why_and_merging_is_the_usual_merge(self, site):
        preview = flat(site.get(f"/devices/{FIREWALL}/merge?other={GW20}").text)
        assert "Suggested by SNMP: firewall reports 10.1.20.1 as one of its own addresses." in preview
        site.post(f"/devices/{FIREWALL}/merge", data={"other_id": str(GW20)})
        assert run(_get(Device, GW20)) is None
        assert "1 possible duplicate<" in flat(site.get(f"/devices/{FIREWALL}").text)

    def test_a_merge_picked_by_hand_says_no_reason(self, site):
        preview = site.get(f"/devices/{FIREWALL}/merge?other={HOST40}").text
        assert "Suggested by SNMP" not in preview and "Not the same" not in preview

    @pytest.mark.parametrize("back, lands", [
        ("/devices#suggested", "/devices#suggested"),
        (f"/devices/{GW20}#addresses", f"/devices/{GW20}#addresses"),
        (f"/devices/{FIREWALL}#addresses", f"/devices/{FIREWALL}#addresses"),
        ("https://evil.example/", f"/devices/{FIREWALL}#addresses"),
        (f"/devices/{SERVER}#addresses", f"/devices/{FIREWALL}#addresses"),
    ])
    def test_not_the_same(self, site, back, lands):
        response = site.post(f"/devices/{FIREWALL}/merge/dismiss",
                             data={"other_id": str(GW20), "back": back})
        assert response.headers["location"] == lands
        assert (FIREWALL, GW20, "own") not in run(_suggestions())
        assert run(_get(Device, GW20)) is not None, "dismissing merges nothing"

    def test_not_the_same_with_nonsense_changes_nothing(self, site):
        site.post(f"/devices/{FIREWALL}/merge/dismiss", data={"other_id": "99999999999999999999"})
        site.post(f"/devices/{FIREWALL}/merge/dismiss", data={"other_id": "abc"})
        assert len(run(_suggestions())) == 4


# --------------------------------------------------------------------------
# Scheduling and the schema
# --------------------------------------------------------------------------


def test_the_job_runs_soon_after_start_and_sooner_for_a_new_device(db):
    from datetime import timedelta

    from spark import scheduler as S

    async def body():
        S.start()
        try:
            assert S.schedule_identity(db)
            job = S.get_scheduler().get_job(identity.JOB_ID)
            first = job.next_run_time - utcnow()
            assert timedelta(seconds=30) < first < timedelta(seconds=60)
            assert job.trigger.interval == timedelta(minutes=identity.REFRESH_MINUTES)
            job.modify(next_run_time=utcnow() + timedelta(minutes=10))
            await S.sync_snmp_jobs(db)          # device 1 on the list: a new poll job
            soon = S.get_scheduler().get_job(identity.JOB_ID).next_run_time - utcnow()
            assert soon < timedelta(seconds=60)
        finally:
            await S.shutdown()
    run(body())


def test_migration_11_from_a_version_10_database():
    from sqlalchemy import inspect, text

    async def shape(wind_back: bool):
        config = _config()
        D.init_engine(config)
        await D.init_db(config)
        if wind_back:
            async with D.session_scope() as s:
                await s.execute(text("DROP TABLE snmp_address"))
                await D._set_version(s, 10)
            await D.close_engine()
            D.init_engine(config)
            await D.init_db(config)
        async with D.session_scope() as s:
            connection = await s.connection()
            columns = await connection.run_sync(
                lambda sync: sorted(c["name"] for c in inspect(sync).get_columns("snmp_address")))
            version = await D._get_version(s)
            dismissed = await D.get_setting(s, identity.DISMISSED)
        await D.close_engine()
        return columns, version, dismissed

    assert run(shape(True)) == run(shape(False))
    columns, version, dismissed = run(shape(False))
    assert version == D.CURRENT_VERSION >= 11
    assert dismissed == {"pairs": []}
