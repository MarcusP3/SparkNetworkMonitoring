"""Time until full: storage history and the trend through it.

A sample of each pool and filesystem an hour, a least-squares line through
the last 14 days, "full in about N days" once there are two days of it.
An alert when that is under the rule's days, on two reads in a row; it ends
once it would take half as long again, or it stops growing. History is
thinned to a day after 90 days and gone after two years.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from spark import db as D
from spark import retention, snmp_alerts, storage
from spark.collectors.snmp import FilesystemReading, PoolReading
from spark.main import create_app
from spark.models import AlertIncident, AlertSuppression, StorageSample, utcnow

from test_credentials import flat
from test_storage import TB, _config, _sent, db, read, run  # noqa: F401

GB = 10**9
NOW = utcnow()
PASSWORD = "correct horse battery"


def history(row_id: int, *, days: float, used_start: float, per_day: float, size: float = 10 * TB,
            kind: str = "pool", name: str = "tank", end=NOW) -> None:  # type: ignore[no-untyped-def]
    """Hourly samples ending at `end`, growing `per_day` bytes a day."""
    hours = int(days * 24)

    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            s.add_all(StorageSample(snmp_device_id=row_id, kind=kind, name=name,
                                    ts=end - timedelta(hours=hours - h), size_bytes=int(size),
                                    used_bytes=int(used_start + per_day * h / 24))
                      for h in range(hours + 1))
    run(go())


def trend(row_id=1, kind="pool", name="tank"):  # type: ignore[no-untyped-def]
    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            return await storage.trend(s, row_id, kind, name, NOW)
    return run(go())


def samples(row_id=1) -> int:  # type: ignore[no-untyped-def]
    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            return await s.scalar(select(func.count(StorageSample.id)).where(
                StorageSample.snmp_device_id == row_id))
    return run(go())


def tank(used: float, size: float = 10 * TB) -> PoolReading:
    return PoolReading("tank", "ONLINE", int(used), int(size))


class TestTrend:
    def test_a_steady_rise(self):
        assert storage.fmt_days(8.4) == "about 8 days" and storage.fmt_days(0.3) == "under a day"
        assert storage.fmt_days(1) == "about 1 day" and storage.fmt_days(95) == "about 3 months"
        assert storage.fmt_days(None) == "not growing" and storage.fmt_days(1000) == "years"

    def test_the_line(self, db):
        history(1, days=5, used_start=7.5 * TB, per_day=100 * GB)
        t = trend()
        assert round(t.per_day / GB) == 100
        assert round(t.days_left) == 20, "2 TB free at 100 GB a day"
        assert abs((t.full_at - (NOW + timedelta(days=20))).total_seconds()) < 3600
        assert round(t.span.total_seconds() / 86400) == 5

    def test_two_days_before_it_says_anything(self, db):
        history(1, days=1.5, used_start=5 * TB, per_day=100 * GB)
        assert trend() is None

    @pytest.mark.parametrize("per_day", [0, -50 * GB, 0.5 * 10**6])
    def test_flat_shrinking_or_a_trickle_is_not_growing(self, db, per_day):
        history(1, days=7, used_start=5 * TB, per_day=per_day)
        t = trend()
        assert t is not None and t.days_left is None and t.full_at is None

    def test_only_the_last_two_weeks(self, db):
        history(1, days=30, used_start=1 * TB, per_day=200 * GB, end=NOW - timedelta(days=15))
        history(1, days=14, used_start=7 * TB, per_day=0)
        assert trend().days_left is None, "it grew, then stopped: now it is not growing"

    def test_one_sample_an_hour(self, db):
        async def store_at(minute):  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                await storage.store(s, 1, pools=[tank(5 * TB)], drives=None, filesystems=None,
                                    now=NOW + timedelta(minutes=minute))
        for minute in (0, 5, 30, 59, 60, 65, 125):
            run(store_at(minute))
        assert samples() == 3, "minutes 0, 60 and 125"

    def test_filesystems_too_and_nothing_without_a_size(self, db):
        async def go():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                await storage.store(s, 2, pools=None, drives=None, now=NOW, filesystems=[
                    FilesystemReading("/", 100 * GB, 40 * GB),
                    FilesystemReading("/broken", None, None)])
        run(go())
        assert samples(2) == 1


def read_at(hours_from_now: float, used: float) -> None:
    run(read(1, at=NOW + timedelta(hours=hours_from_now), pools=[tank(used)]))


class TestAlert:
    def test_full_soon_on_two_reads_then_it_stops(self, db):
        history(1, days=3, used_start=6.4 * TB, per_day=400 * GB)       # 7.6 TB now, 2.4 TB free
        read_at(0.1, 7.6 * TB)
        assert run(_sent()) == [], "one read"
        read_at(0.2, 7.6 * TB)
        [subject] = run(_sent())
        assert subject == "truenas: pool tank will be full in about 6 days"
        incident = run(_incidents())[0]
        assert incident.key == "trend:1:pool:tank"
        assert "growing 400 GB a day over the last 3 days" in incident.detail
        assert "At this rate it is full around" in incident.detail
        # Old data deleted: two weeks of steady use, the growth forgotten.
        history(1, days=14, used_start=4 * TB, per_day=0, end=NOW + timedelta(days=15))
        run(read(1, at=NOW + timedelta(days=15, minutes=5), pools=[tank(4 * TB)]))
        assert run(_sent())[-1] == "truenas: pool tank is no longer filling up fast"

    def test_slow_growth_is_not_an_alert(self, db):
        history(1, days=7, used_start=5 * TB, per_day=10 * GB)
        read_at(0.1, 5.07 * TB)
        read_at(0.2, 5.07 * TB)
        assert run(_sent()) == []

    def test_switched_off_suppressed_or_given_its_own_line(self, db):
        history(1, days=3, used_start=6.6 * TB, per_day=400 * GB)
        rules = run(_rules())
        run(_save_rules({**rules, "space_trend": False}))
        read_at(0.1, 7.8 * TB)
        read_at(0.2, 7.8 * TB)
        assert run(_sent()) == []
        run(_save_rules({**rules, "space_trend": True}))
        run(_suppress(1, 3))                                  # alert only under 3 days
        read_at(0.3, 7.8 * TB)
        read_at(0.4, 7.8 * TB)
        assert run(_sent()) == []


async def _incidents():  # type: ignore[no-untyped-def]
    async with D.session_scope() as s:
        return list((await s.execute(select(AlertIncident).order_by(AlertIncident.id))).scalars())


async def _rules():  # type: ignore[no-untyped-def]
    async with D.session_scope() as s:
        return await snmp_alerts.load(s)


async def _save_rules(rules):  # type: ignore[no-untyped-def]
    async with D.session_scope() as s:
        await D.save_setting(s, snmp_alerts.SETTING, rules)


async def _suppress(device_id, days):  # type: ignore[no-untyped-def]
    async with D.session_scope() as s:
        s.add(AlertSuppression(device_id=device_id, rule="space_trend", threshold=days))


class TestRetention:
    def test_hourly_then_daily_then_gone(self, db):
        history(1, days=100, used_start=1 * TB, per_day=GB, end=NOW)
        history(1, days=2, used_start=1 * TB, per_day=GB, end=NOW - timedelta(days=800), name="old")
        before = samples()
        run(retention.run_retention())
        after = samples()

        async def counts():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                cutoff = NOW - timedelta(days=91)
                old = await s.scalar(select(func.count(StorageSample.id)).where(
                    StorageSample.name == "tank", StorageSample.ts < cutoff))
                recent = await s.scalar(select(func.count(StorageSample.id)).where(
                    StorageSample.name == "tank", StorageSample.ts >= NOW - timedelta(days=89)))
                gone = await s.scalar(select(func.count(StorageSample.id)).where(
                    StorageSample.name == "old"))
                return old, recent, gone
        old, recent, gone = run(counts())
        assert old <= 10, "one a day for the days past 90"
        assert recent >= 89 * 24, "hourly inside 90 days"
        assert gone == 0 and after < before
        run(retention.run_retention())
        assert samples() == after, "running it again changes nothing"


# --------------------------------------------------------------------------
# The pages
# --------------------------------------------------------------------------


@pytest.fixture
def site(db):
    with TestClient(create_app(db), follow_redirects=False) as client:
        client.post("/setup", data={"setup_code": client.app.state.setup_code, "username": "admin",
                                    "password": PASSWORD, "password_confirm": PASSWORD,
                                    "timezone": "UTC"})
        yield client


class TestPages:
    def test_the_storage_card_says_when(self, site):
        history(1, days=3, used_start=6.4 * TB, per_day=400 * GB)
        run(read(1, at=NOW, pools=[tank(7.6 * TB)]))
        page = flat(site.get("/devices/1").text)
        assert '<th title="At the rate it has grown over the last two weeks">Full in</th>' in page
        assert '<span class="warn-text" title="Growing 400 GB a day">about 6 days</span>' in page

    def test_learning_and_not_growing(self, site):
        run(read(1, at=NOW, pools=[tank(5 * TB)]))
        assert ">learning</span>" in flat(site.get("/devices/1").text)
        history(2, days=4, used_start=40 * GB, per_day=0, size=100 * GB, kind="fs", name="/")

        async def fs():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                await storage.store(s, 2, pools=[], drives=None, now=NOW,
                                    filesystems=[FilesystemReading("/", 100 * GB, 40 * GB)])
        run(fs())
        assert '<span class="muted small">not growing</span>' in flat(site.get("/devices/2").text)

    def test_the_rule_on_the_form_and_its_line(self, site):
        page = flat(site.get("/alerts/rules").text)
        assert 'name="space_trend" value="1" checked' in page
        assert 'name="space_trend_days" min="1" max="365" required value="14"' in page
        site.post("/settings/suppressions", data={"device_id": "1", "rule": "space_trend",
                                                  "mode": "line", "threshold": "30"})
        assert "A pool or disk will soon be full: alert under 30 days" in \
            flat(site.get("/alerts/suppressions").text)


def test_migration_27_from_a_version_26_database():
    from sqlalchemy import inspect

    async def shape(wind_back: bool):  # type: ignore[no-untyped-def]
        config = _config()
        D.init_engine(config)
        await D.init_db(config)
        if wind_back:
            async with D.session_scope() as s:
                await s.execute(text("DROP TABLE storage_sample"))
                await D._set_version(s, 26)
            await D.close_engine()
            D.init_engine(config)
            await D.init_db(config)
        async with D.session_scope() as s:
            connection = await s.connection()
            columns = await connection.run_sync(
                lambda sync: sorted(c["name"] for c in inspect(sync).get_columns("storage_sample")))
            version = await D._get_version(s)
        await D.close_engine()
        return columns, version

    assert run(shape(True)) == run(shape(False))
    assert run(shape(False))[1] == D.CURRENT_VERSION >= 27
