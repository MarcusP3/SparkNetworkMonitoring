"""Tests for downsampling and pruning.

The risk here is not a crash, it is quiet data loss: a fold that drops an
outage, an average that gets dragged toward zero by windows with no latency, or
a second run that duplicates every bucket. None of those raise. All of them are
discovered months later when a chart looks wrong and the raw rows are gone.
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, insert, select

from spark import db as D
from spark.config import Config
from spark.models import (
    FIVE_MINUTES,
    ONE_HOUR,
    CheckResult,
    CheckRollup,
    CheckType,
    HealthStatus,
    Target,
    utcnow,
)
from spark.retention import run_retention


@pytest.fixture
def db():
    tmp = Path(tempfile.mkdtemp(prefix="spark-retention-"))
    config = Config.model_validate(
        {"app": {"data_dir": str(tmp / "data")}, "network": {"subnets": []}}
    )
    config.app.data_dir.mkdir(parents=True, exist_ok=True)

    async def setup():
        D.init_engine(config)
        await D.init_db(config)
        async with D.session_scope() as session:
            session.add(Target(name="gw", check_type=CheckType.PING, address="10.1.10.1"))

    asyncio.run(setup())
    yield
    asyncio.run(D.close_engine())


async def _seed(days: float, *, every: int = 60, outage_at: float | None = None,
                outage_minutes: int = 20, latency: float | None = 1.5) -> int:
    """Insert `days` of history at `every` seconds. Returns the row count.

    A core bulk insert rather than ORM objects: building 80,000 instances to
    throw them straight at the database made this suite take minutes.
    """
    now = utcnow()
    count = int(days * 24 * 60 * 60 / every)
    rows = []
    for i in range(count):
        ts = now - timedelta(seconds=every * i)
        age = now - ts
        down = (
            outage_at is not None
            and timedelta(days=outage_at) <= age
            < timedelta(days=outage_at, minutes=outage_minutes)
        )
        rows.append({
            "target_id": 1,
            "ts": ts,
            "status": HealthStatus.DOWN if down else HealthStatus.UP,
            "latency_ms": None if down else latency,
            "detail": None,
            "suppressed": False,
        })
    async with D.session_scope() as session:
        await session.execute(insert(CheckResult), rows)
    return count


class TestFolding:
    def test_old_rows_become_buckets_and_recent_rows_stay(self, db):
        async def go():
            await _seed(days=14)
            await run_retention()
            async with D.session_scope() as s:
                raw = await s.scalar(select(func.count()).select_from(CheckResult))
                buckets = await s.scalar(
                    select(func.count()).select_from(CheckRollup)
                    .where(CheckRollup.bucket_seconds == FIVE_MINUTES)
                )
                oldest_raw = await s.scalar(select(func.min(CheckResult.ts)))
            return raw, buckets, oldest_raw

        raw, buckets, oldest = asyncio.run(go())
        # Seven of the fourteen days are kept raw, so about half survive.
        expected_kept = 7 * 24 * 60   # a week at one sample a minute
        assert expected_kept * 0.97 < raw < expected_kept * 1.03, raw
        assert buckets > 1_000
        assert (utcnow() - oldest) < timedelta(days=7, hours=1)

    def test_an_outage_survives_downsampling(self, db):
        async def go():
            await _seed(days=14, outage_at=10, outage_minutes=20)
            await run_retention()
            async with D.session_scope() as s:
                down = await s.scalar(select(func.sum(CheckRollup.down)))
                up = await s.scalar(select(func.sum(CheckRollup.up)))
            return down, up

        down, up = asyncio.run(go())
        # 20 minutes at one sample a minute is 20. Losing them would turn a
        # real outage into a fortnight that looks flawless.
        assert 18 <= down <= 22, down
        assert up > 1000

    def test_counts_are_kept_not_just_an_average(self, db):
        async def go():
            await _seed(days=14, outage_at=10)
            await run_retention()
            async with D.session_scope() as s:
                worst = (
                    await s.execute(
                        select(CheckRollup)
                        .where(CheckRollup.down > 0)
                        .order_by(CheckRollup.down.desc())
                        .limit(1)
                    )
                ).scalars().first()
            return worst

        worst = asyncio.run(go())
        assert worst is not None
        assert worst.samples == worst.up + worst.degraded + worst.down
        assert worst.availability is not None and worst.availability < 1.0

    def test_latency_range_is_preserved(self, db):
        async def go():
            now = utcnow()
            async with D.session_scope() as s:
                s.add_all([
                    CheckResult(target_id=1, ts=now - timedelta(days=8),
                                status=HealthStatus.UP, latency_ms=ms)
                    for ms in (1.0, 5.0, 9.0)
                ])
            await run_retention()
            async with D.session_scope() as s:
                return (await s.execute(select(CheckRollup))).scalars().first()

        bucket = asyncio.run(go())
        assert bucket.latency_min == 1.0
        assert bucket.latency_max == 9.0
        assert 4.9 < bucket.latency_avg < 5.1


class TestIdempotence:
    def test_running_twice_does_not_duplicate_buckets(self, db):
        async def go():
            await _seed(days=10)
            await run_retention()
            async with D.session_scope() as s:
                first = await s.scalar(select(func.count()).select_from(CheckRollup))
            await run_retention()
            async with D.session_scope() as s:
                second = await s.scalar(select(func.count()).select_from(CheckRollup))
            return first, second

        first, second = asyncio.run(go())
        # A retried or double-scheduled run must not double the history.
        assert first == second and first > 0

    def test_an_empty_database_is_not_an_error(self, db):
        summary = asyncio.run(run_retention())
        assert "error" not in summary
        assert summary["raw_deleted"] == 0

    def test_nothing_old_enough_is_a_no_op(self, db):
        async def go():
            await _seed(days=1)
            summary = await run_retention()
            async with D.session_scope() as s:
                raw = await s.scalar(select(func.count()).select_from(CheckResult))
                buckets = await s.scalar(select(func.count()).select_from(CheckRollup))
            return summary, raw, buckets

        summary, raw, buckets = asyncio.run(go())
        assert summary["raw_deleted"] == 0
        assert buckets == 0
        assert raw > 1_400, "a day of recent data must be left completely alone"


class TestHourlyRollup:
    def test_five_minute_buckets_roll_into_hourly_with_a_weighted_average(self, db):
        async def go():
            base = utcnow() - timedelta(days=100)
            base = base.replace(minute=0, second=0, microsecond=0)
            async with D.session_scope() as s:
                # Two buckets in the same hour with very different weights.
                s.add(CheckRollup(target_id=1, bucket_start=base,
                                  bucket_seconds=FIVE_MINUTES, samples=20,
                                  up=20, degraded=0, down=0,
                                  latency_min=10.0, latency_avg=10.0, latency_max=10.0))
                s.add(CheckRollup(target_id=1, bucket_start=base + timedelta(minutes=5),
                                  bucket_seconds=FIVE_MINUTES, samples=4,
                                  up=4, degraded=0, down=0,
                                  latency_min=100.0, latency_avg=100.0, latency_max=100.0))
            await run_retention()
            async with D.session_scope() as s:
                return (
                    await s.execute(
                        select(CheckRollup).where(CheckRollup.bucket_seconds == ONE_HOUR)
                    )
                ).scalars().first()

        hourly = asyncio.run(go())
        assert hourly is not None
        assert hourly.samples == 24
        # Weighted: (20*10 + 4*100)/24 = 25. A plain mean would say 55.
        assert 24.9 < hourly.latency_avg < 25.1, hourly.latency_avg
        assert hourly.latency_min == 10.0 and hourly.latency_max == 100.0

    def test_a_window_with_no_latency_does_not_drag_the_average_down(self, db):
        async def go():
            base = utcnow() - timedelta(days=100)
            base = base.replace(minute=0, second=0, microsecond=0)
            async with D.session_scope() as s:
                s.add(CheckRollup(target_id=1, bucket_start=base,
                                  bucket_seconds=FIVE_MINUTES, samples=10,
                                  up=10, degraded=0, down=0,
                                  latency_min=20.0, latency_avg=20.0, latency_max=20.0))
                # Fully down: no latency at all, not a latency of zero.
                s.add(CheckRollup(target_id=1, bucket_start=base + timedelta(minutes=5),
                                  bucket_seconds=FIVE_MINUTES, samples=10,
                                  up=0, degraded=0, down=10,
                                  latency_min=None, latency_avg=None, latency_max=None))
            await run_retention()
            async with D.session_scope() as s:
                return (
                    await s.execute(
                        select(CheckRollup).where(CheckRollup.bucket_seconds == ONE_HOUR)
                    )
                ).scalars().first()

        hourly = asyncio.run(go())
        # Treating the outage as latency 0 would report 10ms and make an
        # outage look like the network got faster.
        assert 19.9 < hourly.latency_avg < 20.1, hourly.latency_avg
        assert hourly.down == 10 and hourly.samples == 20
