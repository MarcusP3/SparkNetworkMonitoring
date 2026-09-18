"""Tests for the automatic scan schedule.

The bug these were written against was not a crash. Discovery *was* scheduled
at startup and had been all along -- but an interval trigger's first fire is
one whole interval away, so a fresh container sat there for fifteen minutes
doing nothing, and a rebuild started the fifteen minutes again. From the
outside that is indistinguishable from a feature that does not work, and the
only evidence either way was a log line nobody had a reason to read.

So the assertions here are mostly about *when* the first sweep happens, and
about the settings surviving a round trip through the form. The second one
matters more than it looks: a disabled <select> submits nothing, so the
obvious way to grey out the interval when automatic scanning is switched off
quietly erases it.
"""

from __future__ import annotations

import asyncio
import re
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from spark import db as D
from spark import scheduler as S
from spark.config import Config
from spark.discovery.runner import JOB_ID, SWEEP_INTERVAL_CHOICES
from spark.main import create_app
from spark.models import Device, Subnet, utcnow

SUBNET = {"name": "LAN", "cidr": "10.1.10.0/24", "vlan": 1, "attached": True}


def make_config(tmp: Path, *, subnets: bool = True) -> Config:
    config = Config.model_validate(
        {
            "app": {"data_dir": str(tmp / "data"), "port": 9701, "log_level": "WARNING"},
            "network": {"subnets": [SUBNET] if subnets else []},
        }
    )
    config.app.data_dir.mkdir(parents=True, exist_ok=True)
    return config


@pytest.fixture
def config():
    """A config plus a matching subnet row.

    Subnets moved from spark.yaml into the database in increment 4c, so a
    config with subnets in it is no longer enough to get a sweep scheduled --
    which is the whole reason these fixtures changed.
    """
    tmp = Path(tempfile.mkdtemp(prefix="spark-schedule-"))
    cfg = make_config(tmp)

    async def setup():
        D.init_engine(cfg)
        await D.init_db(cfg)
        async with D.session_scope() as session:
            session.add(Subnet(cidr="10.1.10.0/24", name="LAN", vlan=1, attached=True))

    asyncio.run(setup())
    yield cfg
    asyncio.run(D.close_engine())


def with_scheduler(body):
    """Run `body(scheduler)` inside a loop, with the scheduler up and then down.

    AsyncIOScheduler needs a running loop to start, and leaving the module
    global set would leak the scheduler into the next test.
    """

    async def go():
        S.start()
        try:
            return await body()
        finally:
            await S.shutdown()

    return asyncio.run(go())


def seconds_until_next_run() -> float | None:
    when = S.discovery_next_run()
    return None if when is None else (when - utcnow()).total_seconds()


# --------------------------------------------------------------------------
# When the first sweep happens
# --------------------------------------------------------------------------


class TestFirstRun:
    def test_startup_sweeps_within_seconds_rather_than_an_interval(self, config):
        async def body():
            await S.schedule_discovery(config, first_run_delay=5)
            return seconds_until_next_run()

        due = with_scheduler(body)
        # The whole point: a fresh container must not look dead for fifteen
        # minutes while it waits for a timer nobody can see.
        assert due is not None and due <= 10, due

    def test_without_a_delay_the_first_sweep_is_a_whole_interval_away(self, config):
        """The old behaviour, kept as a test so the fix cannot quietly regress.

        If this ever starts failing because the default changed, the startup
        call in main.py is what should be passing first_run_delay -- not this
        expectation that got loosened.
        """

        async def body():
            await S.schedule_discovery(config, {"enabled": True,
                                                "sweep_interval_seconds": 900})
            return seconds_until_next_run()

        due = with_scheduler(body)
        assert due is not None and due > 800, due

    def test_a_rescheduled_sweep_keeps_the_new_interval(self, config):
        async def body():
            await S.schedule_discovery(config, {"enabled": True,
                                                "sweep_interval_seconds": 900})
            await S.schedule_discovery(config, {"enabled": True,
                                                "sweep_interval_seconds": 300})
            return seconds_until_next_run()

        due = with_scheduler(body)
        # Five minutes plus up to 10% jitter, and definitely not fifteen.
        assert due is not None and 200 < due < 340, due


# --------------------------------------------------------------------------
# Whether it is scheduled at all
# --------------------------------------------------------------------------


