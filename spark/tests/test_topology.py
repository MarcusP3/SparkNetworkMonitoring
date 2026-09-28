"""The map from SNMP: which switch port each device is on, and what is
behind what -- worked out from MAC tables and LLDP, and only ever suggested.

Most of it is `topology.infer`, pure, run against several shapes of network:
a UniFi home network (switches with MAC tables and no LLDP, a firewall
and access points with neither, a mesh AP), one with LLDP everywhere, and the ways a
report can be incomplete or contradict itself. Then the same thing through
the database and the pages.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from spark import db as D
from spark import topology
from spark.collectors.snmp import FdbEntry, LldpNeighbour, SnmpCollector
from spark.config import Config
from spark.main import create_app
from spark.models import Device, DeviceRole, SnmpInterface, SnmpNeighbour
from spark.snmp_config import ProfileInput, add_device, save_profile
from spark.topology import Link, Net, infer
from spark.vault import vault_for

PASSWORD = "correct horse battery"


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# The inference
# --------------------------------------------------------------------------

# A UniFi home network. 1 firewall (root; no MAC table). 2 core switch, uplink on
# port 1. 3 second switch on core port 8, uplink on its port 1. 4 wired AP
# on switch 3 port 3; 5 mesh AP whose traffic comes through 4; 6 a wireless
# client. 7 a server on core port 2 with 8 a VM bridged behind it. 9 a
# printer on switch 3 port 5. 10 something plugged into the firewall itself.
# 11 a device no switch has seen.
FW, CORE, SW, AP, MESH, PHONE, NAS, VM, PRINTER, ON_FW, UNSEEN = range(1, 12)
M = {i: f"aa:00:00:00:00:{i:02x}" for i in range(1, 12)}


def home(**over) -> Net:  # type: ignore[no-untyped-def]
    net = Net(
        macs={i: {M[i]} for i in M} | {FW: {M[FW], "aa:00:00:00:01:01"}},
        tables={
            CORE: {1: {M[FW], M[ON_FW]}, 2: {M[NAS], M[VM]},
                   8: {M[SW], M[AP], M[MESH], M[PHONE], M[PRINTER]}},
            SW: {1: {"aa:00:00:00:01:01", M[ON_FW], M[CORE], M[NAS], M[VM]},
                 3: {M[AP], M[MESH], M[PHONE]}, 5: {M[PRINTER]}},
        },
        lldp={},
        infra={FW, CORE, SW, AP, MESH, NAS},
        root=FW,
    )
    for key, value in over.items():
        setattr(net, key, value)
    return net


def parents(links: dict[int, Link]) -> dict[int, int | None]:
    return {device: link.parent for device, link in links.items()}


class TestMacTables:
    def test_the_home_network(self):
        links = infer(home())
        assert parents(links) == {
            FW: None, CORE: FW, SW: CORE, AP: SW, MESH: SW, PHONE: SW,
            NAS: CORE, VM: NAS, PRINTER: SW, ON_FW: FW,
        }
        assert links[SW] == Link(CORE, CORE, 8, "mac"), "the parent's port"
        assert links[PRINTER] == Link(SW, SW, 5, "mac")
        assert links[VM] == Link(NAS, CORE, 2, "behind")
        assert links[CORE] == Link(FW, CORE, 1, "top"), "no switch above it: off the gateway"
        assert links[ON_FW] == Link(FW, None, None, "uplink")
        assert UNSEEN not in links

    def test_two_access_points_on_one_port_cannot_be_told_apart(self):
        # The wired AP and the mesh AP both arrive on switch port 3: which one
        # the phone is behind is unknown, so all three stay on the switch.
        assert {d: infer(home())[d].source for d in (AP, MESH, PHONE)} == dict.fromkeys(
            (AP, MESH, PHONE), "mac")

    def test_until_a_hand_set_parent_says_which_is_wired(self):
        links = infer(home(parents={MESH: AP}))
        assert links[PHONE] == Link(AP, SW, 3, "behind")
        assert links[MESH].parent == AP and links[AP].parent == SW

    def test_no_gateway_no_map(self):
        assert infer(home(root=None)) == {}

    def test_a_switch_that_never_saw_the_gateway_is_not_used(self):
        tables = home().tables
        tables[SW][1] = {M[CORE], M[NAS]}          # the firewall's MAC aged out
        links = infer(home(tables=tables))
        assert links[PRINTER] == Link(CORE, CORE, 8, "mac"), "placed by the switch above"

    def test_the_gateway_found_by_any_of_its_macs(self):
        tables = home().tables
        tables[CORE][1] = {"aa:00:00:00:01:01"}    # its other interface's MAC
        assert infer(home(tables=tables))[SW].parent == CORE

    def test_disagreeing_tables_place_nothing(self):
        # Two switches each see the printer downstream and neither sees the
        # other: stale entries. No guess.
        tables = home().tables
        tables[SW][1] = {"aa:00:00:00:01:01"}
        tables[CORE][8] = {M[PRINTER]}
        assert PRINTER not in infer(home(tables=tables))

    def test_a_gateway_with_its_own_mac_table(self):
        # A core switch doing the routing: every port of it is downstream.
        net = home(tables={FW: {4: {M[CORE], M[PRINTER]}}, CORE: {1: {M[FW]}, 9: {M[PRINTER]}}})
        links = infer(net)
        assert links[CORE] == Link(FW, FW, 4, "mac") and links[PRINTER] == Link(CORE, CORE, 9, "mac")


class TestLldp:
    # 1 router (root), 2 and 3 switches that all speak LLDP.
    def net(self, lldp, tables=None):  # type: ignore[no-untyped-def]
        return Net(macs={1: {"r"}, 2: {"s2"}, 3: {"s3"}, 4: {"h"}},
                   tables=tables if tables is not None else {
                       2: {1: {"r"}, 5: {"s3", "h"}}, 3: {7: {"r", "s2"}, 2: {"h"}}},
                   lldp=lldp, infra={1, 2, 3}, root=1)

    def test_both_ends_report_and_the_parent_side_names_the_port(self):
        links = infer(self.net({2: [(5, 3), (1, 1)], 3: [(7, 2)]}))
        assert links[3] == Link(2, 2, 5, "lldp")
        assert links[2] == Link(1, 2, 1, "lldp")
        assert links[4] == Link(3, 3, 2, "mac")

    def test_it_overrides_the_mac_tables(self):
        # The tables alone would put switch 3 under 2; LLDP says the router.
        links = infer(self.net({1: [(3, 3)]}))
        assert links[3] == Link(1, 1, 3, "lldp")

    def test_a_switch_with_lldp_and_no_mac_table(self):
        links = infer(self.net({3: [(1, 1)]}, tables={}))
        assert links[3] == Link(1, 3, 1, "lldp")

    def test_contradictions_are_dropped_not_drawn(self):
        # Each hears the other on a downstream port: a loop, so neither.
        links = infer(self.net({2: [(5, 3)], 3: [(2, 2)]},
                               tables={2: {1: {"r"}, 5: {"s3"}}, 3: {1: {"r"}, 2: {"s2"}}}))
        assert 2 not in links and 3 not in links


# --------------------------------------------------------------------------
# The collector: parsing what switches send
# --------------------------------------------------------------------------


class FakeAgent(SnmpCollector):
    def __init__(self, tables):  # type: ignore[no-untyped-def]
        super().__init__("192.0.2.9")
        self.tables = tables

    async def walk(self, base_oid, max_rows=4096):  # type: ignore[no-untyped-def]
        return dict(self.tables.get(base_oid, {}))

    async def walk_raw(self, base_oid, max_rows=4096):  # type: ignore[no-untyped-def]
        return dict(self.tables.get(base_oid, {}))


MAC_A = "2.252.0.0.0.5"          # 02:fc:00:00:00:05
MAC_B = "2.252.0.0.0.6"


class TestParsing:
    def test_q_bridge_learned_and_self_one_row_per_port(self):
        agent = FakeAgent({
            "1.3.6.1.2.1.17.7.1.2.2.1.2": {f"10.{MAC_A}": 3, f"20.{MAC_A}": 3, f"10.{MAC_B}": 0,
                                           "10.1.0.94.0.0.1": 3, f"10.{'.'.join(['2'] * 5)}": 3},
            "1.3.6.1.2.1.17.7.1.2.2.1.3": {f"10.{MAC_A}": 3, f"20.{MAC_A}": 3, f"10.{MAC_B}": 4,
                                           "10.1.0.94.0.0.1": 3},
            "1.3.6.1.2.1.17.1.4.1.2": {"3": 1003},
        })
        assert sorted(run(agent.bridge_table()), key=lambda e: e.mac) == [
            FdbEntry("02:fc:00:00:00:05", 1003, 10, False),
            FdbEntry("02:fc:00:00:00:06", None, 10, True),
        ], "per VLAN once, bridge port 3 is ifIndex 1003, multicast and short indexes skipped"

    def test_plain_bridge_mib_and_invalid_rows(self):
        agent = FakeAgent({
            "1.3.6.1.2.1.17.4.3.1.2": {MAC_A: 7, MAC_B: 8},
            "1.3.6.1.2.1.17.4.3.1.3": {MAC_A: 3, MAC_B: 2},
        })
        assert run(agent.bridge_table()) == [FdbEntry("02:fc:00:00:00:05", 7, None, False)]

    def test_lldp(self):
        agent = FakeAgent({
            "1.0.8802.1.1.2.1.4.1.1.4": {"0.5.1": 4, "0.6.2": 7, "0.9.3": 7},
            # 0.6.2 is a local chassis id that happens to be six bytes: not a MAC.
            "1.0.8802.1.1.2.1.4.1.1.5": {"0.5.1": bytes.fromhex("02fc00000005"), "0.6.2": b"rack-1"},
            "1.0.8802.1.1.2.1.4.1.1.9": {"0.5.1": "core", "0.6.2": "ZachSwitch"},
            "1.0.8802.1.1.2.1.4.1.1.8": {"0.5.1": "Port 24"},
        })
        assert run(agent.lldp_neighbours()) == [
            LldpNeighbour(5, "02:fc:00:00:00:05", "core", "Port 24"),
            LldpNeighbour(6, None, "ZachSwitch", None),
        ], "a neighbour with neither a MAC nor a name is nothing"

    def test_nothing_to_read(self):
        agent = FakeAgent({})
        assert run(agent.bridge_table()) == [] and run(agent.lldp_neighbours()) == []


# --------------------------------------------------------------------------
# Through the database
# --------------------------------------------------------------------------


def _config() -> Config:
    tmp = Path(tempfile.mkdtemp(prefix="spark-topology-"))
    config = Config.model_validate({
        "app": {"data_dir": str(tmp / "data"), "log_level": "WARNING"},
        "network": {"subnets": []},
    })
    config.app.data_dir.mkdir(parents=True, exist_ok=True)
    return config


# 1 firewall, 2 ZachSwitch (polled, MAC table), 3 the nas, 4 a laptop with a
# parent set by hand, 5 an ignored device, 6 an access point (polled, no
# table) with 7 a phone behind it.
@pytest.fixture
def db():
    config = _config()

    async def setup():
        D.init_engine(config)
        await D.init_db(config)
        async with D.session_scope() as s:
            s.add_all([
                Device(mac="aa:00:00:00:00:01", primary_ip="10.1.10.1", friendly_name="SPRK-MDF-FW",
                       role=DeviceRole.GATEWAY),
                Device(primary_ip="192.168.1.141", friendly_name="ZachSwitch"),
                Device(mac="aa:00:00:00:00:03", primary_ip="10.1.10.17", friendly_name="thoth"),
                Device(mac="aa:00:00:00:00:04", primary_ip="10.1.10.50", friendly_name="laptop"),
                Device(mac="aa:00:00:00:00:05", primary_ip="10.1.10.60", ignored=True),
                Device(mac="aa:00:00:00:00:06", primary_ip="192.168.1.190", friendly_name="SPRK-AP01"),
                Device(mac="aa:00:00:00:00:07", primary_ip="10.1.30.9", friendly_name="phone"),
            ])
            await s.flush()
            (await s.get(Device, 4)).parent_device_id = 1
            profile = await save_profile(s, vault_for(config), ProfileInput(
                name="lab", version="v2c", community="x"))
            switch = await add_device(s, 2, profile.id)
            ap = await add_device(s, 6, profile.id)
            s.add(SnmpInterface(snmp_device_id=switch.id, if_index=5, name="Port 5"))
            s.add(SnmpInterface(snmp_device_id=switch.id, if_index=8, name="Port 8"))
            s.add(SnmpInterface(snmp_device_id=switch.id, if_index=1, name="Port 1"))
            await topology.store(s, switch.id, lldp=[], fdb=[
                FdbEntry("aa:00:00:00:00:02", None, 1, own=True),       # its own MAC
                FdbEntry("aa:00:00:00:00:01", 1, 1),
                FdbEntry("aa:00:00:00:00:03", 5, 1),
                FdbEntry("aa:00:00:00:00:04", 5, 1),
                FdbEntry("aa:00:00:00:00:05", 5, 1),
                FdbEntry("aa:00:00:00:00:06", 8, 1),
                FdbEntry("aa:00:00:00:00:07", 8, 30),
            ])
            await topology.store(s, ap.id, fdb=[], lldp=[])
    run(setup())
    yield config
    run(D.close_engine())


async def _suggested():
    async with D.session_scope() as s:
        _d, found = await topology.suggestions(s)
        return {f.device.id: (f.parent.id if f.parent else None, f.role, f.where()) for f in found}


async def _get(ident):  # type: ignore[no-untyped-def]
    async with D.session_scope() as s:
        return await s.get(Device, ident)


class TestSuggestions:
    def test_what_is_suggested(self, db):
        assert run(_suggested()) == {
            2: (1, DeviceRole.SWITCH, "its uplink, Port 1, leads to SPRK-MDF-FW"),
            3: (2, None, "ZachSwitch, Port 5"),
            6: (2, None, "ZachSwitch, Port 8"),
            7: (6, None, "behind SPRK-AP01 on ZachSwitch, Port 8"),
        }

    def test_never_a_parent_set_by_hand_nor_an_ignored_device(self, db):
        assert 4 not in run(_suggested()) and 5 not in run(_suggested())

    def test_but_the_device_page_says_where_snmp_sees_it(self, db):
        async def seen():
            async with D.session_scope() as s:
                return (await topology.discover(s)).found[4].where()
        assert run(seen()) == "ZachSwitch, Port 5"

    def test_the_gateway_is_found_by_its_addresses_when_no_role_says(self, db):
        from spark import identity

        async def go():
            async with D.session_scope() as s:
                (await s.get(Device, 1)).role = DeviceRole.UNKNOWN
            async with D.session_scope() as s:
                # One address of its own is any device, not a router.
                await identity.store(s, 2, own=["192.168.1.190"], arp={})
            before = await _suggested()
            async with D.session_scope() as s:
                row = await add_device(s, 1, 1)
                await identity.store(s, row.id, own=["10.1.10.1", "10.1.20.1"], arp={})
            return before, await _suggested()
        before, after = run(go())
        assert before == {}, "no gateway known: nothing"
        assert after[1] == (None, DeviceRole.GATEWAY, "the gateway: the top of the map")
        assert after[3][0] == 2

    def test_accepting_one_and_all(self, db):
        async def go():
            async with D.session_scope() as s:
                _d, found = await topology.suggestions(s)
                assert await topology.accept(s, next(f for f in found if f.device.id == 2))
            first = await _get(2)
            async with D.session_scope() as s:
                _d, found = await topology.suggestions(s)
                for f in found:
                    await topology.accept(s, f)
            return first, [(await _get(i)).parent_device_id for i in (2, 3, 4, 6, 7)]
        first, placed = run(go())
        assert first.parent_device_id == 1 and first.role == DeviceRole.SWITCH
        assert placed == [1, 2, 1, 2, 6], "the laptop keeps the parent set by hand"
        assert run(_suggested()) == {}

    def test_accepting_a_role_keeps_the_parent_set_by_hand(self, db):
        async def go():
            async with D.session_scope() as s:
                (await s.get(Device, 2)).parent_device_id = 4      # by hand
            async with D.session_scope() as s:
                _d, found = await topology.suggestions(s)
                assert await topology.accept(s, next(f for f in found if f.device.id == 2))
        run(go())
        switch = run(_get(2))
        assert switch.parent_device_id == 4 and switch.role == DeviceRole.SWITCH

    def test_a_suggestion_that_would_loop_is_refused(self, db):
        async def go():
            async with D.session_scope() as s:
                (await s.get(Device, 2)).parent_device_id = 3      # set by hand, oddly
                _d, found = await topology.suggestions(s)
                return await topology.accept(s, next(f for f in found if f.device.id == 3))
        assert run(go()) is False
        assert run(_get(3)).parent_device_id is None

    def test_not_right_stays_dismissed(self, db):
        async def go():
            async with D.session_scope() as s:
                _d, found = await topology.suggestions(s)
                await topology.dismiss(s, next(f for f in found if f.device.id == 3))
        run(go())
        assert 3 not in run(_suggested()) and 6 in run(_suggested())

    def test_a_table_not_read_keeps_the_last_one(self, db):
        async def go():
            async with D.session_scope() as s:
                await topology.store(s, 1, fdb=None, lldp=None)
                return len((await s.execute(select(SnmpNeighbour))).all())
        assert run(go()) == 7

    def test_the_reason_when_nothing_can_be_worked_out(self, db):
        async def go():
            async with D.session_scope() as s:
                (await s.get(Device, 1)).role = DeviceRole.UNKNOWN
            async with D.session_scope() as s:
                no_root = (await topology.discover(s)).reason
                await s.execute(SnmpNeighbour.__table__.delete())
            async with D.session_scope() as s:
                return no_root, (await topology.discover(s)).reason
        no_root, no_tables = run(go())
        assert "Set its Role to Gateway / router" in no_root
        assert "No device on the SNMP list has answered with a MAC table" in no_tables


def test_nothing_polled_says_nothing():
    config = _config()

    async def go():
        D.init_engine(config)
        await D.init_db(config)
        async with D.session_scope() as s:
            s.add(Device(mac="aa:00:00:00:00:01", role=DeviceRole.GATEWAY))
        async with D.session_scope() as s:
            found = await topology.discover(s)
        await D.close_engine()
        return found
    found = run(go())
    assert found.reason is None and list(found.found) == [1]


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
    def test_the_map_lists_them(self, site):
        page = flat(site.get("/map").text)
        assert '<section class="card suggest-card" id="suggested">' in page
        assert "Accept all 4" in page
        assert ('<a href="/devices/7">phone</a> → <a href="/devices/6">SPRK-AP01</a>' in page)
        assert "Seen: behind SPRK-AP01 on ZachSwitch, Port 8." in page
        assert "as switch" in page

    def test_accept_all(self, site):
        response = site.post("/map/suggestions/accept-all")
        assert response.headers["location"] == "/map?accepted=4#suggested"
        page = flat(site.get("/map?accepted=4").text)
        assert "Placed 4 devices from SNMP." in page and 'id="suggested"' not in page
        assert run(_get(7)).parent_device_id == 6

    def test_the_device_page(self, site):
        page = flat(site.get("/devices/3").text)
        assert "SNMP sees it:</span> ZachSwitch, Port 5." in page
        assert "Use this: connected to ZachSwitch" in page
        response = site.post("/map/suggestions/3/accept", data={"back": "/devices/3#place"})
        assert response.headers["location"] == "/devices/3#place"
        assert run(_get(3)).parent_device_id == 2
        page = flat(site.get("/devices/3").text)
        assert "SNMP sees it:</span> ZachSwitch, Port 5." in page and "Use this" not in page

    @pytest.mark.parametrize("back, lands", [
        ("/devices/3#place", "/devices/3#place"),
        ("/map#suggested", "/map#suggested"),
        ("/devices/6#place", "/map#suggested"),
        ("https://evil.example/", "/map#suggested"),
    ])
    def test_not_right(self, site, back, lands):
        response = site.post("/map/suggestions/3/dismiss", data={"back": back})
        assert response.headers["location"] == lands
        assert 3 not in run(_suggested()) and run(_get(3)).parent_device_id is None

    def test_a_device_with_no_suggestion_is_left_alone(self, site):
        site.post("/map/suggestions/4/accept")         # parent set by hand
        site.post("/map/suggestions/5/accept")         # ignored
        site.post("/map/suggestions/99999999999999999999/accept")
        assert run(_get(4)).parent_device_id == 1 and run(_get(5)).parent_device_id is None

    def test_no_snmp_no_card(self):
        config = _config()
        with TestClient(create_app(config), follow_redirects=False) as client:
            client.post("/setup", data={"username": "admin", "password": PASSWORD,
                                        "password_confirm": PASSWORD, "timezone": "UTC"})
            page = client.get("/map").text
        assert 'id="suggested"' not in page and 'class="alert info"' not in page


def test_migration_12_from_a_version_11_database():
    from sqlalchemy import inspect, text

    async def shape(wind_back: bool):
        config = _config()
        D.init_engine(config)
        await D.init_db(config)
        if wind_back:
            async with D.session_scope() as s:
                await s.execute(text("DROP TABLE snmp_neighbour"))
                await D._set_version(s, 11)
            await D.close_engine()
            D.init_engine(config)
            await D.init_db(config)
        async with D.session_scope() as s:
            connection = await s.connection()
            columns = await connection.run_sync(
                lambda sync: sorted(c["name"] for c in inspect(sync).get_columns("snmp_neighbour")))
            version = await D._get_version(s)
            dismissed = await D.get_setting(s, topology.DISMISSED)
        await D.close_engine()
        return columns, version, dismissed

    assert run(shape(True)) == run(shape(False))
    assert run(shape(False))[1] == D.CURRENT_VERSION >= 12
    assert run(shape(False))[2] == {"pairs": []}
