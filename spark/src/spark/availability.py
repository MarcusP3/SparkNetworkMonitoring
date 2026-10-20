"""Availability: how much of a stretch of time each target was up.

Built from the incident rows -- the reason they are rows (DESIGN.md) -- and
the check history, over a range: the last 24 hours to a year, this month or
last. For each target:

  * **down**: the time its own outages overlapped the range. An outage
    still open counts up to now.
  * **unreachable**: outages explained by something it depends on, or by a
    device above it on the map, being down. The switch's outage, not the
    server's; it is shown, and counted neither up nor down.
  * **planned**: the part of an outage inside a maintenance window on its
    device (or one above it). Also counted neither way. An outage that
    began before a window, or outlasted it, is unplanned outside it.
  * **watched**: the time there are checks for, from the check history at
    whatever width it is kept (raw for a week, five-minute buckets for 90
    days, hourly for two years). A target paused, or SPARK switched off, is
    not watched, so it is neither up nor down for that time.

Up is the share of watched time that was not down, leaving out what was
unreachable or planned. The internet gets the same treatment from its own
checks (kept 30 days) and "the internet is down" incidents.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import maintenance
from .models import (FIVE_MINUTES, ONE_HOUR, AlertIncident, CheckResult, CheckRollup, Device,
                     Incident, InternetSample, Target, utcnow)

# The ranges the page offers, in the order it shows them.
RANGES = {
    "24h": "24 hours",
    "7d": "7 days",
    "30d": "30 days",
    "90d": "90 days",
    "365d": "A year",
    "month": "This month",
    "last-month": "Last month",
}
DEFAULT_RANGE = "30d"


def span(name: str, now: datetime, tz) -> tuple[datetime, datetime]:  # type: ignore[no-untyped-def]
    """The range's start and end, in UTC. Months are the local calendar's."""
    if name.endswith(("h", "d")) and name[:-1].isdigit():
        size = timedelta(hours=int(name[:-1])) if name.endswith("h") else timedelta(days=int(name[:-1]))
        return now - size, now
    local = now.astimezone(tz)
    first = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if name == "month":
        return first.astimezone(timezone.utc), now
    if name == "last-month":
        year, month = (first.year - 1, 12) if first.month == 1 else (first.year, first.month - 1)
        start = first.replace(year=year, month=month)
        return start.astimezone(timezone.utc), first.astimezone(timezone.utc)
    return span(DEFAULT_RANGE, now, tz)


@dataclass
class Availability:
    watched: float = 0.0        # seconds there are checks for
    down: float = 0.0           # its own outages, unplanned
    unreachable: float = 0.0    # explained by something above it being down
    planned: float = 0.0        # inside a maintenance window
    outages: int = 0            # its own, unplanned, that touched the range
    longest: float = 0.0
    durations: list[float] = field(default_factory=list)
    length: float = 0.0         # the range itself

    @property
    def judged(self) -> float:
        """Watched time that counts: less what was unreachable or planned."""
        return max(self.watched - self.unreachable - self.planned, 0.0)

    @property
    def percent(self) -> float | None:
        if self.judged <= 0:
            return None
        return max(0.0, min(100.0, 100.0 * (1 - self.down / self.judged)))

    @property
    def mean_to_recover(self) -> float | None:
        return sum(self.durations) / len(self.durations) if self.durations else None

    @property
    def coverage(self) -> float | None:
        """The share of the range there are checks for."""
        return min(100.0, 100.0 * self.watched / self.length) if self.length > 0 else None


def _overlap(a0: datetime, a1: datetime, b0: datetime, b1: datetime) -> float:
    return max(0.0, (min(a1, b1) - max(a0, b0)).total_seconds())


async def _add(session: AsyncSession, result: Availability, opened: datetime,
               closed: datetime | None, *, device_id: int | None, explained: bool,
               start: datetime, end: datetime, now: datetime) -> None:
    """One outage's share of the range, sorted into down, unreachable or
    planned."""
    until = min(closed or now, now)
    inside = _overlap(opened, until, start, end)
    if inside <= 0:
        return
    if explained:
        result.unreachable += inside
        return
    held = await maintenance.spans_over(session, device_id, opened, until) if device_id else []
    # The part in the range under a window, and all of it under one.
    planned = sum(_overlap(max(opened, h.start), min(until, h.end), start, end) for h in held)
    held_for = sum(_overlap(opened, until, h.start, h.end) for h in held)
    result.planned += planned
    unplanned = inside - planned
    if unplanned > 0:
        result.down += unplanned
        result.outages += 1
        # How long it lasted, all of it, not just the part in the range.
        whole = (until - opened).total_seconds() - held_for
        result.durations.append(whole)
        result.longest = max(result.longest, whole)


async def _watched(session: AsyncSession, start: datetime, end: datetime) -> dict[int, float]:
    """Seconds of check history per target in the range, at every width."""
    seconds: dict[int, float] = {}
    intervals = dict((await session.execute(select(Target.id, Target.interval_seconds))).all())
    for target_id, count in (await session.execute(
            select(CheckResult.target_id, func.count(CheckResult.id))
            .where(CheckResult.ts >= start, CheckResult.ts < end)
            .group_by(CheckResult.target_id))).all():
        seconds[target_id] = count * float(intervals.get(target_id) or 15)
    for width in (FIVE_MINUTES, ONE_HOUR):
        for target_id, count in (await session.execute(
                select(CheckRollup.target_id, func.count(CheckRollup.id))
                .where(CheckRollup.bucket_seconds == width, CheckRollup.samples > 0,
                       CheckRollup.bucket_start >= start, CheckRollup.bucket_start < end)
                .group_by(CheckRollup.target_id))).all():
            seconds[target_id] = seconds.get(target_id, 0.0) + count * width
    limit = (end - start).total_seconds()
    return {k: min(v, limit) for k, v in seconds.items()}


@dataclass
class Row:
    target: Target
    device: Device | None
    figures: Availability


async def targets(session: AsyncSession, start: datetime, end: datetime,
                  now: datetime | None = None) -> list[Row]:
    """Every target with any check history in the range, worst first."""
    now = now or utcnow()
    watched = await _watched(session, start, end)
    rows: dict[int, Row] = {}
    for target, device in (await session.execute(
            select(Target, Device).outerjoin(Device, Device.id == Target.device_id))).all():
        if watched.get(target.id):
            rows[target.id] = Row(target, device, Availability(
                watched=watched[target.id], length=(end - start).total_seconds()))
    if not rows:
        return []
    for incident in (await session.execute(select(Incident).where(
            Incident.target_id.in_(rows), Incident.opened_at < end,
            or_(Incident.closed_at.is_(None), Incident.closed_at > start)))).scalars():
        row = rows[incident.target_id]
        await _add(session, row.figures, incident.opened_at, incident.closed_at,
                   device_id=row.target.device_id, explained=incident.suppressed_by_dependency,
                   start=start, end=end, now=now)
    return sorted(rows.values(), key=lambda r: (
        r.figures.percent if r.figures.percent is not None else 101.0, r.target.name.lower()))


async def internet(session: AsyncSession, start: datetime, end: datetime,
                   now: datetime | None = None) -> Availability | None:
    """The internet, from its own checks and outages. None without checks."""
    from .internet import ALERT_KEY, INTERVAL_SECONDS

    now = now or utcnow()
    checks = await session.scalar(select(func.count(InternetSample.id)).where(
        InternetSample.ts >= start, InternetSample.ts < end)) or 0
    if not checks:
        return None
    length = (end - start).total_seconds()
    result = Availability(watched=min(checks * INTERVAL_SECONDS, length), length=length)
    gateway = await maintenance.gateway_id(session)
    for row in (await session.execute(select(AlertIncident).where(
            AlertIncident.key == ALERT_KEY, AlertIncident.opened_at < end,
            or_(AlertIncident.closed_at.is_(None), AlertIncident.closed_at > start)))).scalars():
        await _add(session, result, row.opened_at, row.closed_at, device_id=gateway,
                   explained=False, start=start, end=end, now=now)
    return result


def month_name(when: datetime, tz) -> str:  # type: ignore[no-untyped-def]
    local = when.astimezone(tz)
    return f"{calendar.month_name[local.month]} {local.year}"


__all__ = ["Availability", "DEFAULT_RANGE", "RANGES", "Row", "internet", "span", "targets"]