class TestScheduledOrNot:
    def test_disabling_removes_the_job(self, config):
        async def body():
            await S.schedule_discovery(config, {"enabled": True})
            before = S.discovery_next_run()
            await S.schedule_discovery(config, {"enabled": False})
            return before, S.discovery_next_run()

        before, after = with_scheduler(body)
        assert before is not None
        assert after is None

    def test_no_subnets_means_nothing_is_scheduled_even_when_enabled(self):
        tmp = Path(tempfile.mkdtemp(prefix="spark-schedule-empty-"))
        cfg = make_config(tmp, subnets=False)

        async def setup():
            D.init_engine(cfg)
            await D.init_db(cfg)

        asyncio.run(setup())

        async def body():
            await S.schedule_discovery(cfg, {"enabled": True})
            return S.discovery_next_run()

        try:
            # Ticked but impossible. The page has to be able to say so, which
            # it can only do if these two states are distinguishable.
            assert with_scheduler(body) is None
        finally:
            asyncio.run(D.close_engine())

    def test_next_run_is_none_without_a_scheduler(self):
        assert S.get_scheduler() is None
        assert S.discovery_next_run() is None

    def test_disabling_twice_is_not_an_error(self, config):
        async def body():
            await S.schedule_discovery(config, {"enabled": False})
            await S.schedule_discovery(config, {"enabled": False})
            return S.discovery_next_run()

        assert with_scheduler(body) is None


# --------------------------------------------------------------------------
# The form on the Devices page
# --------------------------------------------------------------------------


@pytest.fixture
def client():
    tmp = Path(tempfile.mkdtemp(prefix="spark-schedule-web-"))
    cfg = make_config(tmp)
    with TestClient(create_app(cfg), follow_redirects=False) as c:
        c.post("/setup", data={"username": "admin",
                               "password": "correct horse battery",
                               "password_confirm": "correct horse battery"})
        yield c


def stored_interval_minutes(client: TestClient) -> int:
    """The selected option of the scan-interval dropdown specifically.

    Scoped to that one <select>: the page grew a subnet filter whose options
    are also numbers, and a page-wide search would start matching whichever
    came first in the markup.
    """
    page = client.get("/devices").text
    block = re.search(r'id="scan-interval".*?</select>', page, re.S)
    assert block, "the scan-interval dropdown is missing from the page"
    match = re.search(r'<option value="(\d+)"\s+selected', block.group(0))
    assert match, "no interval is selected in the dropdown"
    return int(match.group(1))


def next_scan_text(client: TestClient) -> str:
    """The rendered status line, not the whole page.

    Searching the page for "Next scan in" matches the countdown's own source
    in the inline <script>, which is true whatever the schedule is doing --
    a test that can never fail.
    """
    page = client.get("/devices").text
    match = re.search(r'id="next-scan"[^>]*>([^<]*)<', page)
    assert match, "the next-scan status line is missing from the page"
    return match.group(1).strip()


class TestScheduleForm:
    def test_the_page_offers_every_choice(self, client):
        page = client.get("/devices").text
        for minutes in SWEEP_INTERVAL_CHOICES:
            assert f'value="{minutes}"' in page, minutes

    def test_the_checkbox_reflects_the_stored_setting(self, client):
        assert "checked" in client.get("/devices").text

        client.post("/devices/schedule", data={"interval_minutes": "15"})
        assert next_scan_text(client) == "Automatic scanning is off."

    def test_applying_an_interval_persists_it(self, client):
        client.post("/devices/schedule", data={"auto": "1", "interval_minutes": "30"})
        assert stored_interval_minutes(client) == 30

    def test_turning_it_off_does_not_erase_the_interval(self, client):
        """The disabled-<select> trap, as a test.

        Grey the dropdown out with the `disabled` attribute and the browser
        stops submitting it, so switching automatic scanning off silently
        resets the interval to the default. You find out weeks later when it
        is scanning every fifteen minutes again and you are sure you changed
        it.
        """
        client.post("/devices/schedule", data={"auto": "1", "interval_minutes": "60"})
        client.post("/devices/schedule", data={"auto": "", "interval_minutes": "60"})
        client.post("/devices/schedule", data={"auto": "1", "interval_minutes": "60"})
        assert stored_interval_minutes(client) == 60

    def test_a_value_outside_the_offered_list_is_refused(self, client):
        client.post("/devices/schedule", data={"auto": "1", "interval_minutes": "30"})
        client.post("/devices/schedule", data={"auto": "1", "interval_minutes": "1"})
        # One minute is not on the menu; a sweep of a /24 can outlast it.
        assert stored_interval_minutes(client) == 30

    def test_junk_is_ignored_rather_than_stored_or_raised(self, client):
        client.post("/devices/schedule", data={"auto": "1", "interval_minutes": "30"})
        response = client.post("/devices/schedule",
                               data={"auto": "1", "interval_minutes": "banana"})
        assert response.status_code == 303, "a bad form must not show a 422 page"
        assert stored_interval_minutes(client) == 30

    def test_the_page_says_when_the_next_scan_is_due(self, client):
        # Scheduled at startup with a short first delay, so by the time the
        # page renders this is a real countdown rather than a claim.
        assert next_scan_text(client).startswith("Next scan in")

    def test_switching_off_stops_the_schedule_for_real(self, client):
        client.post("/devices/schedule", data={"auto": "", "interval_minutes": "15"})
        assert next_scan_text(client) == "Automatic scanning is off."

    def test_switching_back_on_reschedules_without_a_restart(self, client):
        client.post("/devices/schedule", data={"auto": "", "interval_minutes": "15"})
        client.post("/devices/schedule", data={"auto": "1", "interval_minutes": "15"})
        assert next_scan_text(client).startswith("Next scan in")

    def test_the_schedule_form_needs_a_login(self):
        tmp = Path(tempfile.mkdtemp(prefix="spark-schedule-anon-"))
        with TestClient(create_app(make_config(tmp)), follow_redirects=False) as anon:
            anon.post("/setup", data={"username": "admin",
                                      "password": "correct horse battery",
                                      "password_confirm": "correct horse battery"})
            anon.post("/logout")
            response = anon.post("/devices/schedule",
                                 data={"auto": "1", "interval_minutes": "5"})
            assert response.status_code == 303
            assert "/login" in response.headers.get("location", "")


