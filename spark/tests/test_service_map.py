"""The service map: devices in their declared places, and alerts that follow them.

Two jobs, both tested here. The map must show every device -- placed ones in
the tree, the rest in "Not placed yet", never silently dropped -- with a
status from the best source there is. And a device's place must quiet its
alerts while anything above it is down, which is the point of placing it.
"""

from __future__ import annotations

import asyncio
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from spark import alerts, hierarchy, servicemap
from spark import db as D
from spark.checks.base import CheckOutcome
from spark.collectors.base import DeviceHealth
from spark.config import Config
from spark.main import create_app
from spark.models import (
    CheckType,
    Device,
    DeviceRole,
    HealthStatus,
    Incident,
    Notification,
    Service,
    SnmpDevice,
    Target,
    utcnow,
)
from spark.snmp_config import ProfileInput, save_profile
from spark.vault import vault_for

PASSWORD = "correct horse battery"
HOOK = "https://discord.com/api/webhooks/123456789012345678/abcDEF-secret-token_xyz"


def _config() -> Config:
    tmp = Path(tempfile.mkdtemp(prefix="spark-map-"))
    config = Config.model_validate({
        "app": {"data_dir": str(tmp / "data"), "log_level": "WARNING"},
        "network": {"subnets": []},
    })
    config.app.data_dir.mkdir(parents=True, exist_ok=True)
    return config


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


# gateway(1) <- core-switch(2) <- nas(3); laptop(4) unplaced; old-box(5) ignored.
@pytest.fixture
def db():
    config = _config()

    async def setup():
        D.init_engine(config)
        await D.init_db(config)
        async with D.session_scope() as s:
            for n, (name, ip, role, parent, ignored) in enumerate([
                ("gateway", "10.0.0.1", DeviceRole.GATEWAY, None, False),
                ("core-switch", "10.0.0.2", DeviceRole.SWITCH, 1, False),
                ("nas", "10.0.0.20", DeviceRole.HOST, 2, False),
                ("laptop", "10.0.0.50", DeviceRole.UNKNOWN, None, False),
                ("old-box", "10.0.0.99", DeviceRole.UNKNOWN, None, True),
            ], start=1):
                s.add(Device(mac=f"aa:bb:cc:00:00:{n:02x}", primary_ip=ip, friendly_name=name,
                             role=role, ignored=ignored, last_seen=utcnow(), acknowledged=True))
                await s.flush()
                if parent:
                    (await s.get(Device, n)).parent_device_id = parent
            for device_id, name, address in [(1, "gateway ping", "10.0.0.1"),
                                             (3, "nas ping", "10.0.0.20")]:
                s.add(Target(name=name, check_type=CheckType.PING, address=address,
                             device_id=device_id, failure_threshold=1, recovery_threshold=1))
            s.add(Service(device_id=3, port=32400, name="Plex", state="open"))
            s.add(Service(device_id=3, port=445, name="SMB", state="open"))
            s.add(Service(device_id=4, port=8080, name="http-alt", state="open"))
            await alerts.set_webhook(s, vault_for(config), HOOK)
    run(setup())
    yield config
    run(D.close_engine())


async def _build(query: str = "") -> servicemap.ServiceMap:
    async with D.session_scope() as s:
        return await servicemap.build(s, query=query)


def names(nodes) -> list[str]:  # type: ignore[no-untyped-def]
    return [n.device.display_name for n in nodes]


