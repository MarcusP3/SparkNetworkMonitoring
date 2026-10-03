"""The dashboard's stat tiles and network overview.

SPARK 2 gives every tile one colour for good (cyan, green, amber, red, pink, as
the console design draws them). What says whether something is wrong now is
the number: it turns amber or red only above zero. That keeps the lesson of
the first version, where a permanent red icon sat above a neutral 0 and the
tile and the number disagreed about whether anything was wrong: the signal
that changes is the one that means something, and at zero nothing does.
"""

from __future__ import annotations

import asyncio
import re
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from spark import db as D
from spark.config import Config
from spark.main import create_app
from spark.models import CheckType, Device, HealthStatus, Incident, Subnet, Target, utcnow

PASSWORD = "correct horse battery"


def make_client(status: HealthStatus | None, open_incident: bool, routed: bool = False,
                devices: tuple[str, ...] = ()):
    tmp = Path(tempfile.mkdtemp(prefix="spark-dash-"))
    cfg = Config.model_validate({
        "app": {"data_dir": str(tmp / "data"), "port": 9707, "log_level": "WARNING"},
        "network": {"subnets": []},
    })
    cfg.app.data_dir.mkdir(parents=True, exist_ok=True)

    async def seed():
        D.init_engine(cfg)
        await D.init_db(cfg)
        if routed:
            async with D.session_scope() as s:
                s.add(Subnet(cidr="172.16.20.0/24", name="IoT", attached=False, enabled=True))
        if devices:
            async with D.session_scope() as s:
                for n, ip in enumerate(devices):
                    s.add(Device(hostname=f"host-{n}", primary_ip=ip))
        if status is not None:
            async with D.session_scope() as s:
                s.add(Target(name="gw", check_type=CheckType.PING,
                             address="172.16.10.1", status=status, enabled=True))
            if open_incident:
                async with D.session_scope() as s:
                    s.add(Incident(target_id=1, opened_at=utcnow(), cause="timeout"))
        await D.close_engine()

    asyncio.run(seed())
    return TestClient(create_app(cfg), follow_redirects=False)


@pytest.fixture
def page():
    """Render the dashboard for a given state and return its HTML."""
    clients = []

    def render(status=None, open_incident=False, routed=False, devices=()) -> str:
        client = make_client(status, open_incident, routed, devices)
        clients.append(client)
        client.__enter__()
        client.post("/setup", data={"setup_code": client.app.state.setup_code, "username": "admin", "password": PASSWORD,
                                    "password_confirm": PASSWORD})
        return client.get("/").text

    yield render
    for client in clients:
        client.__exit__(None, None, None)


def stat_card(html: str, name: str) -> tuple[set[str], str]:
    """The classes on the stat card labelled `name`, and its number."""
    match = re.search(
        r'<(?:a|div) class="(stat [^"]*)"[^>]*>(?:(?!class="stat ).)*?'
        r'<span class="stat-name">' + re.escape(name) + r'</span>\s*</span>\s*'
        r'<span class="stat-value">([^<]*)</span>',
        html, re.S,
    )
    assert match, f"no stat card named {name!r}"
    return set(match.group(1).split()), match.group(2)


COLOURS = {"Devices": "s-dev", "Services": "s-svc", "Monitored": "s-mon",
           "Degraded": "s-deg", "Down": "s-down", "Open incidents": "s-inc"}
ALARMS = {"Degraded": "is-warn", "Down": "is-bad", "Open incidents": "is-bad"}


class TestTileColours:
    @pytest.mark.parametrize("status, incident", [
        (None, False), (HealthStatus.UP, False),
        (HealthStatus.DEGRADED, False), (HealthStatus.DOWN, True)])
    def test_each_tile_keeps_its_colour_whatever_the_state(self, page, status, incident):
        html = page(status, open_incident=incident)
        for name, colour in COLOURS.items():
            assert colour in stat_card(html, name)[0], f"{name} at {status}"


class TestNumbers:
    def test_plain_on_a_healthy_network(self, page):
        html = page(HealthStatus.UP)
        for name in ALARMS:
            classes, value = stat_card(html, name)
            assert value == "0" and not classes & {"is-warn", "is-bad"}, name

    def test_plain_with_nothing_monitored_at_all(self, page):
        html = page(None)
        for name in ALARMS:
            assert not stat_card(html, name)[0] & {"is-warn", "is-bad"}, name

    def test_degraded_turns_amber(self, page):
        html = page(HealthStatus.DEGRADED)
        assert "is-warn" in stat_card(html, "Degraded")[0]
        assert "is-bad" not in stat_card(html, "Down")[0], "only the one that is true"

    def test_down_and_its_incident_turn_red(self, page):
        html = page(HealthStatus.DOWN, open_incident=True)
        assert "is-bad" in stat_card(html, "Down")[0]
        assert "is-bad" in stat_card(html, "Open incidents")[0]
        assert "is-warn" not in stat_card(html, "Degraded")[0]

    def test_number_and_alarm_agree(self, page):
        """Lit exactly when the number is above zero, at every state --
        including zero, where the first version's bug lived."""
        for status, incident in ((HealthStatus.UP, False),
                                 (HealthStatus.DEGRADED, False),
                                 (HealthStatus.DOWN, True)):
            html = page(status, open_incident=incident)
            for name, flag in ALARMS.items():
                classes, value = stat_card(html, name)
                assert (flag in classes) == (int(value) > 0), f"{name} at {status}"

    def test_counting_tiles_never_light_up(self, page):
        html = page(HealthStatus.DOWN, open_incident=True)
        for name in ("Devices", "Services", "Monitored"):
            assert not stat_card(html, name)[0] & {"is-warn", "is-bad"}, name


class TestNetworkOverview:
    def test_counts_the_devices_on_each_subnet(self, page):
        html = page(HealthStatus.UP, routed=True, devices=("172.16.20.5", "172.16.20.6", "198.51.100.7"))
        flat = " ".join(html.split())
        assert "<strong>IoT</strong> <code>172.16.20.0/24</code>" in flat
        assert "2 devices" in flat, "the one outside every subnet is not counted"
        assert '<rect class="net-track-fill" width="67"' in flat, "2 of 3 known devices"

    def test_one_device_is_not_plural(self, page):
        html = page(HealthStatus.UP, routed=True, devices=("172.16.20.5",))
        flat = " ".join(html.split())
        assert "1 device " in flat and "1 devices" not in flat


class TestBanners:
    def test_a_routed_subnet_is_not_a_warning(self, page):
        """Routed is a setting, not a fault. The network overview still says so."""
        html = page(HealthStatus.UP, routed=True)
        assert "Routed · IP identity only" in html, "the subnet still marks it"
        assert "routed rather than directly attached" not in html
        banners = re.findall(r'<div class="alert warn">(.*?)</div>', html, re.S)
        assert not any("IoT" in b for b in banners), banners
