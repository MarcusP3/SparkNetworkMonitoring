"""Tests for what the Targets list actually says.

Small file, narrow subject: the failure counter. It is the kind of text that
gets added because it is easy to render and then never reconsidered, and a
target that has been down for a month reporting "2613 consecutive failure(s)"
is noise sitting exactly where the useful line is.

The distinction worth keeping is between a target that has already flipped and
one that is on its way. Below the threshold the count is the only place a
wobble is visible before it becomes an incident; at or past it, the status pill
has already said everything the number would.
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
from spark.models import CheckType, HealthStatus, Target, utcnow

PASSWORD = "correct horse battery"


def make_config(tmp: Path) -> Config:
    config = Config.model_validate(
        {"app": {"data_dir": str(tmp / "data"), "port": 9703, "log_level": "WARNING"},
         "network": {"subnets": []}}
    )
    config.app.data_dir.mkdir(parents=True, exist_ok=True)
    return config


def client_with(targets: list[Target]) -> TestClient:
    tmp = Path(tempfile.mkdtemp(prefix="spark-targets-page-"))
    cfg = make_config(tmp)

    async def seed():
        D.init_engine(cfg)
        await D.init_db(cfg)
        async with D.session_scope() as session:
            for target in targets:
                session.add(target)
        await D.close_engine()

    asyncio.run(seed())
    client = TestClient(create_app(cfg), follow_redirects=False)
    client.__enter__()
    client.post("/setup", data={"username": "admin", "password": PASSWORD,
                                "password_confirm": PASSWORD})
    return client


def row_for(page: str, name: str) -> str:
    """The one table row containing this target, so assertions cannot stray."""
    match = re.search(rf"<tr[^>]*>(?:(?!</tr>).)*{re.escape(name)}.*?</tr>", page, re.S)
    assert match, f"no row for {name!r} on the page"
    return match.group(0)


@pytest.fixture
def down_target():
    client = client_with([
        Target(name="long-gone", check_type=CheckType.PING, address="10.1.10.9",
               status=HealthStatus.DOWN, consecutive_failures=2613,
               failure_threshold=4, last_checked_at=utcnow()),
    ])
    yield client
    client.__exit__(None, None, None)
    asyncio.run(D.close_engine())


@pytest.fixture
def wobbling_target():
    client = client_with([
        Target(name="flaky", check_type=CheckType.PING, address="10.1.10.8",
               status=HealthStatus.UP, consecutive_failures=2,
               failure_threshold=4, last_checked_at=utcnow()),
    ])
    yield client
    client.__exit__(None, None, None)
    asyncio.run(D.close_engine())


class TestPausedRowLayout:
    """A paused row must not dim its own controls or move the table.

    Both of these are CSS, so what is asserted here is the hook the CSS hangs
    off: if the class disappears from the markup the rule silently stops
    applying and nothing else notices.
    """

    def test_the_pause_toggle_has_a_fixed_width_class(self):
        client = client_with([
            Target(name="resting", check_type=CheckType.PING, address="10.1.10.6",
                   status=HealthStatus.PAUSED, enabled=False, last_checked_at=utcnow()),
        ])
        try:
            row = row_for(client.get("/targets").text, "resting")
            # "Pause" and "Resume" are different lengths and the table is
            # width:100%, so without a floor on this button pressing it shaves
            # pixels off every column to its left.
            assert "toggle-enabled" in row
            assert "Resume" in row
        finally:
            client.__exit__(None, None, None)
            asyncio.run(D.close_engine())

    def test_a_paused_row_is_marked_so_the_stale_cells_can_be_dimmed(self):
        client = client_with([
            Target(name="resting", check_type=CheckType.PING, address="10.1.10.6",
                   status=HealthStatus.PAUSED, enabled=False, last_checked_at=utcnow()),
        ])
        try:
            row = row_for(client.get("/targets").text, "resting")
            assert "is-paused" in row
            # The actions cell is exempt from the dimming by class, so it has
            # to keep that class for the exemption to mean anything.
            assert 'class="actions"' in row
        finally:
            client.__exit__(None, None, None)
            asyncio.run(D.close_engine())


class TestFailureCount:
    def test_a_down_target_does_not_report_its_running_total(self, down_target):
        row = row_for(down_target.get("/targets").text, "long-gone")
        # The number climbs forever and says nothing the status pill has not.
        assert "2613" not in row
        assert "consecutive" not in row

    def test_a_down_target_still_shows_its_status_and_last_check(self, down_target):
        row = row_for(down_target.get("/targets").text, "long-gone")
        # Removing the count must not take the useful part of the cell with it.
        assert "status-down" in row
        assert re.search(r"\d\d:\d\d:\d\d", row), "the last-checked time is gone"

    def test_a_target_on_its_way_down_still_shows_progress(self, wobbling_target):
        row = row_for(wobbling_target.get("/targets").text, "flaky")
        # Two of four failures is the only warning you get before it flips.
        assert "2 of 4" in row

    def test_a_healthy_target_says_nothing_about_failures(self):
        client = client_with([
            Target(name="fine", check_type=CheckType.PING, address="10.1.10.7",
                   status=HealthStatus.UP, consecutive_failures=0,
                   failure_threshold=4, last_checked_at=utcnow()),
        ])
        try:
            row = row_for(client.get("/targets").text, "fine")
            assert "of 4" not in row
        finally:
            client.__exit__(None, None, None)
            asyncio.run(D.close_engine())

    def test_a_paused_target_does_not_report_a_stale_count(self):
        """A paused target's counter is frozen, not current.

        Reporting it would describe the moment it was paused as though it were
        now, which is worse than saying nothing.
        """
        client = client_with([
            Target(name="resting", check_type=CheckType.PING, address="10.1.10.6",
                   status=HealthStatus.PAUSED, consecutive_failures=7,
                   failure_threshold=4, enabled=False, last_checked_at=utcnow()),
        ])
        try:
            row = row_for(client.get("/targets").text, "resting")
            assert "7 of 4" not in row and "consecutive" not in row
        finally:
            client.__exit__(None, None, None)
            asyncio.run(D.close_engine())