class TestTheMap:
    def test_placed_devices_form_the_tree_and_the_rest_are_listed_apart(self, db):
        m = run(_build())
        assert names(m.roots) == ["gateway"]
        assert names(m.roots[0].children) == ["core-switch"]
        assert names(m.roots[0].children[0].children) == ["nas"]
        assert names(m.unplaced) == ["laptop"], "unplaced is shown; ignored is not"
        assert m.roots[0].count == 3

    def test_a_parent_alone_is_enough_to_be_placed(self, db):
        """No role set on either: the laptop hangs off the nas, and a device
        with only a child still shows at the top."""
        async def place():
            async with D.session_scope() as s:
                (await s.get(Device, 4)).parent_device_id = 3
                s.add(Device(mac="aa:bb:cc:00:00:20", primary_ip="10.0.0.60",
                             friendly_name="phone", last_seen=utcnow()))
                await s.flush()
                s.add(Device(mac="aa:bb:cc:00:00:21", primary_ip="10.0.0.61", friendly_name="watch",
                             parent_device_id=6, last_seen=utcnow()))
        run(place())
        m = run(_build())
        assert names(m.roots[0].children[0].children[0].children) == ["laptop"]
        assert "phone" in names(m.roots) and m.unplaced == []

    def test_status_comes_from_the_best_source(self, db):
        async def seed():
            async with D.session_scope() as s:
                (await s.get(Target, 1)).status = HealthStatus.DOWN
                (await s.get(Target, 2)).status = HealthStatus.UP
                profile = await save_profile(s, vault_for(db), ProfileInput(
                    name="lab", version="v2c", community="x"))
                s.add(SnmpDevice(device_id=2, profile_id=profile.id, enabled=True))
        run(seed())
        from spark.snmp_poll import record_poll

        async def poll():
            async with D.session_scope() as s:
                await record_poll(s, 1, DeviceHealth(reachable=True, uptime_seconds=5.0),
                                  [], utcnow(), interval=60)
        run(poll())
        m = run(_build())
        gateway = m.roots[0]
        switch = gateway.children[0]
        nas = switch.children[0]
        assert (gateway.state.label, gateway.state.kind) == ("down", "bad")
        assert (switch.state.label, switch.state.kind, switch.state.source) == ("up", "ok", "SNMP polling")
        assert (nas.state.label, nas.state.kind) == ("up", "ok")
        assert (m.unplaced[0].state.label, m.unplaced[0].state.kind) == ("not watched", "neutral")

    def test_the_worst_target_decides_and_paused_is_paused(self):
        t = lambda status, enabled=True: Target(name="t", status=status, enabled=enabled)  # noqa: E731
        state = servicemap.device_state([t(HealthStatus.UP), t(HealthStatus.DEGRADED)], None)
        assert (state.label, state.kind) == ("degraded", "warn")
        state = servicemap.device_state([t(HealthStatus.DOWN, enabled=False)], None)
        assert state.label == "paused"
        assert servicemap.device_state([t(HealthStatus.UNKNOWN)], None).label == "checking"

    def test_siblings_sort_infrastructure_first(self, db):
        async def add():
            async with D.session_scope() as s:
                s.add(Device(mac="aa:bb:cc:00:00:10", primary_ip="10.0.0.3", friendly_name="aaa-server",
                             role=DeviceRole.HOST, parent_device_id=2, last_seen=utcnow()))
                s.add(Device(mac="aa:bb:cc:00:00:11", primary_ip="10.0.0.4", friendly_name="zz-ap",
                             role=DeviceRole.ACCESS_POINT, parent_device_id=2, last_seen=utcnow()))
        run(add())
        assert names(run(_build()).roots[0].children[0].children) == ["zz-ap", "aaa-server", "nas"]

    def test_a_hand_made_loop_does_not_hang_or_lose_devices(self, db):
        async def loop():
            async with D.session_scope() as s:
                (await s.get(Device, 1)).parent_device_id = 3
        run(loop())
        m = run(_build())
        shown = set()

        def walk(nodes):  # type: ignore[no-untyped-def]
            for n in nodes:
                shown.add(n.device.display_name)
                walk(n.children)
        walk(m.roots)
        walk(m.unplaced)
        assert {"gateway", "core-switch", "nas", "laptop"} <= shown

    def test_the_service_list_filters(self, db):
        assert [r["service"].port for r in run(_build()).services] == [445, 8080, 32400]
        assert [r["service"].name for r in run(_build("plex")).services] == ["Plex"]
        assert [r["service"].port for r in run(_build("8080")).services] == [8080]
        assert [r["service"].port for r in run(_build("nas")).services] == [445, 32400]
        assert [r["service"].port for r in run(_build("10.0.0.50")).services] == [8080]
        assert run(_build("nothing-like-this")).services == []


class TestHierarchy:
    def test_can_parent_refuses_itself_and_anything_below(self):
        parents = {1: None, 2: 1, 3: 2, 4: None}
        assert hierarchy.can_parent(parents, 3, 1)
        assert not hierarchy.can_parent(parents, 1, 1)
        assert not hierarchy.can_parent(parents, 1, 3), "3 is below 1"
        assert hierarchy.descendants(parents, 1) == {2, 3}

    def test_ancestors_stop_at_a_loop(self, db):
        async def go():
            async with D.session_scope() as s:
                (await s.get(Device, 1)).parent_device_id = 3
            async with D.session_scope() as s:
                return await hierarchy.ancestors(s, 3)
        assert run(go()) == [2, 1]


# --------------------------------------------------------------------------
# Alerts follow the map
# --------------------------------------------------------------------------


def run_checks(monkeypatch, target_id: int, *results: bool) -> None:
    from spark.engine import runner

    outcomes = iter(results)

    async def fake(check_type, spec):  # type: ignore[no-untyped-def]
        return CheckOutcome.up(1.0) if next(outcomes) else CheckOutcome.down("no reply")

    monkeypatch.setattr(runner, "run_check", fake)
    for _ in results:
        run(runner.run_target(target_id))


def sent() -> list[str]:
    async def go():
        async with D.session_scope() as s:
            return [n.subject for n in (await s.execute(select(Notification).order_by(Notification.id))).scalars()]
    return run(go())


