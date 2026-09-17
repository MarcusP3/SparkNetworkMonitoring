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
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from .db import session_scope
from .engine.runner import run_target
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


async def run_now(target_id: int) -> None:
    """Run a target's check immediately, outside its schedule.

    Used by the "Check now" button, so a person adding a target finds out
    whether it works in a second rather than at the top of the next interval.
    """
    await run_target(target_id)


def _check_type(target: Target) -> Any:
    return getattr(target.check_type, "value", target.check_type)
