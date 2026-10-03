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
        client.post("/setup", data={"setup_code": client.app.state.setup_code, "username": "admin", "password": PASSWORD,
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
        assert 'data-find="nas 10.0.0.20 aa:bb:cc:00:00:03 server smb 445 plex 32400"' in page, \
            "ports are found by the Find box even while folded to a count"

    def test_the_toolbar(self, site):
        page = " ".join(site.get("/map").text.split())
        assert page.index('id="map-toolbar"') < page.index('<div id="live">'), \
            "outside the live region, so a refresh never takes the text being typed"
        assert '<div class="map-toolbar js-only" id="map-toolbar">' in page
        assert '<input type="search" id="map-find" placeholder="Find a device: name, address, port"' in page
        assert '<span>Problems only (<span id="map-problem-count">0</span>)</span>' in page
        assert ('<button type="button" data-mode="infra" aria-pressed="true">Infrastructure</button> '
                '<button type="button" data-mode="all" aria-pressed="false">Everything</button>') in page
        assert "'spark.map.mode'" in page and "'spark.map.open'" in page

    def test_branches_fold_and_the_page_remembers_which(self, site):
        page = " ".join(site.get("/map").text.split())
        assert '<li data-branch="1">' in page and '<ul id="branch-1">' in page
        assert re.search(r'<button type="button" class="branch-toggle js-only" aria-expanded="true"'
                         r' aria-controls="branch-1"[^>]*> <span class="branch-count">2 below</span>', page)
        assert page.count('class="branch-toggle') == 2, "gateway and switch; not the nas"
        assert 'data-fold="all">Collapse all' in page and 'data-fold="none">Expand all' in page
        assert "'spark.map.folded'" in page and "addEventListener('spark:live-updated', apply)" in page
        assert "new CustomEvent('spark:live-updated')" in page, "the live refresh says when it swapped"

    def test_unwatched_ports_are_a_count_linking_to_services(self, site):
        page = " ".join(site.get("/map").text.split())
        tree = page[page.index('<ul class="tree">'):]
        assert re.search(r'<a class="more-ports" href="/services\?q=10.0.0.20" title="[^"]*">2 ports</a>', tree)

        async def watch_plex():
            async with D.session_scope() as s:
                s.add(Target(name="plex web", check_type=CheckType.TCP, address="10.0.0.20:32400",
                             device_id=3, service_id=1))
        run(watch_plex())
        page = " ".join(site.get("/map").text.split())
        tree = page[page.index('<ul class="tree">'):]
        row = tree[tree.index('<li data-branch="3">'):]
        assert row.index("Plex") < row.index('class="more-ports"'), "the watched one stays in view"
        assert re.search(r'<a class="more-ports" href="/services\?q=10.0.0.20" title="[^"]*">\+1 port</a>', row)

    def test_end_devices_are_tiles_under_their_parent(self, site):
        async def place():
            async with D.session_scope() as s:
                laptop = await s.get(Device, 4)
                laptop.parent_device_id, laptop.role = 2, DeviceRole.CLIENT
                s.add(Target(name="laptop ping", check_type=CheckType.PING, address="10.0.0.50",
                             device_id=4, status="down"))
        run(place())
        page = " ".join(site.get("/map").text.split())
        tree = page[page.index('<ul class="tree">'):]
        assert '<li class="leaf-row" data-leaves="2">' in tree
        assert re.search(r'<button type="button" class="leaf-toggle js-only" aria-expanded="false" '
                         r'aria-controls="leaves-2"> 1 device <span class="leaf-trouble">· 1 with a problem</span>', tree)
        assert re.search(r'<a href="/devices/4" class="chip is-bad" data-find="laptop 10.0.0.50[^"]*" data-problem '
                         r'title="laptop — 10.0.0.50 — down — 1 port">', tree)
        assert '<span class="chip-name">laptop</span> <span class="chip-ip">50</span>' in tree
        assert tree.index('data-leaves="2"') < tree.index('<li data-branch="3">'), "tiles, then the branches"
        assert '<span>Problems only (<span id="map-problem-count">1</span>)</span>' in page

    def test_a_watched_port_down_is_a_problem(self, site):
        async def watch():
            async with D.session_scope() as s:
                s.add(Target(name="plex web", check_type=CheckType.TCP, address="10.0.0.20:32400",
                             device_id=3, service_id=1, status="down"))
        run(watch())
        page = " ".join(site.get("/map").text.split())
        assert re.search(r'<div class="map-node" data-find="nas [^"]*" data-problem>', page)

    def test_nas_ups_and_camera(self, site):
        page = " ".join(site.get("/devices/4").text.split())
        assert re.search(r'<option value="host" ?>Server</option> <option value="nas" ?>NAS</option> '
                         r'<option value="ups" ?>UPS</option> <option value="client" ?>Client device</option> '
                         r'<option value="camera" ?>Camera</option>', page)
        for device_id, role, parent in ((3, "nas", "2"), (4, "camera", "2")):
            response = site.post(f"/devices/{device_id}/place", data={
                "role": role, "parent_id": parent, "back": f"/devices/{device_id}"})
            assert response.status_code == 303
        assert _device(3).role == DeviceRole.NAS and _device(4).role == DeviceRole.CAMERA

        async def ups():
            async with D.session_scope() as s:
                s.add(Device(mac="aa:bb:cc:00:00:10", primary_ip="10.0.0.30", friendly_name="ups",
                             role=DeviceRole.UPS, parent_device_id=2, last_seen=utcnow()))
        run(ups())
        page = " ".join(site.get("/map").text.split())
        tree = page[page.index('<ul class="tree">'):]
        # A NAS and a UPS are rows; a camera is a tile.
        assert re.search(r'<li data-branch="3"> <div class="map-node" data-find="nas [^"]* nas smb', tree)
        assert re.search(r'<li data-branch="6"> <div class="map-node" data-find="ups 10.0.0.30 [^"]* ups"', tree)
        assert re.search(r'<a href="/devices/4" class="chip" data-find="laptop 10.0.0.50 [^"]* camera http-alt 8080"', tree)
        assert '<span class="muted small">NAS</span>' in tree and '<span class="muted small">UPS</span>' in tree
        assert tree.index('data-branch="3"') < tree.index('data-branch="6"'), "NAS before UPS"

    def test_the_tiles(self, site):
        """SPARK 2's tiles. Outside the live region, beside the toolbar, so the
        page script recounts them from the tree: the role classes it counts
        by have to be on the page."""
        page = " ".join(site.get("/map").text.split())
        tiles = dict(re.findall(r'<span class="stat-value" data-tile="(\w+)">(\d+)</span>', page))
        assert tiles == {"placed": "3", "gear": "2", "servers": "1", "problems": "0",
                         "unplaced": "1", "hints": "0"}
        assert page.index('data-tile="placed"') < page.index('<div id="live">')
        assert 'class="map-icon role-gateway"' in page and 'class="map-icon role-switch"' in page
        assert "querySelectorAll('[data-tile]')" in page

    def test_the_targets_filter_chips_leave_the_map_tiles_alone(self):
        """The Targets page's status chips were styled as a bare .chip, which
        is also the map's device tile: it turned the map's grid of tiles into
        a row of round buttons. Scoped to their toolbar now."""
        from pathlib import Path
        import spark
        css = (Path(spark.__file__).parent / "static" / "app.css").read_text()
        targets = css[css.index("SPARK 2: Targets"):css.index("SPARK 2: Devices")]
        assert not re.search(r"^\.chips? ", targets, re.M), "a bare .chip rule in the Targets section"

    def test_not_placed_is_one_line_of_tiles(self, site):
        page = " ".join(site.get("/map").text.split())
        assert '<details class="card strip unplaced" data-strip="unplaced">' in page
        strip = page[page.index('data-strip="unplaced"'):]
        assert '<span class="pill neutral">1</span> <strong>Not placed yet</strong>' in strip
        assert '<a href="/devices/4" class="chip"' in strip

    def test_searching_services(self, site):
        page = site.get("/services?q=8080").text
        table = page[page.index('<th data-sort="name"'):page.index("</table>")]
        assert "http-alt" in table and "Plex" not in table
        assert "Nothing matches “zzz”." in site.get("/services?q=zzz").text
        page = site.get("/services?q=<script>alert(1)</script>").text
        assert "<script>alert(1)</script>" not in page

    def test_services_is_its_own_tab(self, site):
        page = " ".join(site.get("/services").text.split())
        assert '<a href="/services" class="active">Services</a>' in page
        assert '<span class="nav-full">Network map</span><span class="nav-short">Map</span>' in page
        assert "<h1>Services</h1>" in page and '<button type="button">Service</button></th>' in page
        assert '<form method="get" action="/services" class="row-form service-search">' in page
        assert "Plex" in page and "3 services" in page
        page = " ".join(site.get("/services?q=8080").text.split())
        assert "1 of 3 match" in page and '<a href="/services" class="btn-quiet">Clear</a>' in page

    def test_the_services_tiles_count_everything(self, site):
        page = " ".join(site.get("/services?q=8080").text.split())
        tiles = dict(re.findall(r'<span class="stat-name">([^<]+)</span></span> ?<span class="stat-value">([^<]+)</span>', page))
        assert tiles == {"Services": "3", "On devices": "2", "Watched": "0", "Worth a look": "1",
                         "Distinct ports": "3", "Last port scan": "—"}, "whatever the search"

    def test_worth_a_second_look_and_common_ports(self, site):
        page = " ".join(site.get("/services").text.split())
        look = page[page.index("<h2>Worth a second look</h2>"):page.index("<h2>Most common ports</h2>")]
        assert ">nas</a></strong> <code class=\"small\">SMB 445</code>" in look
        assert "SMB has a long history of wormable flaws." in look and "Plex" not in look
        assert '<a href="/services?q=445"><code>445</code><span>SMB</span><span class="muted small">1 device</span></a>' in page
        assert 'class="svc-flag" title="SMB has a long history of wormable flaws"' in page

    def test_the_chips_narrow_and_keep_the_search(self, site):
        page = " ".join(site.get("/services?q=nas").text.split())
        assert '<a class="chip k-warn" href="/services?q=nas&amp;show=look" >' in page \
            or '<a class="chip k-warn" href="/services?q=nas&amp;show=look">' in page
        page = " ".join(site.get("/services?show=look").text.split())
        table = page[page.index('<tbody>'):page.index("</table>")]
        assert "SMB" in table and "Plex" not in table and "http-alt" not in table
        page = " ".join(site.get("/services?show=unwatched").text.split())
        assert page.count("<tr data-name=") == 3
        page = " ".join(site.get("/services?show=watched").text.split())
        assert "<tr data-name=" not in page and "Show every service" in page
        assert page.count('aria-current="true"') == 1
        assert "<tr data-name=" in site.get("/services?show=nonsense").text, "unknown shows all"

    def test_the_subnet_filter(self, site):
        site.post("/settings/subnets", data={"cidr": "10.0.0.0/27", "name": "Low", "attached": "1", "enabled": "1"})
        page = " ".join(site.get("/services").text.split())
        assert '<select name="subnet" class="auto-submit">' in page
        assert page.count("<tr data-name=") == 3
        low = re.search(r'<option value="(\d+)" >Low · 10.0.0.0/27</option>', page)
        assert low, "the subnet is offered"
        page = " ".join(site.get(f"/services?subnet={low.group(1)}").text.split())
        table = page[page.index("<tbody>"):page.index("</table>")]
        assert "SMB" in table and "Plex" in table and "http-alt" not in table, "10.0.0.50 is outside a /27"
        assert page.count("<tr data-name=") == 2

    def test_scan_ports_comes_back_to_the_page_it_was_pressed_on(self, site, monkeypatch):
        from spark import scheduler as scheduler_module
        monkeypatch.setattr(scheduler_module, "trigger_port_scan_now", lambda: None)
        assert site.post("/devices/scan-ports", data={"back": "/services?q=nas"}).headers["location"] \
            == "/services?q=nas"
        assert site.post("/devices/scan-ports", data={"back": "/devices?subnet=1"}).headers["location"] \
            == "/devices?subnet=1"
        assert site.post("/devices/scan-ports", data={"back": "https://evil.example/"}).headers["location"] \
            == "/devices"
        assert site.post("/devices/scan-ports").headers["location"] == "/devices"

    def test_the_map_no_longer_lists_services(self, site):
        page = " ".join(site.get("/map").text.split())
        assert "<h1>Network map</h1>" in page and ">Service</button></th>" not in page
        assert 'class="row-form service-search"' not in page
        assert '<a href="/services">Services</a>' in page, "the map points at the new tab"

    def test_an_old_search_link_goes_to_services(self, site):
        response = site.get("/map?q=plex%20box")
        assert response.status_code == 303
        assert response.headers["location"] == "/services?q=plex+box"
        assert site.get("/map?q=%20").status_code == 200, "a blank search is just the map"

    def test_the_dashboard_tile_opens_services(self, site):
        page = " ".join(site.get("/").text.split())
        assert '<a class="stat s-svc" href="/services"' in page

    def test_place_a_device_from_its_page(self, site):
        page = site.get("/devices/4").text
        assert "On the network map" in page and 'name="parent_id"' in page
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
