"""Hysteresis and the incident lifecycle.

This is the part that decides whether SPARK is a tool you trust or one you mute
in a week. A single failed probe is not an outage -- a dropped packet, a
garbage-collecting web server, a switch busy with something else. A target goes
DOWN only after `failure_threshold` consecutive failures, and comes back only
after `recovery_threshold` consecutive successes.

Three states, and the asymmetry between them is deliberate:

  DOWN       hard, and hysteretic in both directions. Opens an incident.
  DEGRADED   soft. Reachable but impaired -- packet loss, an expiring
             certificate. Moves in and out immediately, because it is already
             the early warning and delaying an early warning defeats it. Never
             opens an incident.
  UP         the absence of the other two.

Incidents are rows, opened on the transition into DOWN and closed on the way
out, rather than something derived from raw results at query time. "Was it
down, and for how long" is the question you ask most, and deriving it from a
million check_result rows gets painful fast.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..checks.base import CheckOutcome
from ..models import CheckResult, HealthStatus, Incident, Severity, Target, utcnow

log = logging.getLogger(__name__)

# Why an incident closed. Stored on the row because a duration cannot tell
# "it came back" apart from "we stopped watching".
RECOVERED = "recovered"
PAUSED = "paused"
SUPERSEDED = "superseded"


@dataclass
class Transition:
    """What the state machine did, for the caller to log or alert on."""

    target_id: int
    previous: HealthStatus
    current: HealthStatus
    changed: bool = False
    incident_opened: Incident | None = None
    incident_closed: Incident | None = None
    suppressed_by_dependency: bool = False

    @property
    def recovered(self) -> bool:
        return self.changed and self.previous == HealthStatus.DOWN

    @property
    def went_down(self) -> bool:
        return self.changed and self.current == HealthStatus.DOWN


def observed_status(outcome: CheckOutcome) -> HealthStatus:
    if not outcome.ok:
        return HealthStatus.DOWN
    return HealthStatus.DEGRADED if outcome.degraded else HealthStatus.UP


async def apply_outcome(
    session: AsyncSession, target: Target, outcome: CheckOutcome
) -> Transition:
    """Record one probe against a target and move its state machine.

    Writes the check_result row, updates the counters, and opens or closes an
    incident if a hard transition happened. The caller commits.
    """
    observed = observed_status(outcome)
    previous = _as_status(target.status)
    now = utcnow()

    session.add(
        CheckResult(
            target_id=target.id,
            ts=now,
            status=observed,
            latency_ms=outcome.latency_ms,
            detail=outcome.detail,
        )
    )

    if observed is HealthStatus.DOWN:
        target.consecutive_failures += 1
        target.consecutive_successes = 0
    else:
        target.consecutive_successes += 1
        target.consecutive_failures = 0

    target.last_checked_at = now

    transition = Transition(target_id=target.id, previous=previous, current=previous)

    if observed is HealthStatus.DOWN:
        # Hard failure: only believe it after enough consecutive failures.
        if previous is not HealthStatus.DOWN:
            if target.consecutive_failures >= max(1, target.failure_threshold):
                transition.current = HealthStatus.DOWN
                transition.changed = True
    else:
        if previous is HealthStatus.DOWN:
            # Recovering from a hard failure is hysteretic too: a flapping
            # target that answers once should not close its incident.
            if target.consecutive_successes >= max(1, target.recovery_threshold):
                transition.current = observed
                transition.changed = True
        elif previous is not observed:
            # UP <-> DEGRADED. Soft, immediate, no incident either way.
            transition.current = observed
            transition.changed = True

    if not transition.changed:
        return transition

    target.status = transition.current
    target.last_status_change = now

    if transition.went_down:
        suppressed = await _dependency_is_down(session, target)
        transition.suppressed_by_dependency = suppressed

        # An incident already open means this target never stopped being down
        # as far as the record is concerned -- most likely it was paused while
        # down and then resumed, which resets its status to UNKNOWN and makes
        # the next failure look like a fresh transition. Opening a second row
        # would claim it was down twice at once. Reuse the one that is open.
        existing = await _open_incident(session, target.id)
        if existing is not None:
            log.info(
                "%s is DOWN again, continuing the incident opened at %s",
                target.name, existing.opened_at,
            )
            transition.incident_opened = None
            return transition

        incident = Incident(
            target_id=target.id,
            opened_at=now,
            severity=Severity.CRITICAL,
            cause=outcome.detail,
            suppressed_by_dependency=suppressed,
        )
        session.add(incident)
        transition.incident_opened = incident
        log.info(
            "%s is DOWN after %d consecutive failures (%s)%s",
            target.name,
            target.consecutive_failures,
            outcome.detail,
            " [suppressed: dependency is down]" if suppressed else "",
        )
    elif transition.recovered:
        # All of them, not just the newest: a database that already contains
        # overlapping open incidents would otherwise keep the older ones open
        # forever, and they would read as ongoing outages years from now.
        closed = await close_open_incidents(session, target.id, when=now,
                                            resolution=RECOVERED)
        if closed:
            transition.incident_closed = closed[0]
            log.info(
                "%s recovered after %s",
                target.name,
                human_duration(closed[0].duration_seconds),
            )
        else:
            log.info("%s recovered", target.name)

    return transition


async def close_open_incidents(
    session: AsyncSession,
    target_id: int,
    *,
    resolution: str,
    when=None,  # type: ignore[no-untyped-def]
) -> list[Incident]:
    """Close every open incident for a target, newest first.

    Pausing a target calls this. An incident whose target nobody is checking
    has no knowable end, so leaving it open makes the dashboard report an
    outage that grows for as long as the pause lasts -- and the resumed target
    starts from UNKNOWN, so recovery never closes it either. Closing it at the
    moment monitoring stopped is the only honest answer available.
    """
    when = when or utcnow()
    incidents = list(
        (
            await session.execute(
                select(Incident)
                .where(Incident.target_id == target_id, Incident.closed_at.is_(None))
                .order_by(Incident.opened_at.desc())
            )
        )
        .scalars()
        .all()
    )
    for index, incident in enumerate(incidents):
        incident.closed_at = when
        # Only the newest ended now; anything older was already a duplicate.
        incident.resolution = resolution if index == 0 else SUPERSEDED
    return incidents


async def _dependency_is_down(session: AsyncSession, target: Target) -> bool:
    """Is this failure a symptom of something upstream already being down.

    The switch goes down and thirty hosts behind it go with it. One alert is
    news; thirty is a reason to mute the tool. The incident is still recorded --
    you want the history -- it is just flagged so the notifier stays quiet.
    """
    if target.depends_on_target_id is None:
        return False
    parent_status = await session.scalar(
        select(Target.status).where(Target.id == target.depends_on_target_id)
    )
    return _as_status(parent_status) is HealthStatus.DOWN


async def _open_incident(session: AsyncSession, target_id: int) -> Incident | None:
    return await session.scalar(
        select(Incident)
        .where(Incident.target_id == target_id, Incident.closed_at.is_(None))
        .order_by(Incident.opened_at.desc())
    )


def _as_status(value: object) -> HealthStatus:
    """Tolerate a bare string from an older row or a hand-edited database."""
    if isinstance(value, HealthStatus):
        return value
    try:
        return HealthStatus(str(value))
    except ValueError:
        return HealthStatus.UNKNOWN


def human_duration(seconds: float | None) -> str:
    if seconds is None:
        return "an unknown time"
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"