class TestJobIdentity:
    def test_rescheduling_replaces_rather_than_stacks(self, config):
        async def body():
            for _ in range(5):
                await S.schedule_discovery(config, {"enabled": True})
            scheduler = S.get_scheduler()
            return [job.id for job in scheduler.get_jobs() if job.id == JOB_ID]

        # Five sweeps of the same network on the same timer would be a
        # self-inflicted denial of service.
        assert len(with_scheduler(body)) == 1


@pytest.fixture
def client_with_device():
    """A logged-in client whose database already holds one device.

    Seeded before the app starts rather than through the API: discovery needs a
    real network to find anything, and the device row markup is what these
    tests are about.
    """
    tmp = Path(tempfile.mkdtemp(prefix="spark-schedule-dev-"))
    cfg = make_config(tmp)

    async def seed():
        D.init_engine(cfg)
        await D.init_db(cfg)
        async with D.session_scope() as session:
            session.add(Device(mac="aa:bb:cc:dd:ee:ff", primary_ip="10.1.10.50",
                               hostname="nas.lan", friendly_name="nas",
                               subnet="LAN", last_seen=utcnow()))
        await D.close_engine()

    asyncio.run(seed())

    with TestClient(create_app(cfg), follow_redirects=False) as c:
        c.post("/setup", data={"username": "admin",
                               "password": "correct horse battery",
                               "password_confirm": "correct horse battery"})
        yield c


class TestInlineSaveButton:
    """The Save button next to each device name.

    It is hidden by CSS that hangs off a class JavaScript puts on <html>, so
    the server-rendered page must always contain the button and the value the
    script compares against. A browser with JavaScript off gets the old
    always-visible button and a form that still works, which is the right way
    for this to degrade.
    """

    def test_the_button_is_in_the_html_not_rendered_conditionally(self, client_with_device):
        page = client_with_device.get("/devices").text
        assert "nas.lan" in page, "the seeded device is not on the page"
        # Hidden means hidden in the browser, not absent from the response.
        # Omitting it server-side would leave a no-JS browser unable to save.
        assert "save-name" in page

    def test_the_name_field_carries_its_stored_value_to_compare_against(self, client_with_device):
        page = client_with_device.get("/devices").text
        assert 'data-original="nas"' in page

    def test_a_device_with_no_name_compares_against_empty(self, client_with_device):
        client_with_device.post("/devices/1/name", data={"friendly_name": ""})
        page = client_with_device.get("/devices").text
        # Not the hostname placeholder: typing the hostname in by hand is a
        # real edit, and comparing against the placeholder would hide Save.
        assert 'data-original=""' in page

    def test_saving_a_name_still_works(self, client_with_device):
        client_with_device.post("/devices/1/name", data={"friendly_name": "storage"})
        page = client_with_device.get("/devices").text
        assert 'data-original="storage"' in page
