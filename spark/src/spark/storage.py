"""Storage from SNMP: TrueNAS pools and drives, and filesystems on anything.

Read every 5 minutes, on a schedule of its own rather than with the
60-second poll. TrueNAS answers its own MIB through a helper that works each
value out when asked, which takes seconds, and while it works the whole SNMP
agent on the box waits -- so SPARK asks rarely, one device at a time, gives it
30 seconds, and only asks devices that are answering their polls.

What is read (collectors/snmp.py):
  * TrueNAS: each pool's health, and its space from the pool's root dataset;
    each drive's temperature.
  * Anything else running net-snmp: its real filesystems from hrStorageTable.
    Not on TrueNAS, where that table lists every dataset's mount and would
    repeat the pools many times over.

Alerts, decided in the same transaction as the read (the rules are under
Settings -> Alerts, and use the same state machine as the others,
snmp_alerts.step):
  * a pool that is not ONLINE -- DEGRADED, FAULTED, UNAVAIL -- at once, and
    again when it is ONLINE;
  * a pool at or over its space limit (85%) on two reads in a row;
  * a filesystem at or over its limit (90%), the same way;
  * a drive at or over its temperature (50 C) for its time (10 minutes).
A space or temperature alert ends once the value is 5 under the line.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import alerts, snmp_alerts
from .collectors import SnmpCollector
from .collectors.snmp import DriveReading, FilesystemReading, PoolReading
from .db import session_scope
from .engine.state import human_duration
from .models import Device, SnmpDevice, SnmpPoll, SnmpProfile, SnmpStorage, utcnow
from .snmp_config import credential_for
from .vault import SecretUnavailable, vault_for

log = logging.getLogger(__name__)

JOB_ID = "snmp-storage"
INTERVAL_MINUTES = 5
FIRST_RUN_SECONDS = 60
TIMEOUT = 30.0

POOL, DRIVE, FS = "pool", "drive", "fs"
HEALTHY = "ONLINE"

# Back under the line by this much before a space or heat alert ends.
PERCENT_MARGIN = 5.0
CELSIUS_MARGIN = 5.0
# Space moves slowly and one odd read is not news: two in a row.
SPACE_READS = 2


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


async def refresh(config) -> int:  # type: ignore[no-untyped-def]
    """The scheduler's job. Returns how many devices answered. Never raises."""
    try:
        async with session_scope() as session:
            rows = list((await session.execute(
                select(SnmpDevice.id)
                .join(SnmpPoll, SnmpPoll.snmp_device_id == SnmpDevice.id)
                .where(SnmpDevice.enabled.is_(True), SnmpPoll.last_ok_at.is_not(None),
                       SnmpPoll.last_error.is_(None))
                .order_by(SnmpDevice.id)
            )).scalars())
        answered = 0
        for row_id in rows:
            if await refresh_one(config, row_id):
                answered += 1
        return answered
    except Exception:  # noqa: BLE001 - the scheduler must keep running
        log.exception("Reading storage over SNMP failed")
        return 0


async def refresh_one(config, row_id: int) -> bool:  # type: ignore[no-untyped-def]
    """Read one device and decide its alerts. The poll's three phases."""
    async with session_scope() as session:
        row = await session.get(SnmpDevice, row_id)
        if row is None or not row.enabled:
            return False
        device = await session.get(Device, row.device_id)
        profile = await session.get(SnmpProfile, row.profile_id)
        if device is None or not device.primary_ip or profile is None:
            return False
        address = device.primary_ip
        try:
            credential = credential_for(profile, vault_for(config), timeout=TIMEOUT, retries=0)
        except SecretUnavailable:
            return False

    pools = drives = filesystems = None
    collector = SnmpCollector(address, credential)
    try:
        pools = await _ask(collector.truenas_pools, address, "pools")
        if pools:
            drives = await _ask(collector.truenas_drives, address, "drive temperatures")
            filesystems = []        # TrueNAS: its pools say it, once
        elif pools is not None:
            drives = []
            filesystems = await _ask(collector.filesystems, address, "filesystems")
    finally:
        await collector.close()

    async with session_scope() as session:
        if await session.get(SnmpDevice, row_id) is None:
            return False
        await store(session, row_id, pools=pools, drives=drives, filesystems=filesystems)
        await evaluate(session, row_id, utcnow())
    return pools is not None


async def _ask(method, address: str, what: str):  # type: ignore[no-untyped-def]
    try:
        return await method()
    except Exception as exc:  # noqa: BLE001
        log.info("%s: no %s over SNMP (%s)", address, what, exc)
        return None


async def store(session: AsyncSession, row_id: int, *,
                pools: list[PoolReading] | None, drives: list[DriveReading] | None,
                filesystems: list[FilesystemReading] | None) -> None:
    """Replace what this device said last time, kind by kind. None: not
    answered, so the last reading stands."""
    now = utcnow()
    batches = (
        (POOL, pools, lambda p: dict(name=p.name, health=p.health, size_bytes=p.size_bytes,
                                     used_bytes=p.used_bytes)),
        (DRIVE, drives, lambda d: dict(name=d.name, celsius=d.celsius)),
        (FS, filesystems, lambda f: dict(name=f.name, size_bytes=f.size_bytes,
                                         used_bytes=f.used_bytes)),
    )
    for kind, found, fields in batches:
        if found is None:
            continue
        await session.execute(delete(SnmpStorage).where(
            SnmpStorage.snmp_device_id == row_id, SnmpStorage.kind == kind))
        session.add_all(SnmpStorage(snmp_device_id=row_id, kind=kind, read_at=now,
                                    **fields(item)) for item in found)
    await session.flush()


