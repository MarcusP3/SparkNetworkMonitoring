"""Find SNMP devices: try the saved profiles against everything discovered.

SPARK polls only devices someone put on the SNMP list, and putting them there
meant knowing which of a few hundred addresses run an agent. This answers that
question on request, and only suggests: a device that answers is offered with
an Add button, never added. Polling something is a decision, and so is sending
it credentials every minute.

It runs only when the button is pressed, for a reason worth stating: a v2c
community string crosses the network in clear text, so this sends it to every
discovered device. That is the same exposure v2c always has, but spread
wider, and it should happen when someone chooses it rather than on a timer.

Each attempt is one GET of three scalars (name, object id, description),
read-only, with a short timeout and no retry. The profiles are tried in name
order and the first that answers wins; the rest are not tried on that device.
A device that answers with an authentication failure -- v3 only; v2c agents
drop a wrong community silently -- is recorded separately: it runs SNMP, but
none of the profiles is right for it.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from . import events
from .collectors import AuthFailed, SnmpCollector
from .collectors import oids as O
from .db import get_setting, save_setting, session_scope
from .models import Device, SnmpDevice, SnmpProfile, utcnow
from .snmp_config import ProfileError, add_device, credential_for
from .vault import SecretUnavailable, vault_for

log = logging.getLogger(__name__)

STATE_KEY = "snmp_discovery_state"
JOB_ID = "snmp-discovery:now"

CONCURRENCY = 50
TIMEOUT = 1.5
MAX_RESULTS = 500

# A run marked as running for longer than this was cut off by a restart, not
# still going. Without it one crash would leave the button disabled forever.
STALE_AFTER = timedelta(minutes=10)


def is_running(state: dict, now: datetime | None = None) -> bool:
    if not state.get("running"):
        return False
    try:
        started = datetime.fromisoformat(state["started_at"])
    except (KeyError, TypeError, ValueError):
        return False
    return (now or utcnow()) - started < STALE_AFTER


async def load_state(session) -> dict:  # type: ignore[no-untyped-def]
    return await get_setting(session, STATE_KEY)


async def mark_started(session) -> bool:  # type: ignore[no-untyped-def]
    """Record a search as running, before its job starts. False if one is.

    Done by the request that pressed the button, so the page it redirects to
    already says "Searching" rather than showing the previous result for a
    second and looking as if the button did nothing. The caller commits and
    then queues the job (scheduler.trigger_snmp_discovery).
    """
    state = await load_state(session)
    if is_running(state):
        return False
    await save_setting(session, STATE_KEY, {
        **state, "running": True, "started_at": utcnow().isoformat(),
    })
    return True


async def waiting(session, state: dict) -> tuple[dict[int, dict], dict[int, dict]]:  # type: ignore[no-untyped-def]
    """The last search's answers that still need someone: (found, refused).

    Keyed by device id, and filtered against the database now rather than
    trusted as saved -- a device added, removed or ignored since drops out
    without anyone pressing Find again.
    """
    rows = state.get("found", []) + state.get("refused", [])
    ids = {r.get("device_id") for r in rows}
    if not ids:
        return {}, {}
    listed = set((await session.execute(
        select(SnmpDevice.device_id).where(SnmpDevice.device_id.in_(ids))
    )).scalars())
    present = set((await session.execute(
        select(Device.id).where(Device.id.in_(ids), Device.ignored.is_(False))
    )).scalars())
    keep = present - listed

    def by_id(entries: list[dict]) -> dict[int, dict]:
        return {e["device_id"]: e for e in entries if e.get("device_id") in keep}

    return by_id(state.get("found", [])), by_id(state.get("refused", []))


async def add_all_found(session) -> int:  # type: ignore[no-untyped-def]
    """Put every device the last search found on the SNMP list, each with the
    profile it answered. Skips any listed, removed or ignored since. Returns
    how many were added; the caller commits and reschedules polling."""
    found, _ = await waiting(session, await load_state(session))
    added = 0
    for device_id, entry in found.items():
        try:
            await add_device(session, device_id, int(entry["profile_id"]))
        except (ProfileError, KeyError, TypeError, ValueError):
            continue
        added += 1
    return added


async def _ask(address: str, credential) -> tuple[str, dict | None]:  # type: ignore[no-untyped-def]
    """("found", details) / ("refused", None) / ("silent", None). Never raises."""
    collector = SnmpCollector(address, credential)
    try:
        values = await collector.get(O.SYS_NAME, O.SYS_OBJECT_ID, O.SYS_DESCR)
    except AuthFailed:
        return "refused", None
    except Exception:  # noqa: BLE001 - silence, timeouts and oddities alike
        return "silent", None
    finally:
        await collector.close()
    descr = values.get(O.SYS_DESCR)
    return "found", {
        "sys_name": values.get(O.SYS_NAME),
        "vendor": O.identify_vendor(values.get(O.SYS_OBJECT_ID)),
        "sys_descr": str(descr)[:160] if descr else None,
    }


async def run_discovery(config) -> dict:  # type: ignore[no-untyped-def]
    """Try every profile against every discovered device not already listed."""
    started = utcnow()
    try:
        return await _run(config, started)
    except Exception as exc:  # noqa: BLE001 - never take the scheduler down
        log.exception("SNMP discovery failed")
        state = {"running": False, "started_at": started.isoformat(),
                 "finished_at": utcnow().isoformat(),
                 "error": f"{type(exc).__name__}: {exc}", "found": [], "refused": []}
        async with session_scope() as session:
            await save_setting(session, STATE_KEY, state)
        events.publish({"kind": "snmp-discovery"})
        return state


async def _run(config, started: datetime) -> dict:  # type: ignore[no-untyped-def]
    # ---- read, then let go of the database for the whole sweep ----
    async with session_scope() as session:
        listed = set((await session.execute(select(SnmpDevice.device_id))).scalars())
        devices = [
            (d.id, d.primary_ip)
            for d in (await session.execute(
                select(Device).where(Device.ignored.is_(False), Device.primary_ip.isnot(None))
                .order_by(Device.id)
            )).scalars()
            if d.id not in listed
        ]
        profiles = list(
            (await session.execute(select(SnmpProfile).order_by(SnmpProfile.name))).scalars()
        )
        vault = vault_for(config)
        credentials = []
        for profile in profiles:
            try:
                credentials.append(
                    (profile.id, profile.name,
                     credential_for(profile, vault, timeout=TIMEOUT, retries=0))
                )
            except SecretUnavailable:
                continue
        state = {
            "running": True,
            "started_at": started.isoformat(),
            "devices": len(devices),
            "profiles": len(credentials),
            "found": [],
            "refused": [],
        }
        await save_setting(session, STATE_KEY, state)
    events.publish({"kind": "snmp-discovery"})

    gate = asyncio.Semaphore(CONCURRENCY)
    found: list[dict] = []
    refused: list[dict] = []

    async def one(device_id: int, address: str) -> None:
        refused_by: list[str] = []
        for profile_id, profile_name, credential in credentials:
            async with gate:
                outcome, details = await _ask(address, credential)
            if outcome == "found":
                found.append({"device_id": device_id, "address": address,
                              "profile_id": profile_id, "profile": profile_name, **details})
                return
            if outcome == "refused":
                refused_by.append(profile_name)
        if refused_by:
            refused.append({"device_id": device_id, "address": address,
                            "profiles": refused_by})

    await asyncio.gather(*(one(i, a) for i, a in devices))

    state.update(
        running=False,
        finished_at=utcnow().isoformat(),
        seconds=round((utcnow() - started).total_seconds(), 1),
        found=sorted(found, key=lambda r: r["device_id"])[:MAX_RESULTS],
        refused=sorted(refused, key=lambda r: r["device_id"])[:MAX_RESULTS],
    )
    async with session_scope() as session:
        await save_setting(session, STATE_KEY, state)
    log.info("SNMP discovery: %d device(s) tried, %d answered, %d refused the credentials",
             len(devices), len(found), len(refused))
    events.publish({"kind": "snmp-discovery"})
    return state
