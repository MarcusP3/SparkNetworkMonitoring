"""Map mode, manual or automatic, and Wipe map.

Automatic applies what SNMP reports after every read and follows a device
it placed when it moves; a place a person set -- on the device's page, by
Accept, or by changing or clearing one automatic set -- is never touched.
Wipe map clears roles, places and "Not right" answers, and nothing else.
"""

from __future__ import annotations

import asyncio
import re
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from spark import db as D
from spark import identity, topology
from spark.collectors.snmp import FdbEntry
from spark.config import Config
from spark.main import create_app
from spark.models import (
    CheckType,
    Device,
    DeviceRole,
    MapAuto,
    SnmpInterface,
    Target,
)
from spark.snmp_config import ProfileInput, add_device, save_profile
from spark.vault import vault_for

PASSWORD = "correct horse battery"
FW, SWITCH, NAS, LAPTOP, IGNORED, AP, PHONE = range(1, 8)
MAC = {i: f"aa:00:00:00:00:{i:02x}" for i in range(1, 8)}


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def _config() -> Config:
    tmp = Path(tempfile.mkdtemp(prefix="spark-mapmode-"))
    config = Config.model_validate({
        "app": {"data_dir": str(tmp / "data"), "log_level": "WARNING"},
        "network": {"subnets": []},
    })
    config.app.data_dir.mkdir(parents=True, exist_ok=True)
    return config


def switch_table(**ports: int) -> list[FdbEntry]:
    """The switch's MAC table: its own MAC, the firewall on port 1, and each
    device on the port given (defaults: nas and laptop on 5, AP and phone on 8)."""
    where = {NAS: 5, LAPTOP: 5, IGNORED: 5, AP: 8, PHONE: 8}
    where.update({{"nas": NAS, "phone": PHONE, "laptop": LAPTOP}[k]: v for k, v in ports.items()})
    return [FdbEntry(MAC[SWITCH], None, 1, own=True), FdbEntry(MAC[FW], 1, 1),
            *(FdbEntry(MAC[d], port, 1) for d, port in where.items())]


# 1 firewall (gateway), 2 switch (polled, MAC table), 3 nas, 4 laptop placed
# under the firewall by hand, 5 ignored, 6 access point (polled), 7 phone.
@pytest.fixture
def db():
    config = _config()

    async def setup():
        D.init_engine(config)
        await D.init_db(config)
        async with D.session_scope() as s:
            s.add_all([
                Device(mac=MAC[FW], primary_ip="172.16.10.1", friendly_name="edge-fw",
                       role=DeviceRole.GATEWAY),
                Device(primary_ip="192.168.1.2", friendly_name="office-switch"),
                Device(mac=MAC[NAS], primary_ip="172.16.10.17", friendly_name="truenas"),
                Device(mac=MAC[LAPTOP], primary_ip="172.16.10.50", friendly_name="laptop"),
                Device(mac=MAC[IGNORED], primary_ip="172.16.10.60", ignored=True),
                Device(mac=MAC[AP], primary_ip="192.168.1.31", friendly_name="ap-hall"),
                Device(mac=MAC[PHONE], primary_ip="172.16.30.9", friendly_name="phone"),
            ])
            await s.flush()
            (await s.get(Device, LAPTOP)).parent_device_id = FW
            profile = await save_profile(s, vault_for(config), ProfileInput(
                name="lab", version="v2c", community="x"))
            switch = await add_device(s, SWITCH, profile.id)
            await add_device(s, AP, profile.id)
            for port in (1, 5, 8):
                s.add(SnmpInterface(snmp_device_id=switch.id, if_index=port, name=f"Port {port}"))
            await topology.store(s, switch.id, lldp=[], fdb=switch_table())
    run(setup())
    yield config
    run(D.close_engine())


async def _parents() -> dict[int, int | None]:
    async with D.session_scope() as s:
        return {d.id: d.parent_device_id for d in (await s.execute(select(Device))).scalars()}


