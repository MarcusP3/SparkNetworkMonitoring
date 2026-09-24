"""Scheduled SNMP polling: health and interface traffic, every interval.

Test (in `snmp_config`) asks a device what it supports, once, when you press
it. This is the other half: ask the devices on the SNMP list how they are, on a
schedule, and keep the answers.

Three phases per poll, and the middle one holds no database connection:

  1. Read the device, its profile and the interval; unseal the credential.
  2. Ask the device. A dead device costs the full timeout twice over, and
     nothing about the database should be held open while that happens.
  3. Write the whole result in one short transaction.

The arithmetic that turns counters into rates is in plain functions at the top,
with no database and no network, because it is where the subtle failures live
and it deserves tests that can pin each one down:

  * **Wrap.** A 32-bit octet counter wraps after 4 GiB -- about 34 seconds at
    a full gigabit. A reading lower than the last is a wrap if the counter is
    32-bit, and is added back once; it is a reset if the counter is 64-bit,
    which will not wrap this century.
  * **Ambiguous wraps.** One wrap is all a 32-bit counter can show. If the link
    could have carried more than 4 GiB in the interval, a small result may be
    a large one that wrapped twice. So a 32-bit rate above the link's speed is
    discarded rather than stored.
  * **Resets.** A reboot sets every counter back to zero. `sysUpTime` going
    backwards is how it is recognised; the poll after one records a new
    baseline and no rate.
  * **Gaps.** SPARK down for an hour, then a poll: the difference is a
    correct average over the hour, stored as though it were one interval.
    Past three intervals the reading becomes a baseline instead.
  * **A change of width.** A device that answered the 64-bit table last time
    and only the 32-bit one now has not had a 4-billion-octet minute.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import alerts
from .collectors import SnmpCollector
from .collectors.base import DeviceHealth, InterfaceStat
from .db import get_setting, session_scope
from .models import (
    Device,
    SnmpDevice,
    SnmpHealthSample,
    SnmpInterface,
    SnmpInterfaceSample,
    SnmpPoll,
    SnmpProfile,
    utcnow,
)
from .snmp_config import credential_for
from .vault import SecretUnavailable, vault_for

log = logging.getLogger(__name__)

DEFAULT_INTERVAL = 60
MIN_INTERVAL = 30
MAX_INTERVAL = 3600

# Per request, and one retry: an unanswered poll costs about six seconds, well
# inside the shortest interval.
POLL_TIMEOUT = 3.0

WRAP_32 = 1 << 32

# Headroom over the nominal link speed before a 32-bit rate is thrown out.
# Counters are read a moment apart from the timestamps they are paired with,
# so a saturated link can read a few percent over its speed honestly.
SPEED_TOLERANCE = 1.1


def clamp_interval(value: object) -> int:
    try:
        seconds = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL
    return max(MIN_INTERVAL, min(MAX_INTERVAL, seconds))


def max_gap(interval: int) -> float:
    """The longest gap between readings that still yields a rate."""
    return max(3 * interval, 180)


# --------------------------------------------------------------------------
# Counter arithmetic -- pure, and tested as such
# --------------------------------------------------------------------------


def counter_delta(previous: int | None, current: int | None, bits: int) -> int | None:
    """How far a counter moved, allowing for a single 32-bit wrap.

    None when either reading is missing, or when a 64-bit counter went
    backwards -- that is a reset, not a wrap, and the true movement is unknown.
    """
    if previous is None or current is None:
        return None
    delta = current - previous
    if delta >= 0:
        return delta
    if bits == 32 and previous < WRAP_32:
        return delta + WRAP_32
    return None


@dataclass(frozen=True)
class Reading:
    """The previous poll's counters for one interface."""

    bits: int
    in_octets: int | None
    out_octets: int | None
    in_errors: int | None
    out_errors: int | None
    at: datetime


@dataclass(frozen=True)
class Rates:
    in_bps: float | None
    out_bps: float | None
    in_errors: int | None
    out_errors: int | None


