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
  * a pool or filesystem filling fast enough to be full within its days
    (14), from the trend below, on two reads in a row;
  * a drive at or over its temperature (50 C) for its time (10 minutes).
A space or temperature alert ends once the value is 5 under the line; a
trend alert once it would take half as long again (21 days) or more.

**Time until full.** Each pool's and filesystem's space is kept once an
hour (models.StorageSample), and a least-squares line through the last 14
days of it says how fast it is filling and when it would be full: 85% means
one thing on a pool growing a gigabyte a month and another on one taking
camera footage at 50 GB a day. Two days of history before it says anything;
a pool shrinking, or growing less than a megabyte a day, is not growing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import alerts, prefs, snmp_alerts, suppressions
from .collectors import SnmpCollector
from .collectors.snmp import DriveReading, FilesystemReading, PoolReading
from .db import session_scope
from .engine.state import human_duration
from .models import Device, SnmpDevice, SnmpPoll, SnmpProfile, SnmpStorage, StorageSample, utcnow
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

# The trend: a sample an hour, fitted over this long, once there is enough.
SAMPLE_EVERY = timedelta(hours=1)
TREND_WINDOW = timedelta(days=14)
TREND_NEEDS = timedelta(days=2)
NOT_GROWING = 1_000_000          # bytes a day: less is noise, not growth
# A trend alert ends once it would take this much longer than its line.
TREND_CLEAR = 1.5


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
                filesystems: list[FilesystemReading] | None, now: datetime | None = None) -> None:
    """Replace what this device said last time, kind by kind. None: not
    answered, so the last reading stands."""
    now = now or utcnow()
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
    await _sample(session, row_id, now, pools=pools, filesystems=filesystems)
    await session.flush()


async def _sample(session: AsyncSession, row_id: int, now: datetime, *,
                  pools: list[PoolReading] | None,
                  filesystems: list[FilesystemReading] | None) -> None:
    """A sample of each pool's and filesystem's space, if its last is an
    hour old: the read is every five minutes, the trend needs far fewer."""
    last = dict(((kind, name), ts) for kind, name, ts in (await session.execute(
        select(StorageSample.kind, StorageSample.name, func.max(StorageSample.ts))
        .where(StorageSample.snmp_device_id == row_id)
        .group_by(StorageSample.kind, StorageSample.name))).all())
    for kind, found in ((POOL, pools), (FS, filesystems)):
        for item in found or ():
            if item.size_bytes is None or item.used_bytes is None or not item.size_bytes:
                continue
            before = last.get((kind, item.name))
            if before is not None and now - before < SAMPLE_EVERY:
                continue
            session.add(StorageSample(snmp_device_id=row_id, kind=kind, name=item.name, ts=now,
                                      size_bytes=item.size_bytes, used_bytes=item.used_bytes))


@dataclass(frozen=True)
class Trend:
    per_day: float              # bytes a day, from the fitted line
    days_left: float | None     # None: not growing
    span: timedelta             # how much history the line is fitted to
    full_at: datetime | None


async def trend(session: AsyncSession, row_id: int, kind: str, name: str,
                now: datetime | None = None) -> Trend | None:
    """How fast it is filling and when it would be full, from a least-
    squares line through its samples over TREND_WINDOW. None until there are
    TREND_NEEDS of them."""
    now = now or utcnow()
    points = (await session.execute(
        select(StorageSample.ts, StorageSample.used_bytes, StorageSample.size_bytes)
        .where(StorageSample.snmp_device_id == row_id, StorageSample.kind == kind,
               StorageSample.name == name, StorageSample.ts >= now - TREND_WINDOW)
        .order_by(StorageSample.ts))).all()
    if len(points) < 3 or points[-1].ts - points[0].ts < TREND_NEEDS:
        return None
    start = points[0].ts
    xs = [(p.ts - start).total_seconds() / 86400 for p in points]
    ys = [float(p.used_bytes) for p in points]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    spread = sum((x - mx) ** 2 for x in xs)
    if spread <= 0:
        return None
    per_day = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / spread
    span = points[-1].ts - start
    if per_day < NOT_GROWING:
        return Trend(per_day, None, span, None)
    free = max(0.0, float(points[-1].size_bytes) - float(points[-1].used_bytes))
    days = free / per_day
    return Trend(per_day, days, span, points[-1].ts + timedelta(days=days))