async def _do(fn, *args):  # type: ignore[no-untyped-def]
    async with D.session_scope() as s:
        return await fn(s, *args)


async def _set_parent(device_id: int, parent_id: int | None) -> None:
    async with D.session_scope() as s:
        (await s.get(Device, device_id)).parent_device_id = parent_id


async def _move(**ports: int) -> None:
    async with D.session_scope() as s:
        await topology.store(s, 1, lldp=None, fdb=switch_table(**ports))


def automatic() -> topology.Applied:
    return run(_do(topology.set_mode, "automatic"))


class TestAutomatic:
    def test_manual_is_the_default_and_places_nothing(self, db):
        assert run(_do(topology.get_mode)) == "manual"
        assert run(_do(topology.apply_automatic)) == topology.Applied()
        assert run(_parents())[NAS] is None

    def test_switching_on_places_everything_at_once(self, db):
        done = automatic()
        assert (done.placed, done.moved) == (4, 0)
        parents = run(_parents())
        assert (parents[SWITCH], parents[NAS], parents[AP], parents[PHONE]) == (FW, SWITCH, SWITCH, AP)
        assert parents[LAPTOP] == FW, "placed by hand: kept"
        assert parents[IGNORED] is None
        assert run(_do(lambda s: s.get(Device, SWITCH))).role == DeviceRole.SWITCH

    def test_a_device_it_placed_follows_when_it_moves(self, db):
        automatic()
        run(_move(nas=8))                     # the nas now sits behind the AP's port
        done = run(_do(topology.apply_automatic))
        assert (done.placed, done.moved) == (0, 1)
        assert run(_parents())[NAS] == AP

    def test_a_place_changed_or_cleared_by_hand_is_never_moved(self, db):
        automatic()
        run(_set_parent(NAS, LAPTOP))
        run(_set_parent(PHONE, None))
        run(_move(nas=8))
        run(_do(topology.apply_automatic))
        parents = run(_parents())
        assert parents[NAS] == LAPTOP, "changed by hand"
        assert parents[PHONE] is None, "cleared by hand"

    def test_accepted_in_manual_mode_is_a_persons(self, db):
        async def accept_nas():
            async with D.session_scope() as s:
                _d, found = await topology.suggestions(s)
                await topology.accept(s, next(f for f in found if f.device.id == NAS))
        run(accept_nas())
        automatic()
        run(_move(nas=8))
        run(_do(topology.apply_automatic))
        assert run(_parents())[NAS] == SWITCH

    def test_not_right_takes_it_off_and_it_stays_off(self, db):
        automatic()

        async def not_right():
            async with D.session_scope() as s:
                found = (await topology.discover(s)).found[NAS]
                return await topology.take_back(s, found)
        assert run(not_right()) is True
        run(_do(topology.apply_automatic))
        assert run(_parents())[NAS] is None
        assert run(not_right()) is False, "nothing of automatic's left to take back"

    def test_never_into_a_loop(self, db):
        run(_set_parent(SWITCH, NAS))          # odd, but set by hand
        automatic()
        parents = run(_parents())
        assert parents[SWITCH] == NAS and parents[NAS] is None

    def test_taken_back_then_placed_by_hand_is_a_persons(self, db):
        automatic()

        async def not_right():
            async with D.session_scope() as s:
                await topology.take_back(s, (await topology.discover(s)).found[NAS])
        run(not_right())
        run(_set_parent(NAS, SWITCH))          # the person puts it back themselves
        run(_move(nas=8))
        run(_do(topology.apply_automatic))
        assert run(_parents())[NAS] == SWITCH

    def test_a_merge_keeps_what_automatic_placed_below_the_duplicate(self, db):
        from spark import merge

        async def go():
            async with D.session_scope() as s:
                s.add(Device(id=8, primary_ip="192.168.1.191"))
                await s.flush()
                (await s.get(Device, PHONE)).parent_device_id = 8
                s.add(MapAuto(device_id=PHONE, parent_device_id=8))
            async with D.session_scope() as s:
                await merge.apply(s, AP, 8)
            async with D.session_scope() as s:
                return await topology.placed_automatically(s, await s.get(Device, PHONE))
        assert run(go()) is True
        assert run(_parents())[PHONE] == AP

    def test_the_read_applies_it(self, db, monkeypatch):
        async def no_network(config, row_id):  # type: ignore[no-untyped-def]
            return False
        monkeypatch.setattr(identity, "refresh_one", no_network)
        run(identity.refresh(db))
        assert run(_parents())[NAS] is None, "manual: the read changes nothing"
        run(_do(lambda s: topology.save_setting(s, "map", {"mode": "automatic"})))
        run(identity.refresh(db))
        assert run(_parents())[NAS] == SWITCH


