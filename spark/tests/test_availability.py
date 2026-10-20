"""Availability: how much of a range each target was up.

From the incident rows and the check history. An outage explained by a
dependency is unreachable; one inside a maintenance window is planned;
neither counts up or down, and nor does time with no checks. The internet
gets a row of its own. The figures are worked out against a fixed clock;
the page and the CSV against the real one.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from spark import availability
from spark import db as D
from spark.main import create_app
from spark.models import (AlertIncident, CheckResult, CheckRollup, CheckType, Device, DeviceRole,
                          HealthStatus, Incident, InternetSample, MaintenanceWindow, Target,
                          utcnow)

from test_alerts import _config
from test_credentials import flat

PASSWORD = "correct horse battery"
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
DAY = timedelta(days=1)


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


@pytest.fixture
def db():
    """A switch and a NAS behind it, each with a ping target checked every minute."""
    config = _config()

    async def setup():  # type: ignore[no-untyped-def]
        D.init_engine(config)
        await D.init_db(config)
        async with D.session_scope() as s:
            s.add(Device(mac="aa:00:00:00:00:01", primary_ip="10.0.0.2", friendly_name="switch",
                         role=DeviceRole.SWITCH, last_seen=NOW))
            await s.flush()
            s.add(Device(mac="aa:00:00:00:00:02", primary_ip="10.0.0.20", friendly_name="nas",
                         parent_device_id=1, last_seen=NOW))
            await s.flush()
            s.add_all([
                Target(name="switch ping", check_type=CheckType.PING, address="10.0.0.2",
                       device_id=1, interval_seconds=60),
                Target(name="nas ping", check_type=CheckType.PING, address="10.0.0.20",
                       device_id=2, interval_seconds=60),
            ])
    run(setup())
    yield config
    run(D.close_engine())


def add(*rows) -> None:  # type: ignore[no-untyped-def]
    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            s.add_all(rows)
    run(go())


def checked(target_id: int, start: datetime, end: datetime) -> None:
    """A check a minute from start to end."""
    minutes = int((end - start).total_seconds() // 60)
    add(*[CheckResult(target_id=target_id, ts=start + timedelta(minutes=i), status=HealthStatus.UP)
          for i in range(minutes)])


def outage(target_id: int, opened: datetime, closed: datetime | None, *, explained=False) -> None:
    add(Incident(target_id=target_id, opened_at=opened, closed_at=closed,
                 resolution="recovered" if closed else None, suppressed_by_dependency=explained))


def figures(start=NOW - DAY, end=NOW) -> dict[str, availability.Availability]:  # type: ignore[no-untyped-def]
    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            return await availability.targets(s, start, end, now=NOW)
    return {r.target.name: r.figures for r in run(go())}


class TestRanges:
    def test_rolling(self):
        assert availability.span("24h", NOW, timezone.utc) == (NOW - DAY, NOW)
        assert availability.span("90d", NOW, timezone.utc) == (NOW - 90 * DAY, NOW)
        assert availability.span("nonsense", NOW, timezone.utc) == (NOW - 30 * DAY, NOW)

    def test_months_follow_the_local_calendar(self):
        chicago = ZoneInfo("America/Chicago")
        start, end = availability.span("month", NOW, chicago)
        assert start == datetime(2026, 10, 1, 5, tzinfo=timezone.utc) and end == NOW, "midnight CDT"
        start, end = availability.span("last-month", NOW, chicago)
        assert (start, end) == (datetime(2026, 9, 1, 5, tzinfo=timezone.utc),
                                datetime(2026, 10, 1, 5, tzinfo=timezone.utc))
        start, _ = availability.span("last-month", datetime(2027, 1, 10, tzinfo=timezone.utc), chicago)
        assert start == datetime(2026, 12, 1, 6, tzinfo=timezone.utc), "December, in CST"


class TestFigures:
    def test_one_outage(self, db):
        checked(1, NOW - DAY, NOW)
        outage(1, NOW - timedelta(hours=5), NOW - timedelta(hours=4, minutes=30))
        f = figures()["switch ping"]
        assert f.watched == 86400 and f.down == 1800 and f.outages == 1
        assert f.longest == f.mean_to_recover == 1800
        assert round(f.percent, 4) == round(100 * (1 - 1800 / 86400), 4)
        assert f.coverage == 100

    def test_only_the_part_in_the_range_is_down_but_it_lasted_all_of_it(self, db):
        checked(1, NOW - DAY, NOW)
        outage(1, NOW - DAY - timedelta(hours=1), NOW - DAY + timedelta(hours=1))
        f = figures()["switch ping"]
        assert f.down == 3600 and f.longest == 7200

    def test_still_down_counts_until_now(self, db):
        checked(1, NOW - DAY, NOW)
        outage(1, NOW - timedelta(minutes=10), None)
        assert figures()["switch ping"].down == 600

    def test_unreachable_is_neither_up_nor_down(self, db):
        checked(2, NOW - DAY, NOW)
        outage(2, NOW - timedelta(hours=2), NOW - timedelta(hours=1), explained=True)
        f = figures()["nas ping"]
        assert f.unreachable == 3600 and f.down == 0 and f.outages == 0
        assert f.percent == 100.0 and f.judged == 86400 - 3600

    def test_maintenance_is_planned_and_what_outlasts_it_is_not(self, db):
        checked(2, NOW - DAY, NOW)
        add(MaintenanceWindow(device_id=2, starts_at=NOW - timedelta(hours=3),
                              ends_at=NOW - timedelta(hours=2)))
        outage(2, NOW - timedelta(hours=2, minutes=30), NOW - timedelta(hours=1, minutes=30))
        f = figures()["nas ping"]
        assert f.planned == 1800 and f.down == 1800 and f.outages == 1
        assert f.longest == 1800, "the unplanned part"

    def test_an_outage_that_runs_into_a_window_is_planned_only_inside_it(self, db):
        checked(2, NOW - DAY, NOW)
        add(MaintenanceWindow(device_id=2, starts_at=NOW - timedelta(hours=2),
                              ends_at=NOW - timedelta(hours=1)))
        outage(2, NOW - timedelta(hours=3), NOW - timedelta(minutes=30))
        f = figures()["nas ping"]
        assert f.planned == 3600 and f.down == 5400, "an hour before it, half an hour after"
        assert f.longest == 5400

    def test_a_weekly_window_counts_every_week(self, db):
        checked(2, NOW - 30 * DAY, NOW)
        add(MaintenanceWindow(device_id=2, starts_at=NOW - 28 * DAY, ends_at=NOW - 28 * DAY + timedelta(hours=1),
                              weekly=True))
        outage(2, NOW - 22 * DAY, NOW - 6 * DAY)             # spans two of its weeks: -21d and -14d, and -7d
        f = figures(NOW - 30 * DAY, NOW)["nas ping"]
        assert f.planned == 3 * 3600

    def test_below_a_window_on_the_switch(self, db):
        checked(2, NOW - DAY, NOW)
        add(MaintenanceWindow(device_id=1, starts_at=NOW - timedelta(hours=3),
                              ends_at=NOW - timedelta(hours=1)))
        outage(2, NOW - timedelta(hours=2), NOW - timedelta(hours=1, minutes=30))
        f = figures()["nas ping"]
        assert f.planned == 1800 and f.down == 0 and f.outages == 0

    def test_unwatched_time_is_left_out(self, db):
        checked(1, NOW - timedelta(hours=12), NOW)       # paused, or SPARK off, before that
        outage(1, NOW - timedelta(hours=1), NOW - timedelta(minutes=30))
        f = figures()["switch ping"]
        assert f.watched == 43200 and f.coverage == 50
        assert round(f.percent, 4) == round(100 * (1 - 1800 / 43200), 4)

    def test_rollups_count_as_watched_too(self, db):
        add(*[CheckRollup(target_id=1, bucket_start=NOW - 30 * DAY + timedelta(hours=h),
                          bucket_seconds=3600, samples=60, up=60)
              for h in range(24 * 20)])
        add(*[CheckRollup(target_id=1, bucket_start=NOW - 10 * DAY + timedelta(minutes=5 * i),
                          bucket_seconds=300, samples=5, up=5)
              for i in range(12 * 24 * 3)])
        f = figures(NOW - 30 * DAY, NOW)["switch ping"]
        assert f.watched == 20 * 86400 + 3 * 86400

    def test_never_checked_is_not_listed(self, db):
        checked(1, NOW - DAY, NOW)
        assert list(figures()) == ["switch ping"]

    def test_worst_first(self, db):
        checked(1, NOW - DAY, NOW)
        checked(2, NOW - DAY, NOW)
        outage(1, NOW - timedelta(hours=3), NOW - timedelta(hours=2))
        assert list(figures()) == ["switch ping", "nas ping"]

    def test_the_internet(self, db):
        add(*[InternetSample(ts=NOW - DAY + timedelta(minutes=i), state="up") for i in range(1440)])
        add(AlertIncident(key="internet", title="The internet is down",
                          opened_at=NOW - timedelta(hours=6), closed_at=NOW - timedelta(hours=5, minutes=40),
                          resolution="recovered"))

        async def go():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                return await availability.internet(s, NOW - DAY, NOW, now=NOW)
        f = run(go())
        assert f.watched == 86400 and f.down == 1200 and f.outages == 1


# --------------------------------------------------------------------------
# The page and the CSV
# --------------------------------------------------------------------------


@pytest.fixture
def site(db):
    now = utcnow()
    checked(1, now - timedelta(hours=2), now)
    checked(2, now - timedelta(hours=2), now)
    outage(1, now - timedelta(minutes=40), now - timedelta(minutes=30))
    outage(2, now - timedelta(minutes=40), now - timedelta(minutes=30), explained=True)
    with TestClient(create_app(db), follow_redirects=False) as client:
        client.post("/setup", data={"setup_code": client.app.state.setup_code, "username": "admin",
                                    "password": PASSWORD, "password_confirm": PASSWORD,
                                    "timezone": "UTC"})
        yield client


class TestPage:
    def test_the_tab(self, site):
        page = flat(site.get("/alerts/availability?range=24h").text)
        assert "Availability · 24 hours" in page
        assert '<a class="chip" href="/alerts/availability?range=24h" aria-current="true">24 hours</a>' in page
        table = page[page.index("avail-table"):]
        assert table.index("switch ping") < table.index("nas ping"), "worst first"
        assert '<td class="num avail-up">91.67%</td>' in table, "10 minutes of 2 hours"
        assert '<td class="num avail-up">100%</td>' in table
        assert "+ 10m 0s unreachable" in table
        assert '<a href="/alerts?source=Target&amp;device=1#history">1</a>' in table
        assert ">Availability<" in page and 'href="/alerts/availability"' in page

    def test_an_unknown_range_is_thirty_days(self, site):
        page = flat(site.get("/alerts/availability?range=forever").text)
        assert "Availability · 30 days" in page

    def test_months_are_named(self, site):
        label = availability.month_name(utcnow(), timezone.utc)
        assert f"Availability · {label}" in site.get("/alerts/availability?range=month").text

    def test_nothing_yet(self, db):
        with TestClient(create_app(db), follow_redirects=False) as client:
            client.post("/setup", data={"setup_code": client.app.state.setup_code, "username": "admin",
                                        "password": PASSWORD, "password_confirm": PASSWORD,
                                        "timezone": "UTC"})
            assert "No checks in this range yet." in client.get("/alerts/availability").text

    def test_csv(self, site):
        answer = site.get("/alerts/availability.csv?range=24h")
        assert answer.status_code == 200 and answer.headers["content-type"].startswith("text/csv")
        assert answer.headers["content-disposition"].startswith('attachment; filename="spark-availability-')
        lines = answer.text.splitlines()
        assert lines[0].startswith("target,device,address,up_percent,outages,down_seconds")
        switch = next(line for line in lines if line.startswith("switch ping"))
        cells = switch.split(",")
        assert cells[1:3] == ["switch", "10.0.0.2"] and cells[4:6] == ["1", "600"]
        nas = next(line for line in lines if line.startswith("nas ping")).split(",")
        assert nas[3] == "100.0000" and nas[6] == "600", "unreachable, in seconds"

    def test_a_name_that_looks_like_a_formula_stays_text(self, site):
        async def rename():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                target = await s.get(Target, 1)
                target.name = '=HYPERLINK("http://evil.example","x")'
        run(rename())
        text = site.get("/alerts/availability.csv?range=24h").text
        assert '"\'=HYPERLINK(""http://evil.example"",""x"")"' in text

    def test_signed_out(self, db):
        with TestClient(create_app(db), follow_redirects=False) as client:
            for path in ("/alerts/availability", "/alerts/availability.csv"):
                assert client.get(path).status_code in (302, 303, 307)
