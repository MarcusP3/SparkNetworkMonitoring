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
from datetime import datetime, timedelta, timezone

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


def _floor(when: datetime, width: int) -> datetime:
    """The start of the bucket `when` falls in.

    Cutoffs are aligned to this, so no bucket ever straddles one. Unaligned,
    tonight folded the part of a bucket before the cutoff and tomorrow's fold
    of the rest hit the existing row, was ignored, and was then deleted: a
    bucket's worth of samples per series per night, lost silently.
    """
    epoch = int(when.timestamp())
    return datetime.fromtimestamp(epoch - epoch % width, tz=timezone.utc)


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


# --------------------------------------------------------------------------
# SNMP history
#
# The same ladder as check results -- raw, then five-minute, then hourly -- on
# the same settings, so "how long is history kept" has one answer.
#
# Each table's fold is a statement pair written out rather than generated from
# a column list: these are the statements that decide what the long-term
# history says, and they should be readable as SQL.
# --------------------------------------------------------------------------

_SNMP_HEALTH_RAW = """
    INSERT OR IGNORE INTO snmp_health_rollup
        (snmp_device_id, bucket_start, bucket_seconds, samples, reachable,
         cpu_avg, cpu_max, load_avg, memory_avg, memory_max, temperature_max)
    SELECT
        snmp_device_id, {bucket}, :width, COUNT(*),
        SUM(CASE WHEN reachable THEN 1 ELSE 0 END),
        AVG(cpu_percent), MAX(cpu_percent), AVG(load_1min),
        AVG(memory_percent), MAX(memory_percent), MAX(temperature_max)
    FROM snmp_health_sample
    WHERE ts < :cutoff
    GROUP BY snmp_device_id, {bucket}
"""

# Weighted by answered polls, not all polls: health values exist only when the
# device answered, so an hour with a long silence must not pull its averages
# toward whichever bucket happened to answer least.
_SNMP_HEALTH_WIDER = """
    INSERT OR IGNORE INTO snmp_health_rollup
        (snmp_device_id, bucket_start, bucket_seconds, samples, reachable,
         cpu_avg, cpu_max, load_avg, memory_avg, memory_max, temperature_max)
    SELECT
        snmp_device_id, {bucket}, :width, SUM(samples), SUM(reachable),
        SUM(cpu_avg * reachable)
          / NULLIF(SUM(CASE WHEN cpu_avg IS NOT NULL THEN reachable END), 0),
        MAX(cpu_max),
        SUM(load_avg * reachable)
          / NULLIF(SUM(CASE WHEN load_avg IS NOT NULL THEN reachable END), 0),
        SUM(memory_avg * reachable)
          / NULLIF(SUM(CASE WHEN memory_avg IS NOT NULL THEN reachable END), 0),
        MAX(memory_max), MAX(temperature_max)
    FROM snmp_health_rollup
    WHERE bucket_seconds = :src AND bucket_start < :cutoff
    GROUP BY snmp_device_id, {bucket}
"""

_SNMP_IF_RAW = """
    INSERT OR IGNORE INTO snmp_interface_rollup
        (interface_id, bucket_start, bucket_seconds, samples,
         in_avg, in_max, out_avg, out_max, in_errors, out_errors)
    SELECT
        interface_id, {bucket}, :width, COUNT(*),
        AVG(in_bps), MAX(in_bps), AVG(out_bps), MAX(out_bps),
        SUM(in_errors), SUM(out_errors)
    FROM snmp_interface_sample
    WHERE ts < :cutoff
    GROUP BY interface_id, {bucket}
"""

_SNMP_IF_WIDER = """
    INSERT OR IGNORE INTO snmp_interface_rollup
        (interface_id, bucket_start, bucket_seconds, samples,
         in_avg, in_max, out_avg, out_max, in_errors, out_errors)
    SELECT
        interface_id, {bucket}, :width, SUM(samples),
        SUM(in_avg * samples)
          / NULLIF(SUM(CASE WHEN in_avg IS NOT NULL THEN samples END), 0),
        MAX(in_max),
        SUM(out_avg * samples)
          / NULLIF(SUM(CASE WHEN out_avg IS NOT NULL THEN samples END), 0),
        MAX(out_max), SUM(in_errors), SUM(out_errors)
    FROM snmp_interface_rollup
    WHERE bucket_seconds = :src AND bucket_start < :cutoff
    GROUP BY interface_id, {bucket}
"""


async def _snmp_retention(session: AsyncSession, raw_cutoff, five_cutoff,
                          hourly_cutoff) -> dict:
    """Fold, then delete, for both SNMP series. Same order rule as checks."""
    out: dict = {}
    series = (
        ("health", "snmp_health_sample", "snmp_health_rollup",
         _SNMP_HEALTH_RAW, _SNMP_HEALTH_WIDER),
        ("interfaces", "snmp_interface_sample", "snmp_interface_rollup",
         _SNMP_IF_RAW, _SNMP_IF_WIDER),
    )
    raw_bucket = _BUCKET.format(column="ts")
    wide_bucket = _BUCKET.format(column="bucket_start")
    for name, raw_table, rollup_table, fold_raw, fold_wider in series:
        folded = await session.execute(
            text(fold_raw.format(bucket=raw_bucket)),
            {"width": FIVE_MINUTES, "cutoff": raw_cutoff},
        )
        deleted = await session.execute(
            text(f"DELETE FROM {raw_table} WHERE ts < :cutoff"), {"cutoff": raw_cutoff}
        )
        hourly = await session.execute(
            text(fold_wider.format(bucket=wide_bucket)),
            {"width": ONE_HOUR, "src": FIVE_MINUTES, "cutoff": five_cutoff},
        )
        deleted_five = await session.execute(
            text(f"DELETE FROM {rollup_table} "
                 "WHERE bucket_seconds = :width AND bucket_start < :cutoff"),
            {"width": FIVE_MINUTES, "cutoff": five_cutoff},
        )
        deleted_hourly = await session.execute(
            text(f"DELETE FROM {rollup_table} "
                 "WHERE bucket_seconds = :width AND bucket_start < :cutoff"),
            {"width": ONE_HOUR, "cutoff": hourly_cutoff},
        )
        out[name] = {
            "five_minute_buckets": folded.rowcount or 0,
            "raw_deleted": deleted.rowcount or 0,
            "hourly_buckets": hourly.rowcount or 0,
            "five_minute_deleted": deleted_five.rowcount or 0,
            "hourly_deleted": deleted_hourly.rowcount or 0,
        }
    return out


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

            raw_cutoff = _floor(started - timedelta(days=raw_days), FIVE_MINUTES)
            five_cutoff = _floor(started - timedelta(days=five_minute_days), ONE_HOUR)
            hourly_cutoff = _floor(started - timedelta(days=hourly_days), ONE_HOUR)

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

            summary["snmp"] = await _snmp_retention(
                session, raw_cutoff, five_cutoff, hourly_cutoff
            )

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