class TestWipe:
    def test_it_clears_the_map_and_nothing_else(self, db):
        async def extra():
            async with D.session_scope() as s:
                s.add(Target(name="nas ping", check_type=CheckType.PING, address="172.16.10.17",
                             device_id=NAS))
                _d, found = await topology.suggestions(s)
                await topology.dismiss(s, next(f for f in found if f.device.id == PHONE))
        run(extra())
        counts = run(_do(topology.wipe_counts))
        assert (counts.roles, counts.parents, counts.dismissed) == (1, 1, 1)
        _counts, applied = run(_do(topology.wipe))
        assert applied == topology.Applied(), "manual: nothing rebuilt"
        assert set(run(_parents()).values()) == {None}
        assert run(_do(lambda s: s.get(Device, FW))).role == DeviceRole.UNKNOWN
        assert run(_do(topology.dismissed)) == set()
        assert len(run(_do(lambda s: s.execute(select(Target)))).all()) == 1
        assert len(run(_do(lambda s: s.execute(select(Device)))).all()) == 7

    def test_automatic_rebuilds_straight_away(self, db):
        automatic()
        # Without a gateway role it is found by its addresses.
        async def own_addresses():
            async with D.session_scope() as s:
                profile_id = 1
                row = await add_device(s, FW, profile_id)
                await identity.store(s, row.id, own=["172.16.10.1", "172.16.20.1"], arp={})
        run(own_addresses())
        _counts, applied = run(_do(topology.wipe))
        assert applied.placed == 5, "the laptop's hand-set place went with the wipe"
        assert run(_parents())[LAPTOP] == SWITCH
        assert run(_do(lambda s: s.get(Device, FW))).role == DeviceRole.GATEWAY
        assert {row.device_id for row in run(_do(lambda s: s.execute(select(MapAuto)))).scalars()} \
            == {SWITCH, NAS, LAPTOP, AP, PHONE}


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
    def test_preferences_switches_it(self, site):
        page = flat(site.get("/preferences").text)
        assert '<section class="card" id="map">' in page
        assert '<option value="manual" selected>Manual: SNMP suggests, you accept</option>' in page
        response = site.post("/preferences", data={"map_mode": "automatic"})
        assert response.headers["location"] == "/preferences?saved=map&placed=4#map"
        page = flat(site.get("/preferences?saved=map&placed=4").text)
        assert "Saved. The network map is automatic: 4 devices placed from SNMP just now." in page
        assert site.post("/preferences", data={"map_mode": "sometimes"}).status_code == 400

    def test_the_map_and_device_pages_in_automatic_mode(self, site):
        site.post("/preferences", data={"map_mode": "automatic"})
        page = flat(site.get("/map").text)
        assert "<strong>Automatic map.</strong>" in page and 'id="suggested"' not in page
        page = flat(site.get(f"/devices/{NAS}").text)
        assert "Placed there automatically." in page and "Use this" not in page
        site.post(f"/map/suggestions/{NAS}/dismiss", data={"back": f"/devices/{NAS}#place"})
        assert run(_parents())[NAS] is None
        assert "Placed there automatically." not in site.get(f"/devices/{NAS}").text

    def test_wipe_asks_first_then_wipes(self, site):
        page = flat(site.get("/preferences/wipe-map").text)
        assert "1 device loses its role" in page and "1 device is taken off the map" in page
        # The firewall is the gateway by its role alone: say so before it goes.
        assert "SPARK knows edge-fw is your gateway only from its role." in page
        assert '<form method="post" action="/preferences/wipe-map">' in page
        assert run(_parents())[LAPTOP] == FW, "asking changed nothing"
        response = site.post("/preferences/wipe-map")
        assert response.headers["location"] == "/map?wiped=0"
        assert "Map wiped: every role, place and “Not right” is cleared." in site.get("/map?wiped=0").text
        assert run(_parents())[LAPTOP] is None

    def test_no_warning_when_its_addresses_give_the_gateway_away(self, site):
        async def own_addresses():
            async with D.session_scope() as s:
                row = await add_device(s, FW, 1)
                await identity.store(s, row.id, own=["172.16.10.1", "172.16.20.1"], arp={})
        run(own_addresses())
        page = flat(site.get("/preferences/wipe-map").text)
        assert "only from its role" not in page
        site.post("/preferences", data={"map_mode": "automatic"})      # gives the switch a role
        assert "2 devices lose their roles" in flat(site.get("/preferences/wipe-map").text)

    def test_a_hand_edit_on_the_device_page_takes_it_from_automatic(self, site):
        site.post("/preferences", data={"map_mode": "automatic"})
        site.post(f"/devices/{NAS}/place", data={"role": "host", "parent_id": str(FW)})
        run(_move(nas=8))
        run(_do(topology.apply_automatic))
        assert run(_parents())[NAS] == FW
        assert "Placed there automatically." not in site.get(f"/devices/{NAS}").text


