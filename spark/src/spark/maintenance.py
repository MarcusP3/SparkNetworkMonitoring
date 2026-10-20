"""Maintenance windows: a device being worked on, on purpose.

A window is one device -- and, unless told otherwise, everything below it on
the network map -- for a stretch of time, once or every week at the same
local time. While one is on, alerts about what it covers are held, the way
the mute list holds them: everything is still checked, polled, recorded and
shown, and incidents that open read "during maintenance", but nothing is
sent and they do not turn the counts red.

When a window ends, whatever it covers that is still broken is sent then.
An update that left the box dead is news the moment the window closes, and
it is the one thing a monitor that went quiet for the window must not miss.
Each is sent the way it would have been (an outage as "... is still down
after maintenance"), and marked as sent, so its recovery follows as usual.

Two choices worth knowing:

  * A recovery during a window is not sent either, even for an outage
    alerted before it began: you are working on it.
  * A device under two windows at once is dealt with when the last ends.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import alerts, events, prefs
from .db import session_scope
from .hierarchy import ancestors, descendants
from .models import (AlertIncident, AlertMute, AlertState, Device, Incident, MaintenanceWindow,
                     Target, utcnow)

log = logging.getLogger(__name__)

JOB_ID = "maintenance:finish"
INTERVAL_SECONDS = 60
FIRST_RUN_SECONDS = 20

WEEK = timedelta(days=7)
# Long enough for a rebuild, short enough that a forgotten window is not a
# month of silence. A weekly one must end before the next begins.
LONGEST = timedelta(days=14)
LONGEST_WEEKLY = timedelta(days=1)
# The quick choices on a device's page, in minutes.
DURATIONS = (30, 60, 120, 240, 480, 1440)


@dataclass(frozen=True)
class Span:
    start: datetime
    end: datetime

    def holds(self, when: datetime) -> bool:
        return self.start <= when < self.end


def _index(window: MaintenanceWindow, when: datetime, tz) -> int:  # type: ignore[no-untyped-def]
    """Which weekly occurrence `when` falls in or after: 0 for the first,
    -1 before it. Counted on the local clock, so 03:00 stays 03:00 across a
    daylight-saving change."""
    first = window.starts_at.astimezone(tz).replace(tzinfo=None)
    local = when.astimezone(tz).replace(tzinfo=None)
    return -1 if local < first else (local - first) // WEEK


def _occurrence(window: MaintenanceWindow, index: int, tz) -> Span:  # type: ignore[no-untyped-def]
    length = window.ends_at - window.starts_at
    first = window.starts_at.astimezone(tz).replace(tzinfo=None)
    start = (first + index * WEEK).replace(tzinfo=tz).astimezone(timezone.utc)
    return Span(start, start + length)


def span_at(window: MaintenanceWindow, when: datetime, tz) -> Span:  # type: ignore[no-untyped-def]
    """The occurrence on at `when`, else the latest begun before it, else
    the first."""
    if not window.weekly:
        return Span(window.starts_at, window.ends_at)
    return _occurrence(window, max(0, _index(window, when, tz)), tz)


def last_ended(window: MaintenanceWindow, now: datetime, tz) -> Span | None:  # type: ignore[no-untyped-def]
    """The latest occurrence that has ended by `now`, if any has."""
    if not window.weekly:
        return Span(window.starts_at, window.ends_at) if window.ends_at <= now else None
    index = _index(window, now, tz)
    if index < 0:
        return None
    current = _occurrence(window, index, tz)
    if current.end <= now:
        return current
    return _occurrence(window, index - 1, tz) if index > 0 else None


def next_span(window: MaintenanceWindow, now: datetime, tz) -> Span | None:  # type: ignore[no-untyped-def]
    """The occurrence on now, or the next to come; None for a one-off that
    is over."""
    if not window.weekly:
        return None if window.ends_at <= now else Span(window.starts_at, window.ends_at)
    current = span_at(window, now, tz)
    if current.end > now:
        return current
    return _occurrence(window, _index(window, now, tz) + 1, tz)


async def zone(session: AsyncSession):  # type: ignore[no-untyped-def]
    return prefs.zone_of(await prefs.get_timezone(session))


async def _candidates(session: AsyncSession, when: datetime) -> list[MaintenanceWindow]:
    """Windows that could be on at `when`: begun by then, and weekly or not
    yet over. Usually none, which keeps every alert's check to one query."""
    return list((await session.execute(select(MaintenanceWindow).where(
        MaintenanceWindow.starts_at <= when,
        or_(MaintenanceWindow.weekly.is_(True), MaintenanceWindow.ends_at > when),
    ))).scalars())


