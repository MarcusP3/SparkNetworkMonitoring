"""The polling scheduler.

One APScheduler instance in the same process and the same event loop as the web
app. No broker, no worker pool, no Redis -- a homelab polling a few dozen
targets over asyncio does not need any of it, and every moving part is a thing
that breaks at 3 a.m.

Jobs are reconciled against the database rather than created once at startup,
so adding a target in the UI schedules it immediately and disabling one stops
it, without a restart.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from .db import get_setting, session_scope
from .discovery.runner import JOB_ID as DISCOVERY_JOB_ID
from .discovery.runner import run_sweep
from .engine.runner import run_target
from .retention import JOB_ID as RETENTION_JOB_ID
from .retention import run_retention
from .models import Target

log = logging.getLogger(__name__)

JOB_PREFIX = "target:"

_scheduler: AsyncIOScheduler | None = None


def job_id(target_id: int) -> str:
    return f"{JOB_PREFIX}{target_id}"


def get_scheduler() -> AsyncIOScheduler | None:
    return _scheduler


def start() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    scheduler = AsyncIOScheduler(
        job_defaults={
            # A slow check must not stack up behind itself, and a missed run
            # should be skipped rather than replayed in a burst.
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": 30,
        }
    )
    scheduler.start()
    _scheduler = scheduler
    log.info("Scheduler started")
    return scheduler


async def shutdown() -> None:
    global _scheduler
    if _scheduler is None:
        return
    _scheduler.shutdown(wait=False)
    _scheduler = None
    log.info("Scheduler stopped")


def schedule_target(target: Target) -> None:
    """Add or update one target's job."""
    scheduler = _scheduler
    if scheduler is None:
        return
    interval = max(5, int(target.interval_seconds or 60))
    scheduler.add_job(
        run_target,
        "interval",
        seconds=interval,
        # Without jitter, every target added in the same minute polls in the
        # same instant forever after.
        jitter=min(int(interval * 0.1) or 1, 30),
        args=[target.id],
        id=job_id(target.id),
        replace_existing=True,
        name=f"{target.name} ({_check_type(target)})",
    )


def unschedule_target(target_id: int) -> None:
    scheduler = _scheduler
    if scheduler is None:
        return
    try:
        scheduler.remove_job(job_id(target_id))
    except Exception:  # noqa: BLE001 - job may already be gone
        pass


async def sync_jobs() -> int:
    """Make the scheduler match the database.

    Called at startup and after any change to targets. Returns how many jobs
    are scheduled, which is the number worth logging.
    """
    scheduler = _scheduler
    if scheduler is None:
        return 0

    async with session_scope() as session:
        targets = list(
            (await session.execute(select(Target).where(Target.enabled.is_(True))))
            .scalars()
            .all()
        )

    wanted = {job_id(t.id) for t in targets}
    for job in scheduler.get_jobs():
        if job.id.startswith(JOB_PREFIX) and job.id not in wanted:
            scheduler.remove_job(job.id)
    for target in targets:
        schedule_target(target)
    return len(targets)


async def schedule_discovery(
    config,  # type: ignore[no-untyped-def]
    settings: dict | None = None,
    *,
    subnet_count: int | None = None,
    first_run_delay: float | None = None,
) -> bool:
    """Add or update the periodic subnet sweep.

    Returns whether it is scheduled, which is worth logging at startup: a
    silently absent sweep looks exactly like a network with nothing on it.

    `settings` and `subnet_count` let a caller that has just written them pass
    them straight in.
    That is not only a saved round trip: the alternative is opening a second
    session for a read from inside a request that may still hold the write
    lock, which is the shape of the bug that made the Devices page hang.

    `first_run_delay` exists because of a genuinely misleading default. An
    interval trigger's first fire is one whole interval away, so a fresh
    container does not sweep for fifteen minutes, and every rebuild restarts
    that clock. Anyone who deploys, opens the Devices page and presses "Scan
    now" concludes, reasonably, that automatic scanning does not work. Startup
    passes a few seconds here so the first sweep lands while you are still
    looking at the page.
    """
    scheduler = _scheduler
    if scheduler is None:
        return False

    if settings is None or subnet_count is None:
        from .subnets import count_enabled

        async with session_scope() as session:
            if settings is None:
                settings = await get_setting(session, "discovery")
            if subnet_count is None:
                subnet_count = await count_enabled(session)

    if not settings.get("enabled", True) or not subnet_count:
        try:
            scheduler.remove_job(DISCOVERY_JOB_ID)
        except Exception:  # noqa: BLE001 - not scheduled is the desired state
            pass
        return False

    interval = max(60, int(settings.get("sweep_interval_seconds", 900) or 900))
    extra: dict[str, Any] = {}
    if first_run_delay is not None:
        extra["next_run_time"] = datetime.now(timezone.utc) + timedelta(
            seconds=max(0.0, first_run_delay)
        )
    scheduler.add_job(
        run_sweep,
        "interval",
        seconds=interval,
        jitter=min(int(interval * 0.1) or 1, 60),
        args=[config],
        id=DISCOVERY_JOB_ID,
        replace_existing=True,
        name="Subnet sweep",
        **extra,
    )
    return True


def discovery_next_run() -> datetime | None:
    """When the next automatic sweep is due, or None if none is scheduled.

    The Devices page shows this. "Is it scanning automatically?" should be
    answerable by looking at the page, not by watching it for a quarter of an
    hour to see whether anything happens.
    """
    scheduler = _scheduler
    if scheduler is None:
        return None
    job = scheduler.get_job(DISCOVERY_JOB_ID)
    return getattr(job, "next_run_time", None) if job is not None else None


def trigger_discovery_now(config) -> bool:  # type: ignore[no-untyped-def]
    """Queue a sweep to start immediately, and return without waiting.

    Deliberately not `await run_sweep(...)` inside the request. Two reasons,
    and the second one is a bug rather than a preference:

      * A sweep of a /24 takes seconds. Holding the HTTP response open for it
        makes the button feel broken.
      * The request already holds a write transaction -- resolving the session
        cookie updates its last-seen time -- and SQLite allows one writer. A
        sweep writing from a second session inside that request blocks on its
        own caller until the busy timeout, then fails with "database is
        locked".

    The sweep publishes an event when it finishes, so the page updates itself.
    """
    scheduler = _scheduler
    if scheduler is None:
        return False
    scheduler.add_job(
        run_sweep,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=1),
        args=[config],
        id="discovery:now",
        replace_existing=True,
        misfire_grace_time=60,
        name="Sweep now",
    )
    return True


def schedule_retention() -> bool:
    """Nightly downsample and prune.

    03:30 rather than 03:00 so it does not land on the same minute as the
    speed test, which deliberately saturates the WAN.
    """
    scheduler = _scheduler
    if scheduler is None:
        return False
    scheduler.add_job(
        run_retention,
        "cron",
        hour=3,
        minute=30,
        id=RETENTION_JOB_ID,
        replace_existing=True,
        misfire_grace_time=3600,   # a missed night is caught up on, not skipped
        name="Downsample and prune history",
    )
    return True


async def run_now(target_id: int) -> None:
    """Run a target's check immediately, outside its schedule.

    Used by the "Check now" button, so a person adding a target finds out
    whether it works in a second rather than at the top of the next interval.
    """
    await run_target(target_id)


def _check_type(target: Target) -> Any:
    return getattr(target.check_type, "value", target.check_type)