def fmt_days(days: float | None) -> str:
    """'about 9 days', 'under a day', 'about 3 months': what the page and
    the alert say."""
    if days is None:
        return "not growing"
    if days < 1:
        return "under a day"
    if days < 60:
        whole = round(days)
        return f"about {whole} day{'s' if whole != 1 else ''}"
    if days < 730:
        return f"about {round(days / 30.4)} months"
    return "years"


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
    supp = await suppressions.for_device(session, device.id)
    settings = await alerts.load(session)
    sending = settings.get("enabled", True) and not await alerts.is_quiet(
        session, device_id=device.id, when=now)
    recoveries = settings.get("notify_on_recovery", True)
    address = f"`{device.primary_ip or '—'}`"
    interval = INTERVAL_MINUTES * 60
    found = await rows_for(session, row_id)

    async def run(key: str, *, breached: bool, cleared: bool, value: float | None,
                  hold: timedelta, reads: int, on: bool, fire: tuple[str, str],
                  clear: tuple[str, str], kind: str) -> None:
        out = await snmp_alerts.step(
            session, key, breached=breached, cleared=cleared, value=value, now=now, hold=hold,
            min_polls=reads, interval=interval,
            record=snmp_alerts.Record(device.id, fire[0], fire[1]) if on else None)
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
                on=bool(rules.get("pool_health", True)) and not supp.off("pool_health"),
                kind="pool_health",
                fire=(f"{name}: pool {pool.name} is {health}",
                      f"{address} — ZFS reports the pool {health}. Check its disks in TrueNAS."),
                clear=(f"{name}: pool {pool.name} is ONLINE again",
                       f"{address} — it was not ONLINE for {{took}}."),
            )
        await _space(run, row_id, name, address, pool, POOL, rules, supp)
    for fs in found[FS]:
        await _space(run, row_id, name, address, fs, FS, rules, supp)
    tz = prefs.zone_of(await prefs.get_timezone(session))
    for kind in (POOL, FS):
        for row in found[kind]:
            await _trend(session, run, row_id, name, address, row, kind, rules, supp, now, tz)
    limit = supp.line("drive_temperature", float(rules.get("drive_celsius") or 50))
    minutes = int(rules.get("drive_minutes") or 10)
    for drive in found[DRIVE]:
        if drive.celsius is None:
            continue
        await run(
            f"drive:{row_id}:{drive.name}", breached=drive.celsius >= limit,
            cleared=drive.celsius <= limit - CELSIUS_MARGIN, value=drive.celsius,
            hold=timedelta(minutes=minutes), reads=1,
            on=bool(rules.get("drive_temperature", True)) and not supp.off("drive_temperature"),
            kind="drive_temperature",
            fire=(f"{name}: drive {drive.name} is running hot ({drive.celsius:.0f}°C)",
                  f"{address} — at or over {limit:.0f}°C for {minutes} minute"
                  f"{'s' if minutes != 1 else ''}."),
            clear=(f"{name}: drive {drive.name} is back to {drive.celsius:.0f}°C",
                   f"{address} — was at or over {limit:.0f}°C for {{took}}."),
        )


async def _space(run, row_id: int, name: str, address: str, row: SnmpStorage,  # type: ignore[no-untyped-def]
                 kind: str, rules: dict, supp) -> None:
    pct = row.percent
    if pct is None:
        return
    rule = "pool_space" if kind == POOL else "disk_space"
    limit = supp.line(rule, float(rules.get(f"{rule}_percent") or (85 if kind == POOL else 90)))
    what = f"pool {row.name}" if kind == POOL else f"disk {row.name}"
    free = fmt_bytes((row.size_bytes or 0) - (row.used_bytes or 0))
    await run(
        f"space:{row_id}:{kind}:{row.name}", breached=pct >= limit,
        cleared=pct <= limit - PERCENT_MARGIN, value=pct, hold=timedelta(0),
        reads=SPACE_READS, on=bool(rules.get(rule, True)) and not supp.off(rule), kind=rule,
        fire=(f"{name}: {what} is {pct:.0f}% full",
              f"{address} — {free} free of {fmt_bytes(row.size_bytes)}; the line is {limit:.0f}%."),
        clear=(f"{name}: {what} is back to {pct:.0f}% full",
               f"{address} — was at or over {limit:.0f}% for {{took}}."),
    )


async def _trend(session: AsyncSession, run, row_id: int, name: str, address: str,  # type: ignore[no-untyped-def]
                 row: SnmpStorage, kind: str, rules: dict, supp, now: datetime, tz) -> None:  # type: ignore[no-untyped-def]
    """Filling fast enough to be full within its days. Nothing until the
    trend has its two days; then not growing counts as clear."""
    if row.percent is None:
        return
    found = await trend(session, row_id, kind, row.name, now)
    if found is None:
        return
    limit = supp.line("space_trend", float(rules.get("space_trend_days") or 14))
    what = f"pool {row.name}" if kind == POOL else f"disk {row.name}"
    days = found.days_left
    when = found.full_at.astimezone(tz).strftime("%a %b %-d") if found.full_at else ""
    over = round(found.span.total_seconds() / 86400)
    await run(
        f"trend:{row_id}:{kind}:{row.name}", breached=days is not None and days < limit,
        cleared=days is None or days >= limit * TREND_CLEAR, value=days, hold=timedelta(0),
        reads=SPACE_READS, on=bool(rules.get("space_trend", True)) and not supp.off("space_trend"),
        kind="space_trend",
        fire=(f"{name}: {what} will be full in {fmt_days(days)}",
              f"{address} — {fmt_bytes(row.used_bytes)} of {fmt_bytes(row.size_bytes)}, growing "
              f"{fmt_bytes(int(found.per_day))} a day over the last {over} days. "
              f"At this rate it is full around {when}."),
        clear=(f"{name}: {what} is no longer filling up fast",
               f"{address} — {'not growing' if days is None else 'full in ' + fmt_days(days)} "
               f"at the rate now; it was for {{took}}."),
    )
