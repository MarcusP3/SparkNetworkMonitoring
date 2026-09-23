"""The dashboard's stat tiles: colour appears only when there is something wrong.

Degraded, Down and Open incidents shipped with amber and red icon tiles that
were unconditional, so a healthy network showed a permanent red icon above a
neutral 0. The tile and the number disagreed about whether anything was wrong,
and a red that is always there is a red people learn to stop seeing -- the one
habit a monitoring UI cannot afford to teach.
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
from spark.models import CheckType, HealthStatus, Incident, Target, utcnow

PASSWORD = "correct horse battery"


def make_client(status: HealthStatus | None, open_incident: bool):
    tmp = Path(tempfile.mkdtemp(prefix="spark-dash-"))
    cfg = Config.model_validate({
        "app": {"data_dir": str(tmp / "data"), "port": 9707, "log_level": "WARNING"},
        "network": {"subnets": []},
    })
    cfg.app.data_dir.mkdir(parents=True, exist_ok=True)

    async def seed():
        D.init_engine(cfg)
        await D.init_db(cfg)
        if status is not None:
            async with D.session_scope() as s:
                s.add(Target(name="gw", check_type=CheckType.PING,
                             address="10.1.10.1", status=status, enabled=True))
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

    def render(status=None, open_incident=False) -> str:
        client = make_client(status, open_incident)
        clients.append(client)
        client.__enter__()
        client.post("/setup", data={"username": "admin", "password": PASSWORD,
                                    "password_confirm": PASSWORD})
        return client.get("/").text

    yield render
    for client in clients:
        client.__exit__(None, None, None)


def tile_classes(html: str, name: str) -> set[str]:
    """The classes on the icon tile of the stat card labelled `name`."""
    match = re.search(
        r'<span class="(stat-icon[^"]*)">(?:(?!<span class="stat-icon).)*?'
        r'<span class="stat-name">' + re.escape(name) + "</span>",
        html, re.S,
    )
    assert match, f"no stat card named {name!r}"
    return set(match.group(1).split())


STATUS_TILES = ("Degraded", "Down", "Open incidents")


class TestHealthyNetwork:
    def test_status_tiles_are_neutral_when_nothing_is_wrong(self, page):
        html = page(HealthStatus.UP)
        for name in STATUS_TILES:
            assert tile_classes(html, name) == {"stat-icon"}, (
                f"{name} is coloured on a healthy network"
            )

    def test_neutral_with_nothing_monitored_at_all(self, page):
        html = page(None)
        for name in STATUS_TILES:
            assert tile_classes(html, name) == {"stat-icon"}


class TestSomethingWrong:
    def test_degraded_turns_amber(self, page):
        html = page(HealthStatus.DEGRADED)
        assert "tile-4" in tile_classes(html, "Degraded")
        assert tile_classes(html, "Down") == {"stat-icon"}, "only the one that is true"

    def test_down_turns_red(self, page):
        html = page(HealthStatus.DOWN)
        assert "tile-5" in tile_classes(html, "Down")
        assert tile_classes(html, "Degraded") == {"stat-icon"}

    def test_an_open_incident_turns_red(self, page):
        html = page(HealthStatus.DOWN, open_incident=True)
        assert "tile-5" in tile_classes(html, "Open incidents")

    def test_tile_and_number_agree(self, page):
        """The bug in one assertion: tile colour and number colour share a
        condition, so they can never disagree.

        Checked on a healthy network as well as a broken one, and that matters.
        The first version of this test only tried non-zero states -- where the
        two already agreed even with the bug in place -- so it passed against
        the very code it was written to catch. The disagreement lived at zero.
        """
        cards = (("Degraded", "is-warn"), ("Down", "is-bad"),
                 ("Open incidents", "is-bad"))
        for status, incident in ((HealthStatus.UP, False),
                                 (HealthStatus.DEGRADED, False),
                                 (HealthStatus.DOWN, True)):
            html = page(status, open_incident=incident)
            for name, flag in cards:
                coloured_tile = len(tile_classes(html, name)) > 1
                card = re.search(
                    r'<div class="stat ([^"]*)">\s*<span class="stat-head">'
                    r'(?:(?!<div class="stat ).)*?' + re.escape(name), html, re.S)
                coloured_number = bool(card and flag in card.group(1))
                assert coloured_tile == coloured_number, f"{name} at {status}"


class TestUnaffected:
    def test_non_status_tiles_never_take_a_status_colour(self, page):
        html = page(HealthStatus.DOWN, open_incident=True)
        for name in ("Devices", "Services", "Monitored"):
            assert tile_classes(html, name) == {"stat-icon"}, name
