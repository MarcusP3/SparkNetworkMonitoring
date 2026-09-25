"""The time zone preference: chosen at setup or under Preferences, used everywhere.

Stored times stay UTC; only display changes. So the tests check the three
places the zone reaches -- page timestamps, chart axes, quiet hours -- and
that a bad value can never be saved.
"""

from __future__ import annotations

import asyncio
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from spark import alerts, prefs
from spark import db as D
from spark.config import Config
from spark.main import create_app
from spark.models import CheckType, Target, utcnow
from spark.vault import vault_for

PASSWORD = "correct horse battery"


def _config() -> Config:
    tmp = Path(tempfile.mkdtemp(prefix="spark-prefs-"))
    config = Config.model_validate({
        "app": {"data_dir": str(tmp / "data"), "log_level": "WARNING"},
        "network": {"subnets": []},
    })
    config.app.data_dir.mkdir(parents=True, exist_ok=True)
    return config


def saved_zone(config) -> str:  # type: ignore[no-untyped-def]
    async def go():
        D.init_engine(config)
        async with D.session_scope() as s:
            return await prefs.get_timezone(s)
    return asyncio.run(go())


@pytest.fixture
def site():
    config = _config()
    with TestClient(create_app(config), follow_redirects=False) as client:
        client.post("/setup", data={"username": "admin", "password": PASSWORD,
                                    "password_confirm": PASSWORD, "timezone": "UTC"})
        yield client, config


class TestChoosing:
    def test_setup_saves_the_zone_chosen_there(self):
        config = _config()
        with TestClient(create_app(config), follow_redirects=False) as client:
            page = client.get("/setup").text
            assert 'name="timezone"' in page and '<optgroup label="America">' in page
            response = client.post("/setup", data={
                "username": "admin", "password": PASSWORD, "password_confirm": PASSWORD,
                "timezone": "America/Chicago"})
            assert response.status_code == 303
        assert saved_zone(config) == "America/Chicago"

    def test_setup_refuses_a_zone_that_does_not_exist(self):
        config = _config()
        with TestClient(create_app(config), follow_redirects=False) as client:
            response = client.post("/setup", data={
                "username": "admin", "password": PASSWORD, "password_confirm": PASSWORD,
                "timezone": "Mars/Olympus_Mons"})
            assert response.status_code == 400 and "not a time zone" in response.text

    def test_preferences_saves_and_says_so(self, site):
        client, config = site
        page = client.get("/preferences").text
        assert "<h2>Time zone</h2>" in page and re.search(r'<option value="UTC" selected>', page)
        response = client.post("/preferences", data={"timezone": "Europe/London"})
        assert response.status_code == 303 and response.headers["location"] == "/preferences?saved=1"
        assert saved_zone(config) == "Europe/London"
        page = client.get("/preferences?saved=1").text
        assert "shown in Europe/London" in page
        assert re.search(r'<option value="Europe/London" selected>Europe/London</option>', page)

    def test_a_bad_zone_is_refused_and_nothing_changes(self, site):
        client, config = site
        response = client.post("/preferences", data={"timezone": "Not/AZone"})
        assert response.status_code == 400 and "not a time zone" in response.text
        assert saved_zone(config) == "UTC"

    def test_the_account_chip_leads_here(self, site):
        client, _ = site
        assert re.search(r'<a href="/preferences" class="who-link', client.get("/").text)

    def test_the_list_offers_real_zones_and_not_legacy_aliases(self):
        choices = prefs.timezone_choices()
        assert choices[0] == "UTC"
        assert "America/Chicago" in choices and "Asia/Kolkata" in choices
        assert not any(c.startswith(("US/", "Etc/")) for c in choices)


class TestUsing:
    def test_page_times_are_shown_in_the_zone(self, site):
        client, _ = site
        stamp = datetime(2026, 9, 24, 3, 30, tzinfo=timezone.utc)

        async def seed():
            async with D.session_scope() as s:
                s.add(Target(name="gw", check_type=CheckType.PING, address="10.0.0.1",
                             last_checked_at=stamp))
        asyncio.run(seed())

        assert "03:30:00" in client.get("/targets").text
        client.post("/preferences", data={"timezone": "America/Chicago"})
        page = client.get("/targets").text
        # 03:30 UTC is 22:30 the evening before in Chicago (CDT).
        assert "22:30:00" in page and "03:30:00" not in page

    def test_quiet_hours_follow_the_preference(self, site, monkeypatch):
        client, config = site

        async def setup():
            async with D.session_scope() as s:
                settings = await alerts.load(s)
                settings.update(quiet_hours_start="22:00", quiet_hours_end="07:00")
                await D.save_setting(s, alerts.SETTING, settings)
                await alerts.set_webhook(s, vault_for(config),
                                         "https://discord.com/api/webhooks/1/token")
                await alerts.enqueue(s, kind="down", subject="gw is down")
        asyncio.run(setup())
        client.post("/preferences", data={"timezone": "America/Chicago"})

        # 08:00 UTC is 02:00-03:00 in Chicago: quiet there, not quiet in UTC.
        # Tomorrow, so the queued alert is already due.
        tomorrow_8 = (utcnow() + timedelta(days=1)).replace(hour=8, minute=0, second=0,
                                                            microsecond=0)
        monkeypatch.setattr(alerts, "utcnow", lambda: tomorrow_8)
        asyncio.run(alerts.dispatch(config))

        async def status():
            async with D.session_scope() as s:
                return [str(n.status) for n in await alerts.recent(s)]
        assert asyncio.run(status()) == ["held"]

    def test_the_alerts_card_names_the_zone_and_links_to_change_it(self, site):
        client, _ = site
        client.post("/preferences", data={"timezone": "Asia/Tokyo"})
        page = client.get("/settings/alerts").text
        assert "Asia/Tokyo (<a href=\"/preferences\">change</a>)" in page
        assert 'name="timezone"' not in page, "the card no longer sets the zone itself"

    def test_an_older_install_keeps_its_quiet_hours_zone(self):
        config = _config()

        async def go():
            D.init_engine(config)
            await D.init_db(config)
            async with D.session_scope() as s:
                await D.save_setting(s, "alerting", {"timezone": "Australia/Perth"})
                return await prefs.get_timezone(s)
        assert asyncio.run(go()) == "Australia/Perth"
        asyncio.run(D.close_engine())


def test_chart_axes_use_the_zone():
    from spark import charts
    from spark.snmp_history import Window

    w = Window.ending(datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc), "1h")
    # The window ends at 12:01 UTC, which is 07:01 in Chicago (CDT).
    assert ">12:01<" in str(charts.x_labels(w)[-1])
    assert ">07:01<" in str(charts.x_labels(w, prefs.zone_of("America/Chicago"))[-1])
    svg = charts.chart([charts.Line([1.0] * w.count)], w, y_max=10, fmt=str, title="t",
                       tz=prefs.zone_of("America/Chicago"), tz_name="America/Chicago")
    assert 'data-tz="America/Chicago"' in svg


def test_local_formats_in_the_zone_and_handles_nothing():
    stamp = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
    assert prefs.local(stamp, prefs.zone_of("America/New_York"), "%H:%M %Z") == "07:00 EST"
    assert prefs.local(None, timezone.utc, "%H:%M") == "—"
    assert prefs.zone_of("nonsense") is timezone.utc
