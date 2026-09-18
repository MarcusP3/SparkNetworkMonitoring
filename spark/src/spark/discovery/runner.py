"""The scheduled sweep.

Mirrors engine/runner.py: owns its session, contains its own failures, and
publishes only after the transaction has committed.

It also records what the sweep did, so an empty Devices page can say why it is
empty. "Swept 254 addresses, none answered" and "ICMP unavailable" and "never
ran" are three different problems, and an empty list looks identical for all
three.
"""

from __future__ import annotations

import logging

from .. import events
from ..db import get_setting, save_setting, session_scope
from .store import record_all
from .sweep import SweepReport, sweep_all

log = logging.getLogger(__name__)

JOB_ID = "discovery:sweep"

# Stored in the settings table rather than a table of its own: it is one small
# JSON blob, and a diagnostic is a poor reason to make this project run its
# first schema migration.
STATE_KEY = "discovery_state"


async def last_sweep(session) -> dict:  # type: ignore[no-untyped-def]
    """What the previous sweep did, or {} if none has run."""
    return await get_setting(session, STATE_KEY)


async def run_sweep(config) -> None:  # type: ignore[no-untyped-def]
    """Sweep every configured subnet and record what answered."""
    new_names: list[str] = []
    report: SweepReport | None = None

    try:
        async with session_scope() as session:
            settings = await get_setting(session, "discovery")
            if not settings.get("enabled", True):
                log.debug("Discovery is disabled in settings; skipping sweep")
                return

        report = await sweep_all(config.network.subnets)

        async with session_scope() as session:
            seen, new = await record_all(session, report.observations)
            # Read the names inside the session, before it closes.
            new_names = [device.display_name for device in new]
            summary = report.as_dict()
            summary["new"] = len(new_names)
            await save_setting(session, STATE_KEY, summary)

        log.info(
            "Discovery: probed %d, %d answered, %d new",
            report.probed, report.answered, len(new_names),
        )
    except Exception as exc:  # noqa: BLE001 - a failed sweep must not stop the scheduler
        log.exception("Discovery sweep failed")
        # Record the failure too. A sweep that crashes silently is the worst
        # of the three cases, because the page looks the same as never having
        # run and nothing suggests looking at the logs.
        try:
            async with session_scope() as session:
                summary = (report.as_dict() if report else SweepReport().as_dict())
                summary["error"] = f"{type(exc).__name__}: {exc}"
                summary["new"] = 0
                await save_setting(session, STATE_KEY, summary)
        except Exception:  # noqa: BLE001
            log.debug("Could not record the failed sweep", exc_info=True)
        return

    if new_names:
        for name in new_names:
            log.info("New device on the network: %s", name)
    events.publish({"kind": "devices", "new": len(new_names)})