def interface_rates(
    previous: Reading | None,
    current: InterfaceStat,
    now: datetime,
    *,
    interval: int,
    rebooted: bool = False,
) -> Rates | None:
    """Rates between two readings, or None if they cannot be trusted.

    None means "record this reading as the new baseline and store no sample",
    which is always the safe answer: a missing minute on a chart is honest, a
    spike to 40 Gbps on a gigabit port is not.
    """
    if previous is None or rebooted:
        return None
    bits = 64 if current.counters_are_64bit else 32
    if bits != previous.bits:
        return None
    seconds = (now - previous.at).total_seconds()
    if seconds <= 0 or seconds > max_gap(interval):
        return None

    d_in = counter_delta(previous.in_octets, current.in_octets, bits)
    d_out = counter_delta(previous.out_octets, current.out_octets, bits)
    if d_in is None and d_out is None:
        return None
    in_bps = d_in * 8 / seconds if d_in is not None else None
    out_bps = d_out * 8 / seconds if d_out is not None else None

    if bits == 32 and current.speed_mbps:
        # Only for 32-bit counters, where one visible wrap may hide another.
        # 64-bit rates are not capped: virtual interfaces (loopback, bridges,
        # some VLAN interfaces) report a nominal speed they routinely exceed.
        ceiling = current.speed_mbps * 1_000_000 * SPEED_TOLERANCE
        if (in_bps or 0) > ceiling or (out_bps or 0) > ceiling:
            return None

    # Error counters are 32-bit in IF-MIB regardless of the octet counters.
    return Rates(
        in_bps=in_bps,
        out_bps=out_bps,
        in_errors=counter_delta(previous.in_errors, current.in_errors, 32),
        out_errors=counter_delta(previous.out_errors, current.out_errors, 32),
    )


def detect_reboot(
    previous_uptime: float | None,
    current_uptime: float | None,
    seconds_between: float | None,
) -> bool:
    """Whether the device restarted between two polls.

    Uptime should have grown by about the time between the polls. Less than
    that, by more than slack for clock differences, means it started again
    from zero somewhere in between -- even if the new uptime is still larger
    than the old, which a long gap allows.

    It is the agent's uptime, strictly. A restart of snmpd alone looks the
    same, and costs one sample: the interface counters (the kernel's) did not
    reset, but a baseline is the safe reading of an ambiguous case.
    """
    if previous_uptime is None or current_uptime is None:
        return False
    expected = previous_uptime + (seconds_between or 0)
    slack = max(30.0, 0.1 * (seconds_between or 0))
    return current_uptime < expected - slack


# --------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------


def _reading(iface: SnmpInterface) -> Reading | None:
    if iface.counters_at is None or iface.counter_bits is None:
        return None
    return Reading(
        bits=iface.counter_bits,
        in_octets=iface.in_octets,
        out_octets=iface.out_octets,
        in_errors=iface.in_errors,
        out_errors=iface.out_errors,
        at=iface.counters_at,
    )


