"""A Proxmox host's guests, storage and drives, from its API.

Read by the 5-minute credential check (proxmox.test), stored on the
credential, and decided here in the same transaction. Alerts (Settings ->
Alerts; the mute list and suppressions apply):

  * a watched VM or container that is not running, on two checks in a row
    (so a restart is not news), or that Proxmox no longer lists; again once
    it runs. Guests are watched one by one on the device page, and only
    those alert: a stopped test VM is not a problem;
  * a ZFS pool that is not ONLINE, or an enabled storage that is not
    active (an NFS share gone, a disk missing): at once, and again when it
    is back. Rule: "A pool is not ONLINE";
  * a storage at or over the pool space line (85%), on two reads, until it
    is 5 under. Rule: "A pool is ... full". A shared storage counts once;
  * a drive whose SMART health is anything but PASSED (OK for SAS): at
    once, and again when it passes. UNKNOWN (a USB stick, a controller that
    hides SMART) is not decided on. Rule: "A drive is failing".

A part Proxmox did not answer is not decided on: a failed read is not the
all-clear. Nor is a node that did not answer taken to have lost its drives.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import alerts, snmp_alerts, suppressions
from .engine.state import human_duration
from .models import AlertState, ApiCredential, Device, utcnow
from .storage import PERCENT_MARGIN, fmt_bytes

INTERVAL_SECONDS = 5 * 60
GUEST_READS = 2
SPACE_READS = 2
RUNNING = "running"
ONLINE = "ONLINE"
SMART_OK = ("PASSED", "OK")
SMART_UNKNOWN = "UNKNOWN"

PREFIXES = ("pveguest", "pvezfs", "pvestore", "pvespace", "pvedisk")


def prefix(head: str, cred_id: int) -> str:
    return f"{head}:{cred_id}:"


def watched(row: ApiCredential) -> list[int]:
    """The guests that alert when they stop. `options` also keeps "labels":
    {vmid: [kind, name]} as last listed, to name one Proxmox has dropped."""
    raw = (row.options or {}).get("watched") or []
    return sorted({int(v) for v in raw if isinstance(v, int) and not isinstance(v, bool)})


def smart_ok(disk: dict) -> bool | None:
    """True, False, or None for a drive that does not say."""
    health = (disk.get("health") or SMART_UNKNOWN).upper()
    if health == SMART_UNKNOWN:
        return None
    return health in SMART_OK


async def forget(session: AsyncSession, cred_id: int) -> None:
    for head in PREFIXES:
        await drop(session, prefix(head, cred_id))


async def drop(session: AsyncSession, start: str, *, key: str | None = None) -> None:
    """Drop alert states quietly: the thing is no longer watched or listed."""
    now = utcnow()
    if key is not None:
        await session.execute(delete(AlertState).where(AlertState.key == key))
        await snmp_alerts.close_incidents(session, now, key=key, resolution=snmp_alerts.GONE)
        return
    await session.execute(delete(AlertState).where(AlertState.key.startswith(start)))
    await snmp_alerts.close_incidents(session, now, prefix=start, resolution=snmp_alerts.GONE)


async def _keys(session: AsyncSession, start: str) -> set[str]:
    return set((await session.execute(
        select(AlertState.key).where(AlertState.key.startswith(start)))).scalars())


def _node_of(key: str, start: str) -> str:
    return key[len(start):].split(":", 1)[0]


async def evaluate(session: AsyncSession, row: ApiCredential, before: dict,
                   now: datetime) -> None:
    """Every rule, for the reading just stored. `before` is the reading it
    replaced, for the name of a guest Proxmox no longer lists."""
    readings = row.readings or {}
    settings = await alerts.load(session)
    device = await session.get(Device, row.device_id) if row.device_id else None
    sending = settings.get("enabled", True) and not (
        device is not None and await alerts.is_muted(session, device_id=device.id))
    recoveries = settings.get("notify_on_recovery", True)
    rules = await snmp_alerts.load(session)
    supp = await suppressions.for_device(session, row.device_id)
    name = device.display_name if device else row.name
    address = f"`{row.host}`"
    answered = readings.get("answered") or {}

    def on(rule: str) -> bool:
        return bool(rules.get(rule, True)) and not supp.off(rule)

    async def run(key: str, *, breached: bool, cleared: bool, value: float | None = None,
                  reads: int = 1, rule: str, fire: tuple[str, str],
                  clear: tuple[str, str]) -> None:
        active = on(rule)
        out = await snmp_alerts.step(
            session, key[:64], breached=breached, cleared=cleared, value=value, now=now,
            hold=timedelta(0), min_polls=reads, interval=INTERVAL_SECONDS,
            record=snmp_alerts.Record(row.device_id, fire[0][:200], fire[1]) if active else None)
        if out.fired and active and sending:
            await alerts.enqueue(session, kind=f"{rule}_bad", tone="bad", subject=fire[0][:200],
                                 body=fire[1],
                                 dedupe_key=f"rule:{key[:64]}:{out.since.isoformat()}:fire")
            await snmp_alerts._mark_notified(session, key[:64])
        elif out.cleared and out.notified and recoveries and sending:
            took = human_duration((now - out.since).total_seconds())
            await alerts.enqueue(session, kind=f"{rule}_ok", tone="ok", subject=clear[0][:200],
                                 body=clear[1].replace("{took}", took),
                                 dedupe_key=f"rule:{key[:64]}:{out.since.isoformat()}:clear")

    # --- watched guests ---------------------------------------------------
    guests = readings.get("guests")
    start = prefix("pveguest", row.id)
    wanted = watched(row)
    if guests is not None:
        listed = {g["vmid"]: g for g in guests}
        was = {g.get("vmid"): g for g in (before or {}).get("guests") or []}
        # What each watched guest was last called, for when it is gone.
        labels = dict((row.options or {}).get("labels") or {})
        for vmid in wanted:
            g = listed.get(vmid) or was.get(vmid)
            if g:
                labels[str(vmid)] = [g.get("kind") or "guest", g.get("name") or str(vmid)]
        row.options = {**(row.options or {}), "labels": {k: v for k, v in labels.items()
                                                         if int(k) in wanted}}
        for vmid in wanted:
            g = listed.get(vmid)
            kind, label = labels.get(str(vmid)) or ["guest", str(vmid)]
            what = f"{kind} {vmid} ({label})"
            status = g["status"] if g else None
            await run(
                f"{start}{vmid}", breached=status != RUNNING, cleared=status == RUNNING,
                reads=GUEST_READS, rule="guest_down",
                fire=(f"{name}: {what} is {status}" if g else
                      f"{name}: {what} is not listed any more",
                      f"{address} — Proxmox shows it {status}"
                      + (f" on {g['node']}" if g and g.get("node") else "") + "."
                      if g else f"{address} — deleted, or moved where this token cannot see it."),
                clear=(f"{name}: {what} is running again",
                       f"{address} — it was not running for {{took}}."))
    for key in await _keys(session, start):
        if not key[len(start):].isdigit() or int(key[len(start):]) not in wanted:
            await drop(session, start, key=key)

    # --- ZFS pools --------------------------------------------------------
    pools = readings.get("zfs")
    if pools is not None:
        start = prefix("pvezfs", row.id)
        seen = set()
        for p in pools:
            key = f"{start}{p['node']}:{p['name']}"[:64]
            seen.add(key)
            health = p.get("health") or "UNKNOWN"
            await run(key, breached=health != ONLINE, cleared=health == ONLINE, rule="pool_health",
                      fire=(f"{name}: pool {p['name']} is {health}",
                            f"{address} — ZFS on {p['node']} reports the pool {health}. "
                            "Check its disks in Proxmox (the node → Disks → ZFS)."),
                      clear=(f"{name}: pool {p['name']} is ONLINE again",
                             f"{address} — it was not ONLINE for {{took}}."))
        await _gone(session, start, seen, answered.get("zfs") or [])

    # --- storages: active, and space -------------------------------------
    stores = readings.get("storages")
    if stores is not None:
        start, space = prefix("pvestore", row.id), prefix("pvespace", row.id)
        seen = set()
        counted: set[str] = set()
        line = supp.line("pool_space", float(rules.get("pool_space_percent") or 85))
        for s in stores:
            if s.get("shared"):
                if s["name"] in counted:
                    continue
                counted.add(s["name"])
            key = f"{start}{s['node']}:{s['name']}"[:64]
            seen.add(key)
            await run(key, breached=not s["active"], cleared=bool(s["active"]), rule="pool_health",
                      fire=(f"{name}: storage {s['name']} is not available",
                            f"{address} — Proxmox on {s['node']} cannot reach the "
                            f"{s.get('type') or ''} storage {s['name']}.".replace("  ", " ")),
                      clear=(f"{name}: storage {s['name']} is available again",
                             f"{address} — it was not for {{took}}."))
            pct = s.get("pct")
            if pct is None or not s["active"]:
                continue
            key = f"{space}{s['node']}:{s['name']}"[:64]
            seen.add(key)
            free = fmt_bytes((s.get("total") or 0) - (s.get("used") or 0))
            await run(key, breached=pct >= line, cleared=pct <= line - PERCENT_MARGIN, value=pct,
                      reads=SPACE_READS, rule="pool_space",
                      fire=(f"{name}: storage {s['name']} is {pct:.0f}% full",
                            f"{address} — {free} free of {fmt_bytes(s.get('total'))}; "
                            f"the line is {line:.0f}%."),
                      clear=(f"{name}: storage {s['name']} is back to {pct:.0f}% full",
                             f"{address} — was at or over {line:.0f}% for {{took}}."))
        nodes = answered.get("storages") or []
        await _gone(session, start, seen, nodes)
        await _gone(session, space, seen, nodes)

    # --- drives -----------------------------------------------------------
    disks = readings.get("disks")
    if disks is not None:
        start = prefix("pvedisk", row.id)
        seen = set()
        for d in disks:
            ok = smart_ok(d)
            key = f"{start}{d['node']}:{d['devpath']}"[:64]
            seen.add(key)
            if ok is None:
                continue                # not decided on: held as it was
            model = f" ({d['model']})" if d.get("model") else ""
            await run(key, breached=not ok, cleared=ok, rule="drive_errors",
                      fire=(f"{name}: drive {d['devpath']} SMART says {d['health']}",
                            f"{address} — {d['node']}: {d['devpath']}{model}. "
                            "Check it in Proxmox (the node → Disks → Show S.M.A.R.T. values)."),
                      clear=(f"{name}: drive {d['devpath']} passes SMART again",
                             f"{address} — it did not for {{took}}."))
        await _gone(session, start, seen, answered.get("disks") or [])


async def _gone(session: AsyncSession, start: str, seen: set[str], nodes: list[str]) -> None:
    """Drop the alerts of things no longer listed -- only on a node that
    answered; one that did not has not been looked at."""
    for key in await _keys(session, start) - seen:
        if _node_of(key, start) in nodes:
            await drop(session, start, key=key)


# --------------------------------------------------------------------------
# The Targets page: every watched guest, from every Proxmox credential
# --------------------------------------------------------------------------


async def watched_guests(session: AsyncSession) -> list[dict]:
    """Each watched VM or container, as last read, for the Targets page.

    Watching is done on the host's Proxmox card; this only lists them, so
    the Targets page shows everything SPARK alerts on in one place. `stale`
    is set when the host's last check failed: the state shown is then the
    last one read, not the current one."""
    rows = (await session.execute(
        select(ApiCredential).where(ApiCredential.kind == "proxmox").order_by(ApiCredential.name)
    )).scalars()
    out = []
    for row in rows:
        wanted = watched(row)
        if not wanted:
            continue
        readings = row.readings or {}
        guests = readings.get("guests")
        listed = {g["vmid"]: g for g in guests or []}
        labels = (row.options or {}).get("labels") or {}
        device = await session.get(Device, row.device_id) if row.device_id else None
        read_at = _date(readings.get("read_at"))
        for vmid in wanted:
            g = listed.get(vmid)
            kind, name = (labels.get(str(vmid)) or [None, None])[:2]
            if g:
                kind, name = g.get("kind") or kind, g.get("name") or name
            running = bool(g) and g.get("status") == RUNNING
            out.append({
                "cred_id": row.id, "vmid": vmid, "name": name or str(vmid),
                "kind": kind or "guest", "node": g.get("node") if g else None,
                "host": device.display_name if device else row.name,
                "device_id": device.id if device else None,
                "read": guests is not None,
                "listed": g is not None, "running": running,
                "status": (g.get("status") or "unknown") if g else "not listed",
                "up": uptime(g.get("uptime")) if running else "—",
                "stale": bool(row.last_error), "read_at": read_at,
            })
    return out


# --------------------------------------------------------------------------
# The device page
# --------------------------------------------------------------------------


def _date(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def _pct(used: int | None, total: int | None) -> float | None:
    return round(100.0 * used / total, 1) if used is not None and total else None


def uptime(seconds: int | None) -> str:
    if not seconds:
        return "—"
    return human_duration(seconds)


def view(row: ApiCredential, *, line: float) -> dict | None:
    """What the Proxmox card shows from this credential, or None."""
    if row.kind != "proxmox":
        return None
    readings = row.readings or {}
    if not any(readings.get(part) is not None for part in
               ("nodes", "guests", "storages", "zfs", "disks")):
        return None
    wanted = set(watched(row))
    nodes = [{**n, "cpu_pct": round(100 * n["cpu"]) if n.get("cpu") is not None else None,
              "mem_pct": _pct(n.get("mem"), n.get("maxmem")),
              "mem_str": f"{fmt_bytes(n.get('mem'))} of {fmt_bytes(n.get('maxmem'))}"
                         if n.get("maxmem") else "—",
              "up": uptime(n.get("uptime"))} for n in readings.get("nodes") or []]
    guests = []
    for g in readings.get("guests") or []:
        running = g.get("status") == RUNNING
        guests.append({**g, "running": running, "watched": g["vmid"] in wanted,
                       "cpu_pct": round(100 * g["cpu"]) if running and g.get("cpu") is not None else None,
                       "mem_str": f"{fmt_bytes(g.get('mem'))} of {fmt_bytes(g.get('maxmem'))}"
                                  if running and g.get("maxmem") else "—",
                       "up": uptime(g.get("uptime")) if running else "—"})
    listed = {g["vmid"] for g in guests}
    stores = [{**s, "over": s.get("pct") is not None and s["pct"] >= line,
               "bar": min(100.0, max(0.0, s.get("pct") or 0.0)),
               "used_str": fmt_bytes(s.get("used")), "total_str": fmt_bytes(s.get("total")),
               "free_str": fmt_bytes((s.get("total") or 0) - (s.get("used") or 0))
                           if s.get("total") else "—"}
              for s in readings.get("storages") or []]
    zfs = [{**p, "ok": p.get("health") == ONLINE, "size_str": fmt_bytes(p.get("size")),
            "free_str": fmt_bytes(p.get("free"))} for p in readings.get("zfs") or []]
    disks = [{**d, "ok": smart_ok(d), "size_str": fmt_bytes(d.get("size"))}
             for d in readings.get("disks") or []]
    many = len(nodes) > 1
    return {
        "read_at": _date(readings.get("read_at")),
        "nodes": nodes, "many_nodes": many,
        "guests": guests,
        "running": sum(1 for g in guests if g["running"]),
        "watched_missing": [
            {"vmid": vmid, "name": (((row.options or {}).get("labels") or {}).get(str(vmid))
                                    or [None, None])[1]}
            for vmid in sorted(wanted - listed)] if readings.get("guests") is not None else [],
        "storages": stores, "zfs": zfs, "disks": disks,
        "problems": (sum(1 for g in guests if g["watched"] and not g["running"])
                     + sum(1 for s in stores if not s["active"])
                     + sum(1 for p in zfs if not p["ok"])
                     + sum(1 for d in disks if d["ok"] is False)),
    }