async def covering(session: AsyncSession, device_id: int | None,
                   when: datetime | None = None) -> MaintenanceWindow | None:
    """The window this device is under at `when` (now if not given): its
    own, or one on a device above it that takes in what is below."""
    if device_id is None:
        return None
    when = when or utcnow()
    candidates = await _candidates(session, when)
    if not candidates:
        return None
    tz = await zone(session)
    on = [w for w in candidates if span_at(w, when, tz).holds(when)]
    for window in on:
        if window.device_id == device_id:
            return window
    if any(w.include_below for w in on):
        above = set(await ancestors(session, device_id))
        for window in on:
            if window.include_below and window.device_id in above:
                return window
    return None


async def spans_over(session: AsyncSession, device_id: int, start: datetime,
                     end: datetime) -> list[Span]:
    """Every stretch from `start` to `end` that this device was under a
    window, its own or one above it that takes in what is below, merged."""
    above = set(await ancestors(session, device_id))
    windows = [w for w in (await session.execute(select(MaintenanceWindow).where(
        MaintenanceWindow.starts_at < end,
        or_(MaintenanceWindow.weekly.is_(True), MaintenanceWindow.ends_at > start),
    ))).scalars() if w.device_id == device_id or (w.include_below and w.device_id in above)]
    if not windows:
        return []
    tz = await zone(session)
    found: list[Span] = []
    for window in windows:
        if not window.weekly:
            found.append(Span(window.starts_at, window.ends_at))
            continue
        for index in range(max(0, _index(window, start, tz)), _index(window, end, tz) + 1):
            found.append(_occurrence(window, index, tz))
    merged: list[Span] = []
    for piece in sorted((s for s in found if s.end > start and s.start < end),
                        key=lambda s: s.start):
        if merged and piece.start <= merged[-1].end:
            merged[-1] = Span(merged[-1].start, max(merged[-1].end, piece.end))
        else:
            merged.append(piece)
    return merged


async def gateway_id(session: AsyncSession) -> int | None:
    """The gateway's device: "the internet is down" belongs to it."""
    from .internet import _gateway

    gateway = await _gateway(session)
    return gateway.id if gateway else None


# --------------------------------------------------------------------------
# Making and ending one
# --------------------------------------------------------------------------


class MaintenanceError(ValueError):
    """A window that cannot be saved, with a message for the form."""


async def add(session: AsyncSession, *, device_id: int, starts_at: datetime, ends_at: datetime,
              weekly: bool, include_below: bool, note: str,
              now: datetime | None = None) -> MaintenanceWindow:
    now = now or utcnow()
    if await session.get(Device, device_id) is None:
        raise MaintenanceError("Choose a device.")
    if ends_at <= starts_at:
        raise MaintenanceError("The end has to be after the start.")
    length = ends_at - starts_at
    if weekly and length > LONGEST_WEEKLY:
        raise MaintenanceError("A weekly window can be at most a day long.")
    if not weekly and length > LONGEST:
        raise MaintenanceError("A window can be at most 14 days long.")
    if not weekly and ends_at <= now:
        raise MaintenanceError("That window is already over.")
    window = MaintenanceWindow(device_id=device_id, starts_at=starts_at, ends_at=ends_at,
                               weekly=weekly, include_below=include_below,
                               note=note.strip() or None)
    # A weekly window first dated in the past starts with its next
    # occurrence: an end before it existed is not one to report on.
    if weekly:
        before = last_ended(window, now, await zone(session))
        window.finished_until = before.end if before else None
    session.add(window)
    await session.flush()
    return window


async def end_now(session: AsyncSession, window: MaintenanceWindow,
                  now: datetime | None = None) -> bool:
    """Stop a one-off window that is on, and send what it leaves behind
    now rather than at the next minute's run."""
    now = now or utcnow()
    if window.weekly or not window.starts_at <= now < window.ends_at:
        return False
    window.ends_at = now
    await finish(session, now)
    return True


async def remove(session: AsyncSession, window: MaintenanceWindow,
                 now: datetime | None = None) -> None:
    """Take a window away. One that is on is ended first, so what it was
    holding back is still sent."""
    now = now or utcnow()
    current = span_at(window, now, await zone(session))
    if current.holds(now):
        await _leftovers(session, window, Span(current.start, now), now)
    await session.delete(window)


# --------------------------------------------------------------------------
# When one ends
# --------------------------------------------------------------------------


