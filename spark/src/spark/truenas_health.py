"""Drive health and TrueNAS's own alerts, from the TrueNAS API.

What SNMP cannot say: whether each drive is ONLINE in its pool and how many
read, write and checksum errors ZFS has counted on it, and what TrueNAS is
itself warning about (SMART failures among them). Read by the 5-minute
credential check over the same login (truenas.read_storage), stored on the
credential, and decided here in the same transaction.

Alerts (Settings -> Alerts -> APIs; the mute list applies):

  * a drive in a pool that is not ONLINE, or has any read, write or checksum
    errors: at once, and again once it is ONLINE with no errors (the counts
    go back to 0 when the pool is cleared in TrueNAS);
  * each TrueNAS alert at WARNING or above, once, when it appears, and again
    when TrueNAS no longer lists it (or it is dismissed there). INFO and
    NOTICE are shown on the device page only.

A part TrueNAS did not answer is not decided on: a failed read is not the
all-clear. Disks outside any pool (a boot stick) have no ZFS status.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import alerts, snmp_alerts, suppressions
from .engine.state import human_duration
from .models import AlertState, ApiCredential, Device, utcnow
from .storage import fmt_bytes
from .truenas import LEVELS

INTERVAL_SECONDS = 5 * 60
ONLINE = "ONLINE"
FORWARD_FROM = "WARNING"
BAD_FROM = "ERROR"


def _rank(level: str | None) -> int:
    return LEVELS.index(level) if level in LEVELS else 0


def forwarded(level: str | None) -> bool:
    return _rank(level) >= _rank(FORWARD_FROM)


def errors(drive: dict) -> int:
    return drive.get("read_errors", 0) + drive.get("write_errors", 0) + drive.get("checksum_errors", 0)


def healthy(drive: dict) -> bool:
    return drive.get("status") == ONLINE and errors(drive) == 0


def _drive_prefix(cred_id: int) -> str:
    return f"apidrive:{cred_id}:"


def _alert_prefix(cred_id: int) -> str:
    return f"tnalert:{cred_id}:"


async def forget(session: AsyncSession, cred_id: int) -> None:
    for prefix in (_drive_prefix(cred_id), _alert_prefix(cred_id)):
        await session.execute(delete(AlertState).where(AlertState.key.startswith(prefix)))
        await snmp_alerts.close_incidents(session, utcnow(), prefix=prefix,
                                          resolution=snmp_alerts.GONE)


async def context(session: AsyncSession, row: ApiCredential) -> tuple[str, bool, bool, dict]:
    """(what to call it, whether to send, whether to send recoveries, rules)."""
    settings = await alerts.load(session)
    device = await session.get(Device, row.device_id) if row.device_id else None
    sending = settings.get("enabled", True) and not (
        device is not None and await alerts.is_muted(session, device_id=device.id))
    return (device.display_name if device else row.name, sending,
            settings.get("notify_on_recovery", True), await snmp_alerts.load(session))


async def _keys(session: AsyncSession, prefix: str) -> set[str]:
    return set((await session.execute(
        select(AlertState.key).where(AlertState.key.startswith(prefix)))).scalars())


async def evaluate(session: AsyncSession, row: ApiCredential, before: dict,
                   now: datetime) -> None:
    """Every drive and TrueNAS alert in the reading just stored. `before` is
    the reading it replaced, for the words of an alert TrueNAS has dropped."""
    readings = row.readings or {}
    name, sending, recoveries, rules = await context(session, row)
    supp = await suppressions.for_device(session, row.device_id)
    address = f"`{row.host}`"

    async def run(key: str, *, breached: bool, on: bool, kind: str,
                  fire: tuple[str, str, str], clear: tuple[str, str]) -> None:
        out = await snmp_alerts.step(
            session, key, breached=breached, cleared=not breached, value=None, now=now,
            hold=timedelta(0), min_polls=1, interval=INTERVAL_SECONDS,
            record=snmp_alerts.Record(row.device_id, fire[0], fire[1]) if on and fire[0] else None)
        if out.fired and on and sending:
            await alerts.enqueue(session, kind=f"{kind}_bad", tone=fire[2], subject=fire[0],
                                 body=fire[1],
                                 dedupe_key=f"rule:{key}:{out.since.isoformat()}:fire")
            await snmp_alerts._mark_notified(session, key)
        elif out.cleared and out.notified and recoveries and sending:
            took = human_duration((now - out.since).total_seconds())
            await alerts.enqueue(session, kind=f"{kind}_ok", tone="ok", subject=clear[0],
                                 body=clear[1].replace("{took}", took),
                                 dedupe_key=f"rule:{key}:{out.since.isoformat()}:clear")

    drives = readings.get("drives")
    if drives is not None:
        prefix = _drive_prefix(row.id)
        seen = set()
        for d in drives:
            if not d.get("pool"):
                continue
            key = f"{prefix}{d['name']}"[:64]
            seen.add(key)
            status = d.get("status") or "unknown"
            where = f"pool {d['pool']}" + (f", {d['vdev']}" if d.get("vdev") else "")
            counts = (f"{d.get('read_errors', 0)} read, {d.get('write_errors', 0)} write, "
                      f"{d.get('checksum_errors', 0)} checksum errors")
            subject = (f"{name}: drive {d['name']} is {status}" if status != ONLINE else
                       f"{name}: drive {d['name']} has {errors(d)} error"
                       f"{'s' if errors(d) != 1 else ''}")
            await run(key, breached=not healthy(d),
                      on=bool(rules.get("drive_errors", True)) and not supp.off("drive_errors"),
                      kind="drive_health",
                      fire=(subject, f"{address} — {where}: {status}; {counts}."
                            + (f" {d['model']}." if d.get("model") else ""), "bad"),
                      clear=(f"{name}: drive {d['name']} is ONLINE with no errors",
                             f"{address} — it was not for {{took}}."))
        # A drive no longer listed at all: its alert has nothing left to
        # watch. Dropped quietly; a pool missing a drive lists it UNAVAIL.
        for key in await _keys(session, prefix) - seen:
            await session.delete(await session.get(AlertState, key))
            await snmp_alerts.close_incidents(session, now, key=key, resolution=snmp_alerts.GONE)

    listed = readings.get("alerts")
    if listed is not None:
        prefix = _alert_prefix(row.id)
        was = {a.get("uuid"): a for a in (before or {}).get("alerts") or []}
        current = {f"{prefix}{a['uuid']}"[:64]: a for a in listed if forwarded(a.get("level"))}
        on = bool(rules.get("truenas_alerts", True))
        for key, a in current.items():
            level = a["level"].title()
            await run(key, breached=True, on=on and not supp.off("truenas_alerts", a.get("klass")),
                      kind="truenas_alert",
                      fire=(f"{name}: {a['text']}"[:200],
                            f"{address} — TrueNAS {level}" + (f" ({a['klass']})" if a.get("klass") else "")
                            + ".", "bad" if _rank(a["level"]) >= _rank(BAD_FROM) else "info"),
                      clear=("", ""))
        for key in await _keys(session, prefix) - set(current):
            old = was.get(key[len(prefix):]) or {}
            text = old.get("text") or "a TrueNAS alert"
            await run(key, breached=False, on=on, kind="truenas_alert", fire=("", "", "info"),
                      clear=(f"{name}: cleared in TrueNAS: {text}"[:200],
                             f"{address} — TrueNAS listed it for {{took}}."))


# --------------------------------------------------------------------------
# The device page
# --------------------------------------------------------------------------


def _date(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def view(row: ApiCredential, *, hot: float) -> dict | None:
    """What the Storage card shows from this credential, or None."""
    readings = row.readings or {}
    if not any(readings.get(part) for part in ("pools", "drives", "alerts")):
        return None
    drives = []
    for d in readings.get("drives") or []:
        celsius = d.get("celsius")
        drives.append({**d, "errors": errors(d), "ok": healthy(d) if d.get("pool") else None,
                       "hot": celsius is not None and celsius >= hot,
                       "size_str": fmt_bytes(d.get("size")) if d.get("size") else "—"})
    shown = list(readings.get("alerts") or [])
    pools = {}
    for p in readings.get("pools") or []:
        scrub = p.get("scrub") or {}
        pools[p["name"]] = {**p, "scrub_end": _date(scrub.get("end")),
                            "scrub_state": scrub.get("state"), "scrub_errors": scrub.get("errors")}
    return {
        "read_at": _date(readings.get("read_at")),
        "pools": pools,
        "drives": drives,
        "alerts": [{**a, "pill": "bad" if _rank(a["level"]) >= _rank(BAD_FROM)
                    else "warn" if forwarded(a["level"]) else "neutral"} for a in shown],
        "problems": sum(1 for d in drives if d["ok"] is False),
    }