class TestSetup:
    def _setup(self, **extra: str):  # type: ignore[no-untyped-def]
        config = _config()
        with TestClient(create_app(config), follow_redirects=False) as client:
            form = client.get("/setup").text
            response = client.post("/setup", data={
                "username": "admin", "password": PASSWORD, "password_confirm": PASSWORD,
                "timezone": "UTC", **extra})
            mode = run(_do(topology.get_mode))
        return form, response, mode

    def test_setup_asks(self):
        form, response, mode = self._setup(map_mode="automatic")
        assert re.search(r'<select name="map_mode">\s*<option value="manual" selected>', form)
        assert response.status_code in (302, 303) and mode == "automatic"

    def test_manual_unless_chosen_and_nonsense_refused(self):
        assert self._setup()[2] == "manual"
        _form, response, _mode = self._setup(map_mode="both")
        assert response.status_code == 400
        assert "Choose Manual or Automatic for the network map." in response.text
        assert re.search(r'<option value="manual" selected>', response.text)


def test_migration_13_from_a_version_12_database():
    from sqlalchemy import inspect, text

    async def shape(wind_back: bool):
        config = _config()
        D.init_engine(config)
        await D.init_db(config)
        if wind_back:
            async with D.session_scope() as s:
                await s.execute(text("DROP TABLE map_auto"))
                await D._set_version(s, 12)
            await D.close_engine()
            D.init_engine(config)
            await D.init_db(config)
        async with D.session_scope() as s:
            connection = await s.connection()
            columns = await connection.run_sync(
                lambda sync: sorted(c["name"] for c in inspect(sync).get_columns("map_auto")))
            version = await D._get_version(s)
            mode = await topology.get_mode(s)
        await D.close_engine()
        return columns, version, mode

    assert run(shape(True)) == run(shape(False))
    assert run(shape(False))[1:] == (D.CURRENT_VERSION, "manual")
    assert D.CURRENT_VERSION >= 13