class TestSuppression:
    def test_a_device_below_a_down_device_does_not_alert(self, db, monkeypatch):
        run_checks(monkeypatch, 1, False)          # gateway down
        run_checks(monkeypatch, 2, False, True)    # nas, two levels below: silent both ways
        assert sent() == ["gateway ping is down"]

        async def incident():
            async with D.session_scope() as s:
                return await s.scalar(select(Incident).where(Incident.target_id == 2))
        assert run(incident()).suppressed_by_dependency, "still recorded, just flagged"

    def test_with_everything_above_up_it_alerts_as_usual(self, db, monkeypatch):
        run_checks(monkeypatch, 2, False)
        assert sent() == ["nas ping is down"]

    def test_an_unplaced_device_is_unaffected(self, db, monkeypatch):
        async def watch_laptop():
            async with D.session_scope() as s:
                s.add(Target(name="laptop ping", check_type=CheckType.PING, address="10.0.0.50",
                             device_id=4, failure_threshold=1))
        run(watch_laptop())
        run_checks(monkeypatch, 1, False)
        run_checks(monkeypatch, 3, False)
        assert sent() == ["gateway ping is down", "laptop ping is down"]

    def test_snmp_silence_below_a_down_device_is_not_alerted(self, db, monkeypatch):
        from spark.snmp_poll import record_poll

        async def setup():
            async with D.session_scope() as s:
                profile = await save_profile(s, vault_for(db), ProfileInput(
                    name="lab", version="v2c", community="x"))
                s.add(SnmpDevice(device_id=3, profile_id=profile.id, enabled=True))
        run(setup())
        run_checks(monkeypatch, 1, False)
        t0 = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)

        async def poll(minute, answered):  # type: ignore[no-untyped-def]
            health = (DeviceHealth(reachable=True, uptime_seconds=1000 + minute * 60) if answered
                      else DeviceHealth(reachable=False, error="timeout"))
            async with D.session_scope() as s:
                await record_poll(s, 1, health, [], t0 + timedelta(minutes=minute), interval=60)
        run(poll(0, True))
        for minute in range(1, 6):
            run(poll(minute, False))
        assert sent() == ["gateway ping is down"]


# --------------------------------------------------------------------------
# The pages
# --------------------------------------------------------------------------


@pytest.fixture
def site(db):
    with TestClient(create_app(db), follow_redirects=False) as client:
        client.post("/setup", data={"username": "admin", "password": PASSWORD,
                                    "password_confirm": PASSWORD, "timezone": "UTC"})
        yield client


def _device(device_id: int) -> Device:
    async def go():
        async with D.session_scope() as s:
            return await s.get(Device, device_id)
    return run(go())


class TestPages:
    def test_the_map_page(self, site):
        page = site.get("/map").text
        assert '<a href="/map" class="active">' in page, "the nav link is live"
        assert re.search(r'<ul class="tree">.*gateway.*core-switch.*nas', page, re.S)
        assert "Not placed yet" in page and "laptop" in page and "old-box" not in page
        assert "Plex" in page and ":32400" in page

    def test_searching_services(self, site):
        page = site.get("/map?q=8080").text
        table = page[page.index("<th>Service</th>"):]
        assert "http-alt" in table and "Plex" not in table
        assert "Nothing matches “zzz”." in site.get("/map?q=zzz").text
        page = site.get("/map?q=<script>alert(1)</script>").text
        assert "<script>alert(1)</script>" not in page

    def test_place_a_device_from_its_page(self, site):
        page = site.get("/devices/4").text
        assert "On the service map" in page and 'name="parent_id"' in page
        response = site.post("/devices/4/place", data={"role": "client", "parent_id": "2",
                                                       "back": "/devices/4?range=24h"})
        assert response.headers["location"] == "/devices/4?range=24h"
        laptop = _device(4)
        assert laptop.role == DeviceRole.CLIENT and laptop.parent_device_id == 2
        site.post("/devices/4/place", data={"role": "client", "parent_id": ""})
        assert _device(4).parent_device_id is None, "empty means top of the map"

    def test_the_choices_leave_out_itself_and_anything_below(self, site):
        page = site.get("/devices/1").text
        select = page[page.index('name="parent_id"'):page.index("</select>", page.index('name="parent_id"'))]
        values = re.findall(r'<option value="(\d+)"', select)
        assert "1" not in values and "2" not in values and "3" not in values and "4" in values

    @pytest.mark.parametrize("data", [
        {"role": "switch", "parent_id": "3"},     # below it: a loop
        {"role": "switch", "parent_id": "1"},     # itself
        {"role": "switch", "parent_id": "5"},     # ignored
        {"role": "switch", "parent_id": "999"},   # does not exist
        {"role": "switch", "parent_id": "abc"},
        {"role": "overlord", "parent_id": ""},    # not a role
    ])
    def test_nothing_invalid_is_saved(self, site, data):
        site.post("/devices/1/place", data=data)
        gateway = _device(1)
        assert gateway.role == DeviceRole.GATEWAY and gateway.parent_device_id is None
