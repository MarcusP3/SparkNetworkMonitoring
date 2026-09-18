"""Keeping the database from growing forever.

Measured: a check result costs about 147 bytes on disk. Ten targets on a
15-second interval is 57,600 rows a day, so roughly 250 MB a month and 3 GB a
year — unbounded, because nothing ever removed them. The retention settings
have existed since increment 1 and nothing read them.

Raw results answer "what was the latency at 14:32:15", which matters for about
a week. After that the question is "what did last month look like", and a
five-minute bucket answers it with a sixtieth of the rows. Hourly buckets carry
it out to two years for about a thousandth.

Two deliberate choices about how this touches the disk:

  * It runs once a night, as a handful of set-based statements in a single
    transaction. Not a row at a time, not a loop in Python pulling a million
    rows across the process boundary.

  * It never VACUUMs. Deleting rows in SQLite frees pages for reuse rather
    than shrinking the file, so a pruned database plateaus and new inserts
    refill the same pages. VACUUM would rewrite the entire file every night,
    which is precisely the write amplification worth avoiding on an SSD.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import purge_expired
from .db import get_setting, save_setting, session_scope
from .models import FIVE_MINUTES, ONE_HOUR, utcnow

log = logging.getLogger(__name__)

JOB_ID = "retention:nightly"
STATE_KEY = "retention_state"

# The bucket an instant belongs to, as SQLite sees it. Integer division of the
# epoch second, back into a datetime string in the same format the ORM writes.
_BUCKET = "datetime(CAST(strftime('%s', {column}) / :width AS INTEGER) * :width, 'unixepoch')"


async def _fold_raw_into_buckets(session: AsyncSession, cutoff, width: int) -> int:
    """Aggregate raw results older than the cutoff into fixed-width buckets.

    Status counts rather than one average: "95% up" and "up all month except a
    ninety-minute outage" are different months, and a mean hides which you had.
    """
    bucket = _BUCKET.format(column="ts")
    result = await session.execute(
        text(f"""
            INSERT OR IGNORE INTO check_rollup
                (target_id, bucket_start, bucket_seconds, samples,
                 up, degraded, down, latency_min, latency_avg, latency_max)
            SELECT
                target_id,
                {bucket},
                :width,
                COUNT(*),
                SUM(CASE WHEN status = 'up' THEN 1 ELSE 0 END),
                SUM(CASE WHEN status = 'degraded' THEN 1 ELSE 0 END),
                SUM(CASE WHEN status = 'down' THEN 1 ELSE 0 END),
                MIN(latency_ms), AVG(latency_ms), MAX(latency_ms)
            FROM check_result
            WHERE ts < :cutoff
            GROUP BY target_id, {bucket}
        """),
        {"width": width, "cutoff": cutoff},
    )
    return result.rowcount or 0


async def _fold_buckets_into_wider(session: AsyncSession, cutoff, src: int, dst: int) -> int:
    """Roll narrow buckets up into wider ones.

    The average has to be weighted by sample count, and buckets with no latency
    at all (a target that was down for the whole window) must not drag it to
    zero -- they contribute no samples to the denominator rather than a zero.
    """
    bucket = _BUCKET.format(column="bucket_start")
    result = await session.execute(
        text(f"""
            INSERT OR IGNORE INTO check_rollup
                (target_id, bucket_start, bucket_seconds, samples,
                 up, degraded, down, latency_min, latency_avg, latency_max)
            SELECT
                target_id,
                {bucket},
                :width,
                SUM(samples), SUM(up), SUM(degraded), SUM(down),
                MIN(latency_min),
                SUM(latency_avg * samples)
                  / NULLIF(SUM(CASE WHEN latency_avg IS NOT NULL THEN samples END), 0),
                MAX(latency_max)
            FROM check_rollup
            WHERE bucket_seconds = :src AND bucket_start < :cutoff
            GROUP BY target_id, {bucket}
        """),
        {"width": dst, "src": src, "cutoff": cutoff},
    )
    return result.rowcount or 0


async def run_retention() -> dict:
    """Downsample and prune. Safe to run twice; safe to skip a night."""
    started = utcnow()
    summary: dict = {"started_at": started.isoformat()}

    try:
        async with session_scope() as session:
            settings = await get_setting(session, "retention")
            raw_days = int(settings.get("raw_days", 7) or 7)
            five_minute_days = int(settings.get("five_minute_days", 90) or 90)
            hourly_days = int(settings.get("hourly_days", 730) or 730)

            raw_cutoff = started - timedelta(days=raw_days)
            five_cutoff = started - timedelta(days=five_minute_days)
            hourly_cutoff = started - timedelta(days=hourly_days)

            # Fold before deleting, always in that order: a crash between the
            # two costs a duplicate-safe re-fold, the reverse costs the data.
            summary["five_minute_buckets"] = await _fold_raw_into_buckets(
                session, raw_cutoff, FIVE_MINUTES
            )
            deleted_raw = await session.execute(
                text("DELETE FROM check_result WHERE ts < :cutoff"),
                {"cutoff": raw_cutoff},
            )
            summary["raw_deleted"] = deleted_raw.rowcount or 0

            summary["hourly_buckets"] = await _fold_buckets_into_wider(
                session, five_cutoff, FIVE_MINUTES, ONE_HOUR
            )
            deleted_five = await session.execute(
                text(
                    "DELETE FROM check_rollup "
                    "WHERE bucket_seconds = :width AND bucket_start < :cutoff"
                ),
                {"width": FIVE_MINUTES, "cutoff": five_cutoff},
            )
            summary["five_minute_deleted"] = deleted_five.rowcount or 0

            deleted_hourly = await session.execute(
                text(
                    "DELETE FROM check_rollup "
                    "WHERE bucket_seconds = :width AND bucket_start < :cutoff"
                ),
                {"width": ONE_HOUR, "cutoff": hourly_cutoff},
            )
            summary["hourly_deleted"] = deleted_hourly.rowcount or 0

            # Expired sessions and old login attempts used to be cleared only
            # at startup, so an instance that stayed up never cleared them.
            await purge_expired(session)

            summary["finished_at"] = utcnow().isoformat()
            await save_setting(session, STATE_KEY, summary)

        log.info(
            "Retention: %d raw rows folded into %d five-minute buckets, "
            "%d five-minute rolled into %d hourly, %d hourly expired",
            summary["raw_deleted"], summary["five_minute_buckets"],
            summary["five_minute_deleted"], summary["hourly_buckets"],
            summary["hourly_deleted"],
        )
    except Exception as exc:  # noqa: BLE001 - never take the scheduler down
        log.exception("Retention run failed")
        summary["error"] = f"{type(exc).__name__}: {exc}"
    return summary
