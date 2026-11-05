"""Custom SNMP metrics: OIDs a person chose, read in the regular poll.

The built-in collection knows CPU, memory, temperature, interfaces and
storage. Everything else a device can say over SNMP -- a GPU's memory through
net-snmp's `extend`, a UPS's runtime, a sensor in a vendor MIB -- is a
number at an OID only the person who set it up knows. This is how they tell
SPARK: a name, the OID, a unit and a scale, per device.

Gauges only. A value is read as it is and multiplied by its scale; nothing
here turns a counter into a rate (snmp_poll.py does that for interfaces,
with all the wrap and reset handling it takes).

Values may arrive as numbers or as text: `extend` output is always a
string ("28416", "10.50"), and plenty of vendor MIBs put a reading in a
DisplayString. Text is read up to its first number, so "31 C" is 31; text
with no number is an error for that metric, not for the poll.

Reading happens in the poll's own request to the device (snmp_poll), with
the engine it already has; recording happens in the poll's transaction; and
the threshold rule is decided in that same transaction, like every other
alert (alerts.py, snmp_alerts.py).
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import alerts, snmp_alerts
from .engine.state import human_duration
from .models import CustomAlertOp, Device, SnmpCustomMetric, SnmpCustomSample, SnmpDevice, SnmpProfile
from .vault import SecretUnavailable, Vault

log = logging.getLogger(__name__)

# Per device. A poll asks for all of them, so this bounds what one poll adds.
MAX_PER_DEVICE = 20

# Asked for this many at a time: well under any agent's PDU size, and one
# OID an agent dislikes enough to fail a whole request fails only its group.
GET_GROUP = 10

# How far back across the line a value must come before a fired alert clears,
# as a share of the line: an alert at 45,000 MiB clears at 42,750.
CLEAR_MARGIN = 0.05

MINUTES_RANGE = (1, 1440)
SCALE_LIMIT = 1e12

# net-snmp's NET-SNMP-EXTEND-MIB::nsExtendOutLine, indexed by the extend's
# name (as a length-prefixed string) and then the output line number.
NS_EXTEND_OUT_LINE = "1.3.6.1.4.1.8072.1.3.2.4.1.2"

_OID = re.compile(r"^\d+(\.\d+)+$")
_EXTEND_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_NUMBER = re.compile(r"^[-+]?(\d+(\.\d*)?|\.\d+)([eE][-+]?\d+)?")
_UNIT = re.compile(r"^[^\x00-\x1f<>&\"']{0,16}$")


class MetricError(ValueError):
    """Something the form cannot save. The message is for a person."""


# --------------------------------------------------------------------------
# Pure functions
# --------------------------------------------------------------------------


def normalize_oid(raw: str) -> str:
    """A numeric OID with no leading dot, or MetricError."""
    oid = (raw or "").strip().lstrip(".")
    if not oid:
        raise MetricError("Enter the OID to read.")
    if len(oid) > 255 or not _OID.match(oid):
        raise MetricError("The OID must be numeric, like 1.3.6.1.4.1.2021.10.1.3.1. "
                          "SPARK does not load MIBs, so names like UCD-SNMP-MIB::laLoad.1 "
                          "need translating first (snmptranslate -On).")
    if int(oid.split(".", 1)[0]) > 2:
        raise MetricError("An OID starts with 0, 1 or 2.")
    return oid


def parse_number(value: Any) -> tuple[float | None, str | None]:
    """(number, None) or (None, why not). Text is read up to its first number."""
    if isinstance(value, bool):
        return None, "Not a number."
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        match = _NUMBER.match(value.strip())
        if not match:
            shown = value.strip()[:40] or "empty"
            return None, f"Not a number: {shown!r}."
        number = float(match.group(0))
    else:
        return None, "Not a number."
    if not math.isfinite(number):
        return None, "Not a finite number."
    return number, None


def extend_base(name: str) -> str:
    """The nsExtendOutLine subtree for one extend: its name, length-prefixed."""
    if not _EXTEND_NAME.match(name or ""):
        raise MetricError("An extend name is letters, digits, dots, dashes and underscores "
                          "-- the name after `extend` in snmpd.conf.")
    return f"{NS_EXTEND_OUT_LINE}.{len(name)}." + ".".join(str(ord(c)) for c in name)


def fmt_value(value: float | None, unit: str | None = None) -> str:
    """A reading for a person: whole numbers whole, small ones to three figures."""
    if value is None:
        return "—"
    if value == int(value) and abs(value) < 1e15:
        text = f"{int(value):,}"
    elif abs(value) >= 100:
        text = f"{value:,.0f}"
    elif abs(value) >= 10:
        text = f"{value:.1f}".removesuffix(".0")
    else:
        text = f"{value:.3g}"
    if not unit:
        return text
    return f"{text}{unit}" if unit.startswith(("%", "°")) else f"{text} {unit}"


def _float(raw: str, what: str) -> float:
    try:
        value = float(str(raw).strip().replace(",", ""))
    except ValueError:
        raise MetricError(f"{what} must be a number.") from None
    if not math.isfinite(value):
        raise MetricError(f"{what} must be a number.")
    return value


@dataclass
class MetricInput:
    """The add/edit form, as text, before validation."""

    name: str = ""
    oid: str = ""
    unit: str = ""
    scale: str = "1"
    alert_op: str = ""
    alert_value: str = ""
    alert_minutes: str = "5"


@dataclass(frozen=True)
class Clean:
    name: str
    oid: str
    unit: str | None
    scale: float
    alert_op: CustomAlertOp | None
    alert_value: float | None
    alert_minutes: int


def validate(form: MetricInput) -> Clean:
    name = " ".join((form.name or "").split())
    if not name:
        raise MetricError("Give the metric a name.")
    if len(name) > 64:
        raise MetricError("A name is at most 64 characters.")
    unit = (form.unit or "").strip() or None
    if unit is not None and not _UNIT.match(unit):
        raise MetricError("A unit is at most 16 characters, without < > & or quotes.")
    scale = _float(form.scale or "1", "Scale")
    if scale <= 0 or scale > SCALE_LIMIT or scale < 1 / SCALE_LIMIT:
        raise MetricError("Scale must be a positive number, like 1, 0.001 or 1024.")

    op_text = (form.alert_op or "").strip().lower()
    if op_text in ("", "none", "off"):
        op, value = None, None
    elif op_text in (CustomAlertOp.ABOVE, CustomAlertOp.BELOW):
        op = CustomAlertOp(op_text)
        if not (form.alert_value or "").strip():
            raise MetricError("Enter the value to alert at, or choose no alert.")
        value = _float(form.alert_value, "The alert value")
    else:
        raise MetricError("Choose above, below or no alert.")
    try:
        minutes = int(str(form.alert_minutes or "5").strip())
    except ValueError:
        raise MetricError("Minutes must be a whole number.") from None
    low, high = MINUTES_RANGE
    if not low <= minutes <= high:
        raise MetricError(f"Minutes must be between {low} and {high}.")
    return Clean(name, normalize_oid(form.oid), unit, scale, op, value, minutes)


def alert_key(metric_id: int) -> str:
    return f"custom:{metric_id}"


# --------------------------------------------------------------------------
# Saving
# --------------------------------------------------------------------------


async def metrics_for(session: AsyncSession, row_id: int) -> list[SnmpCustomMetric]:
    return list((await session.execute(
        select(SnmpCustomMetric).where(SnmpCustomMetric.snmp_device_id == row_id)
        .order_by(SnmpCustomMetric.name, SnmpCustomMetric.id)
    )).scalars())


async def _check_room(session: AsyncSession, row_id: int, oid: str, adding: int,
                      exclude_id: int | None = None) -> None:
    query = select(SnmpCustomMetric.id).where(SnmpCustomMetric.snmp_device_id == row_id,
                                              SnmpCustomMetric.oid == oid)
    if exclude_id is not None:
        query = query.where(SnmpCustomMetric.id != exclude_id)
    if await session.scalar(query.limit(1)) is not None:
        raise MetricError(f"This device already has a metric reading {oid}.")
    if adding:
        count = await session.scalar(select(func.count()).select_from(SnmpCustomMetric).where(
            SnmpCustomMetric.snmp_device_id == row_id)) or 0
        if count + adding > MAX_PER_DEVICE:
            raise MetricError(f"A device can have at most {MAX_PER_DEVICE} custom metrics.")


async def add_metric(session: AsyncSession, row_id: int, form: MetricInput) -> SnmpCustomMetric:
    clean = validate(form)
    await _check_room(session, row_id, clean.oid, adding=1)
    metric = SnmpCustomMetric(snmp_device_id=row_id)
    _apply(metric, clean)
    session.add(metric)
    await session.flush()
    return metric


async def update_metric(session: AsyncSession, metric: SnmpCustomMetric,
                        form: MetricInput) -> None:
    clean = validate(form)
    await _check_room(session, metric.snmp_device_id, clean.oid, adding=0, exclude_id=metric.id)
    rule_changed = (clean.oid != metric.oid or clean.scale != metric.scale
                    or clean.alert_op != metric.alert_op
                    or clean.alert_value != metric.alert_value
                    or clean.alert_minutes != metric.alert_minutes)
    if clean.oid != metric.oid:
        # A different OID is a different thing; its readings are not this one's.
        metric.last_value = metric.last_read_at = metric.last_error = None
    _apply(metric, clean)
    if rule_changed:
        # A half-counted streak or a standing alert belongs to the old rule.
        await snmp_alerts.forget(session, alert_key(metric.id))


async def delete_metric(session: AsyncSession, metric: SnmpCustomMetric) -> None:
    await snmp_alerts.forget(session, alert_key(metric.id))
    await session.delete(metric)


def _apply(metric: SnmpCustomMetric, clean: Clean) -> None:
    metric.name = clean.name
    metric.oid = clean.oid
    metric.unit = clean.unit
    metric.scale = clean.scale
    metric.alert_op = clean.alert_op
    metric.alert_value = clean.alert_value
    metric.alert_minutes = clean.alert_minutes


# --------------------------------------------------------------------------
# Reading (the network half; no database)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Reading:
    value: float | None
    error: str | None = None


async def read(collector, metrics: list[tuple[int, str]]) -> dict[int, Reading]:  # type: ignore[no-untyped-def]
    """Ask the device for each (metric id, OID). Never raises: a metric the
    device cannot answer gets a reading that says why."""
    out: dict[int, Reading] = {}
    for start in range(0, len(metrics), GET_GROUP):
        group = metrics[start:start + GET_GROUP]
        try:
            values = await collector.get(*(oid for _, oid in group))
        except Exception as exc:  # noqa: BLE001 - a poll must not die on one OID
            why = str(exc) or type(exc).__name__
            for mid, _ in group:
                out[mid] = Reading(None, f"No answer: {why}"[:255])
            continue
        values = {key.lstrip("."): value for key, value in values.items()}
        for mid, oid in group:
            if oid not in values:
                out[mid] = Reading(None, "The device has no value at this OID.")
                continue
            number, why = parse_number(values[oid])
            out[mid] = Reading(number, why)
    return out


# --------------------------------------------------------------------------
# Recording and the threshold rule (in the poll's transaction)
# --------------------------------------------------------------------------


async def record(session: AsyncSession, *, row_id: int, device: Device | None,
                 readings: dict[int, Reading], now: datetime, interval: int) -> None:
    """Keep one answered poll's readings, then decide each metric's rule."""
    if not readings:
        return
    metrics = [m for m in await metrics_for(session, row_id) if m.id in readings]
    for metric in metrics:
        reading = readings[metric.id]
        metric.last_read_at = now
        metric.last_value = reading.value
        metric.last_error = None if reading.value is not None else (reading.error or "No value.")
        if reading.value is not None:
            session.add(SnmpCustomSample(metric_id=metric.id, ts=now, value=reading.value))
    if device is not None:
        await _evaluate(session, device=device, metrics=metrics, now=now, interval=interval)


async def _evaluate(session: AsyncSession, *, device: Device, metrics: list[SnmpCustomMetric],
                    now: datetime, interval: int) -> None:
    ruled = [m for m in metrics if m.alert_op is not None and m.alert_value is not None]
    if not ruled:
        return
    settings = await alerts.load(session)
    quiet = await alerts.is_quiet(session, device_id=device.id, when=now)
    sending = settings.get("enabled", True) and not quiet
    recoveries = settings.get("notify_on_recovery", True)
    address = f"`{device.primary_ip or '—'}`"

    for metric in ruled:
        value = metric.current
        if value is None:
            continue      # nothing read this time: neither breach nor all-clear
        limit = float(metric.alert_value)  # type: ignore[arg-type]
        margin = abs(limit) * CLEAR_MARGIN
        above = metric.alert_op == CustomAlertOp.ABOVE
        breached = value > limit if above else value < limit
        cleared = value <= limit - margin if above else value >= limit + margin
        word = "above" if above else "below"
        minutes = metric.alert_minutes or 1
        shown, line = fmt_value(value, metric.unit), fmt_value(limit, metric.unit)
        key = alert_key(metric.id)
        subject = f"{device.display_name}: {metric.name} is {word} {line} ({shown})"
        body = f"{address} — {word} {line} for {minutes} minute{'s' if minutes != 1 else ''}."
        out = await snmp_alerts.step(
            session, key, breached=breached, cleared=cleared, value=value, now=now,
            hold=timedelta(minutes=minutes), min_polls=1, interval=interval,
            record=snmp_alerts.Record(device.id, subject, body),
        )
        if out.fired and sending:
            await alerts.enqueue(
                session, kind="custom_metric", tone="bad", subject=subject, body=body,
                dedupe_key=f"rule:{key}:{out.since.isoformat()}:fire",
            )
            await snmp_alerts._mark_notified(session, key)
        elif out.cleared and out.notified and recoveries and sending:
            await alerts.enqueue(
                session, kind="custom_metric_ok", tone="ok",
                subject=f"{device.display_name}: {metric.name} back to normal ({shown})",
                body=f"{address} — was {word} {line} for "
                     f"{human_duration((now - out.since).total_seconds())}.",
                dedupe_key=f"rule:{key}:{out.since.isoformat()}:clear",
            )


# --------------------------------------------------------------------------
# Importing a net-snmp extend
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtendLine:
    line: int
    oid: str
    text: str
    value: float | None


async def read_extend(session: AsyncSession, vault: Vault, row_id: int,
                      name: str, *, timeout: float = 3.0) -> list[ExtendLine]:
    """Walk one extend's output lines on the device, for the import form.

    Ends the read transaction before touching the network, like Test
    (snmp_config.probe_device). Raises MetricError with a message for a
    person when there is nothing to show.
    """
    from .collectors import SnmpCollector
    from .snmp_config import credential_for

    base = extend_base(name)
    row = await session.get(SnmpDevice, row_id)
    if row is None:
        raise MetricError("This device is no longer on the SNMP list.")
    device = await session.get(Device, row.device_id)
    profile = await session.get(SnmpProfile, row.profile_id)
    address = device.primary_ip if device else None
    if not address:
        raise MetricError("This device has no address to ask.")
    try:
        credential = credential_for(profile, vault, timeout=timeout, retries=1)
    except SecretUnavailable as exc:
        raise MetricError(str(exc)) from None
    await session.commit()  # end the read transaction before the network

    collector = SnmpCollector(address, credential)
    try:
        rows = await collector.walk(base, max_rows=MAX_PER_DEVICE * 4)
    except Exception as exc:  # noqa: BLE001 - report it on the page
        raise MetricError(f"The device did not answer: {str(exc) or type(exc).__name__}") from None
    finally:
        await collector.close()

    lines = []
    for index, value in rows.items():
        if not index.isdigit():
            continue
        number, _why = parse_number(value)
        lines.append(ExtendLine(int(index), f"{base}.{index}", str(value)[:80], number))
    lines.sort(key=lambda line: line.line)
    if not lines:
        raise MetricError(f"No output from an extend named {name!r}. Check the name after "
                          "`extend` in snmpd.conf, and that this device's SNMP profile "
                          "can read NET-SNMP-EXTEND-MIB (a v2c `rocommunity` or v3 `rouser` "
                          "without a view restriction can).")
    return lines


async def import_lines(session: AsyncSession, row_id: int,
                       forms: list[MetricInput]) -> list[SnmpCustomMetric]:
    """Add several metrics at once, or none: all are validated first."""
    cleans = [validate(form) for form in forms]
    if not cleans:
        raise MetricError("Name at least one line to add it.")
    oids = [c.oid for c in cleans]
    if len(set(oids)) != len(oids):
        raise MetricError("The same line is listed twice.")
    for i, clean in enumerate(cleans):
        await _check_room(session, row_id, clean.oid, adding=len(cleans) if i == 0 else 0)
    added = []
    for clean in cleans:
        metric = SnmpCustomMetric(snmp_device_id=row_id)
        _apply(metric, clean)
        session.add(metric)
        added.append(metric)
    await session.flush()
    return added


async def forget_device(session: AsyncSession, row_id: int) -> None:
    """Drop every custom metric's rule state before its device leaves the
    SNMP list. The metrics go by cascade; their alert_state rows would not,
    and SQLite reuses a deleted row's id, so a later metric could inherit a
    standing alert it never raised."""
    for metric in await metrics_for(session, row_id):
        await snmp_alerts.forget(session, alert_key(metric.id))
