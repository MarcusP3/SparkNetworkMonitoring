"""Running one target's check, end to end.

Deliberately the only place that joins the pure check functions to the
database. The scheduler calls `run_target(id)` and nothing else; everything it
needs -- its own session, its own error containment -- it sets up here, so a
failure polling one target can never take down the scheduler or leak into
another target's transaction.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from ..checks.base import CheckOutcome, CheckSpec
from ..checks.net import run_check
from ..db import session_scope
from ..models import Target
from .state import Transition, apply_outcome

log = logging.getLogger(__name__)


def spec_for(target: Target) -> CheckSpec:
    return CheckSpec(
        address=target.address,
        timeout_seconds=float(target.timeout_seconds or 5.0),
        params=dict(target.params or {}),
    )


async def check_target(session: AsyncSession, target: Target) -> tuple[CheckOutcome, Transition]:
    """Probe one target and move its state machine, in the caller's session."""
    check_type = getattr(target.check_type, "value", str(target.check_type))
    outcome = await run_check(check_type, spec_for(target))
    transition = await apply_outcome(session, target, outcome)
    return outcome, transition


async def run_target(target_id: int) -> None:
    """Scheduler entry point. Owns its session and swallows everything.

    An exception escaping here would be logged by APScheduler and the job would
    keep running, but a half-applied transaction would not roll back cleanly,
    so containment is explicit.
    """
    try:
        async with session_scope() as session:
            target = await session.get(Target, target_id)
            if target is None:
                log.debug("Target %s vanished before its check ran", target_id)
                return
            if not target.enabled:
                return
            await check_target(session, target)
    except Exception:  # noqa: BLE001 - one bad target must not stop the engine
        log.exception("Check for target %s failed unexpectedly", target_id)