async def rows_for(session: AsyncSession, row_id: int) -> dict[str, list[SnmpStorage]]:
    rows = (await session.execute(
        select(SnmpStorage).where(SnmpStorage.snmp_device_id == row_id)
        .order_by(SnmpStorage.name)
    )).scalars()
    out: dict[str, list[SnmpStorage]] = {POOL: [], DRIVE: [], FS: []}
    for row in rows:
        out.setdefault(row.kind, []).append(row)
    return out


# --------------------------------------------------------------------------
# Alerts
# --------------------------------------------------------------------------


def fmt_bytes(value: int | None) -> str:
    """1.2 TB, 512 GB: decimal units, as drives are sold and TrueNAS shows."""
    if value is None:
        return "—"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if size < 1000 or unit == "PB":
            return f"{size:.0f} {unit}" if unit == "B" or size >= 100 else f"{size:.1f} {unit}"
        size /= 1000
    return f"{size:.1f} PB"


async def evaluate(session: AsyncSession, row_id: int, now: datetime) -> None:
    """Every storage rule, for one read of one device."""
    row = await session.get(SnmpDevice, row_id)
    device = await session.get(Device, row.device_id) if row else None
    if device is None:
        return
    rules = await snmp_alerts.load(session)
    settings = await alerts.load(session)
    sending = settings.get("enabled", True) and not await alerts.is_muted(
        session, device_id=device.id)
    recoveries = settings.get("notify_on_recovery", True)
    address = f"`{device.primary_ip or '—'}`"
    interval = INTERVAL_MINUTES * 60
    found = await rows_for(session, row_id)

    async def run(key: str, *, breached: bool, cleared: bool, value: float | None,
                  hold: timedelta, reads: int, on: bool, fire: tuple[str, str],
                  clear: tuple[str, str], kind: str) -> None:
        out = await snmp_alerts.step(session, key, breached=breached, cleared=cleared,
                                     value=value, now=now, hold=hold, min_polls=reads,
                                     interval=interval)
        if out.fired and on and sending:
            await alerts.enqueue(session, kind=f"{kind}_bad", tone="bad", subject=fire[0],
                                 body=fire[1],
                                 dedupe_key=f"rule:{key}:{out.since.isoformat()}:fire")
            await snmp_alerts._mark_notified(session, key)
        elif out.cleared and out.notified and recoveries and sending:
            took = human_duration((now - out.since).total_seconds())
            await alerts.enqueue(session, kind=f"{kind}_ok", tone="ok", subject=clear[0],
                                 body=clear[1].replace("{took}", took),
                                 dedupe_key=f"rule:{key}:{out.since.isoformat()}:clear")

    name = device.display_name
    for pool in found[POOL]:
        health = (pool.health or "").upper()
        if health:
            await run(
                f"pool:{row_id}:{pool.name}", breached=health != HEALTHY,
                cleared=health == HEALTHY, value=None, hold=timedelta(0), reads=1,
                on=bool(rules.get("pool_health", True)), kind="pool_health",
                fire=(f"{name}: pool {pool.name} is {health}",
                      f"{address} — ZFS reports the pool {health}. Check its disks in TrueNAS."),
                clear=(f"{name}: pool {pool.name} is ONLINE again",
                       f"{address} — it was not ONLINE for {{took}}."),
            )
        await _space(run, row_id, name, address, pool, POOL, rules)
    for fs in found[FS]:
        await _space(run, row_id, name, address, fs, FS, rules)
    limit = float(rules.get("drive_celsius") or 50)
    minutes = int(rules.get("drive_minutes") or 10)
    for drive in found[DRIVE]:
        if drive.celsius is None:
            continue
        await run(
            f"drive:{row_id}:{drive.name}", breached=drive.celsius >= limit,
            cleared=drive.celsius <= limit - CELSIUS_MARGIN, value=drive.celsius,
            hold=timedelta(minutes=minutes), reads=1,
            on=bool(rules.get("drive_temperature", True)), kind="drive_temperature",
            fire=(f"{name}: drive {drive.name} is running hot ({drive.celsius:.0f}°C)",
                  f"{address} — at or over {limit:.0f}°C for {minutes} minute"
                  f"{'s' if minutes != 1 else ''}."),
            clear=(f"{name}: drive {drive.name} is back to {drive.celsius:.0f}°C",
                   f"{address} — was at or over {limit:.0f}°C for {{took}}."),
        )


async def _space(run, row_id: int, name: str, address: str, row: SnmpStorage,  # type: ignore[no-untyped-def]
                 kind: str, rules: dict) -> None:
    pct = row.percent
    if pct is None:
        return
    rule = "pool_space" if kind == POOL else "disk_space"
    limit = float(rules.get(f"{rule}_percent") or (85 if kind == POOL else 90))
    what = f"pool {row.name}" if kind == POOL else f"disk {row.name}"
    free = fmt_bytes((row.size_bytes or 0) - (row.used_bytes or 0))
    await run(
        f"space:{row_id}:{kind}:{row.name}", breached=pct >= limit,
        cleared=pct <= limit - PERCENT_MARGIN, value=pct, hold=timedelta(0),
        reads=SPACE_READS, on=bool(rules.get(rule, True)), kind=rule,
        fire=(f"{name}: {what} is {pct:.0f}% full",
              f"{address} — {free} free of {fmt_bytes(row.size_bytes)}; the line is {limit:.0f}%."),
        clear=(f"{name}: {what} is back to {pct:.0f}% full",
               f"{address} — was at or over {limit:.0f}% for {{took}}."),
    )