async def finish(session: AsyncSession, now: datetime | None = None) -> int:
    """Deal with every window that has ended since it was last looked at.
    Returns how many ends that was."""
    now = now or utcnow()
    tz = await zone(session)
    count = 0
    windows = (await session.execute(select(MaintenanceWindow).where(
        MaintenanceWindow.starts_at <= now,
        or_(MaintenanceWindow.weekly.is_(True), MaintenanceWindow.finished_until.is_(None)),
    ))).scalars().all()
    for window in windows:
        ended = last_ended(window, now, tz)
        if ended is None or (window.finished_until is not None
                             and window.finished_until >= ended.end):
            continue
        window.finished_until = ended.end
        await _leftovers(session, window, ended, now)
        count += 1
    return count


async def _covered(session: AsyncSession, window: MaintenanceWindow) -> set[int]:
    devices = {window.device_id}
    if window.include_below:
        parents = dict((await session.execute(select(Device.id, Device.parent_device_id))).all())
        devices |= descendants(parents, window.device_id)
    return devices


async def _leftovers(session: AsyncSession, window: MaintenanceWindow, ended: Span,
                     now: datetime) -> int:
    """Send what is still broken on the devices `window` covered."""
    from . import suppressions
    from .engine.state import human_duration

    settings = await alerts.load(session)
    if not settings.get("enabled", True):
        return 0
    devices = await _covered(session, window)
    muted = set((await session.execute(select(AlertMute.device_id).where(
        AlertMute.device_id.is_not(None)))).scalars())
    devices -= muted
    # Still under another window: that one's end will say it.
    for device_id in list(devices):
        other = await covering(session, device_id, now)
        if other is not None and other.id != window.id:
            devices.discard(device_id)
    if not devices:
        return 0
    owner = await session.get(Device, window.device_id)
    where = owner.display_name if owner else "a device"
    finished = f"Maintenance on {where} ended {human_duration((now - ended.end).total_seconds())} ago." \
        if now - ended.end > timedelta(minutes=2) else f"Maintenance on {where} has ended."
    sent = 0

    # Outages: sent as the down message they would have been, under the
    # same key, so "back up" follows when they recover.
    muted_targets = set((await session.execute(select(AlertMute.target_id).where(
        AlertMute.target_id.is_not(None)))).scalars())
    for incident, target in (await session.execute(
        select(Incident, Target).join(Target, Target.id == Incident.target_id).where(
            Incident.closed_at.is_(None), Incident.suppressed_by_dependency.is_(False),
            Target.device_id.in_(devices), Target.enabled.is_(True))
    )).all():
        if target.id in muted_targets or (target.muted_until and target.muted_until > now):
            continue
        key = f"incident:{incident.id}:down"
        if await alerts._queued(session, key):
            continue
        await alerts.enqueue(
            session, kind="down", tone="bad",
            subject=f"{target.name} is still down after maintenance",
            body=f"`{target.address}` — down for {human_duration((now - incident.opened_at).total_seconds())}.\n{finished}",
            incident_id=incident.id, dedupe_key=key)
        sent += 1

    # Alert rules: the ones that fired silently. Marked notified, so their
    # recovery is sent when they clear.
    gateway = await gateway_id(session)
    hidden = await suppressions.hidden_on_dashboard(session)
    clauses = [AlertIncident.device_id.in_(devices)]
    if gateway in devices:
        clauses.append(AlertIncident.key == "internet")
    for row in (await session.execute(select(AlertIncident).where(
            AlertIncident.closed_at.is_(None), or_(*clauses)))).scalars():
        rule = suppressions.rule_of(row.key)
        klass = (await suppressions.klass_of(session, row.key, row.detail)
                 if rule == "truenas_alerts" else None)
        if hidden(row, rule, klass):
            continue
        body = "\n".join(x for x in (row.detail, finished) if x)
        if row.key.startswith("snmpdown:"):
            # SNMP silence keys its messages by when the device last answered.
            key = f"snmp:{row.key.split(':', 1)[1]}:{row.opened_at.isoformat()}:down"
            if await alerts._queued(session, key):
                continue
            await alerts.enqueue(session, kind="snmp_down", tone="bad", subject=row.title,
                                 body=body, dedupe_key=key)
        else:
            state = await session.get(AlertState, row.key)
            if state is None or not state.fired or state.notified:
                continue
            await alerts.enqueue(session, kind="maintenance_left", tone="bad", subject=row.title,
                                 body=body,
                                 dedupe_key=f"maintenance:{window.id}:{ended.end.isoformat()}:{row.key}")
            state.notified = True
        sent += 1
    return sent


async def run(config=None) -> int:  # type: ignore[no-untyped-def]
    """The scheduler's job, every minute. Never raises."""
    try:
        async with session_scope() as session:
            ended = await finish(session)
        if ended:
            events.publish({"kind": "maintenance"})   # what it held turns red again
        return ended
    except Exception:  # noqa: BLE001 - a failed run is retried in a minute
        log.exception("Ending maintenance windows failed")
        return 0