async def record_poll(
    session: AsyncSession,
    row_id: int,
    health: DeviceHealth,
    interfaces: list[InterfaceStat],
    now: datetime,
    *,
    interval: int,
    duration_ms: int | None = None,
) -> SnmpPoll:
    """Write one poll's result. Separate from the network so tests can feed it.

    A poll that was not answered changes only the poll row and adds a health
    sample marked unreachable. Interface rows keep their last reading, and the
    gap rule decides whether the next answer can be compared with it.
    """
    poll = await session.get(SnmpPoll, row_id)
    if poll is None:
        poll = SnmpPoll(snmp_device_id=row_id)
        session.add(poll)

    previous_ok = poll.last_ok_at
    was_failing = poll.last_error is not None

    # Decided before the row changes, from what it said last time, and in this
    # transaction so the alert commits with the poll that caused it.
    row = await session.get(SnmpDevice, row_id)
    device = await session.get(Device, row.device_id) if row else None
    if device is not None:
        await alerts.on_snmp_poll(
            session, row_id=row_id, device_id=device.id, device_name=device.display_name,
            address=device.primary_ip, answered=health.reachable,
            previous_ok=previous_ok, was_failing=was_failing, now=now,
            interval=interval, error=health.error,
        )

    poll.last_polled_at = now
    poll.duration_ms = duration_ms

    session.add(SnmpHealthSample(
        snmp_device_id=row_id,
        ts=now,
        reachable=health.reachable,
        cpu_percent=health.cpu_percent if health.reachable else None,
        load_1min=health.load_1min if health.reachable else None,
        memory_percent=health.memory_percent if health.reachable else None,
        temperature_max=health.max_temperature if health.reachable else None,
    ))

    if not health.reachable:
        poll.last_error = health.error or "No response."
        return poll

    since_last = (now - previous_ok).total_seconds() if previous_ok else None
    rebooted = detect_reboot(poll.uptime_seconds, health.uptime_seconds, since_last)
    if rebooted:
        log.info("SNMP device %s restarted; taking new counter baselines", row_id)

    poll.last_ok_at = now
    poll.last_error = None
    poll.uptime_seconds = health.uptime_seconds
    poll.cpu_percent = health.cpu_percent
    poll.load_1min = health.load_1min
    poll.memory_percent = health.memory_percent
    poll.temperature_max = health.max_temperature
    poll.sources = dict(health.sources) or None
    poll.interfaces_total = len(interfaces)
    poll.interfaces_up = sum(1 for i in interfaces if i.is_up)

    existing = {
        iface.if_index: iface
        for iface in (
            await session.execute(
                select(SnmpInterface).where(SnmpInterface.snmp_device_id == row_id)
            )
        ).scalars()
    }

    pending: list[tuple[SnmpInterface, Rates]] = []
    for stat in interfaces:
        iface = existing.get(stat.index)
        if iface is None:
            iface = SnmpInterface(snmp_device_id=row_id, if_index=stat.index, first_seen=now)
            session.add(iface)
        elif iface.oper_status != stat.oper_status:
            iface.status_changed_at = now

        rates = interface_rates(
            _reading(iface), stat, now, interval=interval, rebooted=rebooted
        )

        iface.name = stat.name
        iface.descr = stat.descr
        iface.alias = stat.alias
        iface.type_name = stat.type_name
        iface.mac = stat.mac
        iface.admin_status = stat.admin_status
        iface.oper_status = stat.oper_status
        iface.speed_mbps = stat.speed_mbps
        iface.counter_bits = 64 if stat.counters_are_64bit else 32
        iface.in_octets = stat.in_octets
        iface.out_octets = stat.out_octets
        iface.in_errors = stat.in_errors
        iface.out_errors = stat.out_errors
        iface.counters_at = now
        iface.in_bps = rates.in_bps if rates else None
        iface.out_bps = rates.out_bps if rates else None
        iface.last_seen = now

        if rates is not None and stat.is_up:
            pending.append((iface, rates))

    if pending:
        await session.flush()  # new interfaces need their ids for the samples
        session.add_all(
            SnmpInterfaceSample(
                interface_id=iface.id,
                ts=now,
                in_bps=rates.in_bps,
                out_bps=rates.out_bps,
                in_errors=rates.in_errors,
                out_errors=rates.out_errors,
            )
            for iface, rates in pending
        )
    return poll


# --------------------------------------------------------------------------
# One poll, end to end
# --------------------------------------------------------------------------


async def poll_device(config, row_id: int) -> bool | None:  # type: ignore[no-untyped-def]
    """Poll one device and store the result. The scheduler's job function.

    Returns whether the device answered, or None if there was nothing to poll
    (removed, paused, or no address). Never raises: a poller that throws when
    the thing it polls is broken has failed at its job.
    """
    try:
        return await _poll(config, row_id)
    except Exception:  # noqa: BLE001 - the scheduler must keep running
        log.exception("SNMP poll of device %s failed", row_id)
        return None


async def _poll(config, row_id: int) -> bool | None:  # type: ignore[no-untyped-def]
    # ---- 1: read ----
    async with session_scope() as session:
        row = await session.get(SnmpDevice, row_id)
        if row is None or not row.enabled:
            return None
        device = await session.get(Device, row.device_id)
        profile = await session.get(SnmpProfile, row.profile_id)
        address = device.primary_ip if device else None
        interval = clamp_interval(
            (await get_setting(session, "snmp")).get("poll_interval_seconds")
        )
        try:
            credential = credential_for(
                profile, vault_for(config), timeout=POLL_TIMEOUT, retries=1
            ) if address else None
            unsealed_error = None
        except SecretUnavailable as exc:
            credential, unsealed_error = None, str(exc)

    # ---- 2: ask (no connection held) ----
    started = time.monotonic()
    interfaces: list[InterfaceStat] = []
    if credential is None:
        health = DeviceHealth(error=unsealed_error or "This device has no address to ask.")
    else:
        collector = SnmpCollector(address, credential)  # type: ignore[arg-type]
        try:
            health = await collector.collect_health(inventory=False)
            if health.reachable:
                interfaces = await collector.collect_interfaces()
        finally:
            await collector.close()
    now = utcnow()
    duration_ms = int((time.monotonic() - started) * 1000)

    # ---- 3: write ----
    async with session_scope() as session:
        if await session.get(SnmpDevice, row_id) is None:
            return None  # taken off the list while we were asking
        await record_poll(
            session, row_id, health, interfaces, now,
            interval=interval, duration_ms=duration_ms,
        )
    return health.reachable
