"""The scheduled sweep.

Mirrors engine/runner.py: owns its session, contains its own failures, and
publishes only after the transaction has committed.
"""

from __future__ import annotations

import logging

from .. import events
from ..db import get_setting, session_scope
from .store import record_all
from .sweep import sweep_all

log = logging.getLogger(__name__)

JOB_ID = "discovery:sweep"


async def run_sweep(config) -> None:  # type: ignore[no-untyped-def]
    """Sweep every configured subnet and record what answered."""
    new_names: list[str] = []
    try:
        async with session_scope() as session:
            settings = await get_setting(session, "discovery")
            if not settings.get("enabled", True):
                log.debug("Discovery is disabled in settings; skipping sweep")
                return

        observations = await sweep_all(config.network.subnets)
        if not observations:
            return

        async with session_scope() as session:
            seen, new = await record_all(session, observations)
            # Read the names inside the session, before it closes.
            new_names = [device.display_name for device in new]
        log.info("Discovery: %d device(s) seen, %d new", seen, len(new_names))
    except Exception:  # noqa: BLE001 - a failed sweep must not stop the scheduler
        log.exception("Discovery sweep failed")
        return

    if new_names:
        for name in new_names:
            log.info("New device on the network: %s", name)
        events.publish({"kind": "devices", "new": len(new_names)})
