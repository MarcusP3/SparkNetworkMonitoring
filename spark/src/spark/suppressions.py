"""Suppressions: one alert rule, for one device, switched off or given its
own line (Settings -> Suppressions).

The rules under Settings -> Alerts apply to every device. Some devices break
them by design -- ZFS keeps TrueNAS's memory nearly full on purpose -- and
muting the whole device would lose everything else about it. A suppression
is narrower: "memory, on the NAS, off" or "memory, on the NAS, over 100%".
TrueNAS's own alerts can be suppressed one type at a time (PoolUSBDisks).

A suppressed rule is fully quiet: no message and no incident. (The mute list
is the other thing: still recorded, only not sent.) Saving or removing one
starts that rule afresh for that device, so an alert standing at the time
closes as "suppressed" rather than hanging on in the band below its old line.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import snmp_alerts
from .models import (AlertState, AlertSuppression, ApiCredential, Device, SnmpDevice,
                     SnmpInterface, utcnow)

OFF = None
SUPPRESSED = "suppressed"


@dataclass(frozen=True)
class Rule:
    label: str
    unit: str | None = None          # None: off only
    bounds: tuple[int, int] | None = None
    limit_key: str | None = None     # the global line, in alert_rules


PERCENT = snmp_alerts.PERCENT_RANGE
CELSIUS = snmp_alerts.CELSIUS_RANGE

RULES: dict[str, Rule] = {
    "cpu": Rule("CPU", "%", PERCENT, "cpu_percent"),
    "memory": Rule("Memory", "%", PERCENT, "memory_percent"),
    "temperature": Rule("Temperature", "°C", CELSIUS, "temperature_celsius"),
    "port_down": Rule("A starred port goes down"),
    "port_busy": Rule("A starred port is busy", "%", PERCENT, "port_busy_percent"),
    "snmp_down": Rule("Stops answering SNMP"),
    "pool_health": Rule("A pool is not ONLINE"),
    "pool_space": Rule("Pool space", "%", PERCENT, "pool_space_percent"),
    "disk_space": Rule("Disk space", "%", PERCENT, "disk_space_percent"),
    "drive_temperature": Rule("Drive temperature", "°C", CELSIUS, "drive_celsius"),
    "drive_errors": Rule("A drive is not ONLINE, or has errors"),
    "api_down": Rule("An API credential stops working"),
    "truenas_alerts": Rule("A TrueNAS alert"),
}

# The rule behind an alert_state / incident key, by its prefix.
BY_PREFIX = {
    "cpu": "cpu", "memory": "memory", "temperature": "temperature",
    "port": "port_down", "busy": "port_busy", "snmpdown": "snmp_down",
    "pool": "pool_health", "drive": "drive_temperature",
    "api": "api_down", "apidrive": "drive_errors", "tnalert": "truenas_alerts",
}


class SuppressionError(ValueError):
    """A value the form cannot save. The message is for a person."""


def rule_of(key: str) -> str | None:
    head, _, rest = key.partition(":")
    if head == "space":
        return "pool_space" if rest.split(":")[1:2] == ["pool"] else "disk_space"
    return BY_PREFIX.get(head)


def describe(row: AlertSuppression) -> str:
    rule = RULES.get(row.rule)
    what = rule.label if rule else row.rule
    if row.rule == "truenas_alerts":
        what = f"TrueNAS alert {row.detail}" if row.detail else "Every TrueNAS alert"
    if row.threshold is None:
        return f"{what}: off"
    return f"{what}: alert over {row.threshold:g}{rule.unit if rule else ''}"


class Overrides:
    """One device's suppressions, looked up while its alerts are decided."""

    def __init__(self, rows: list[AlertSuppression]) -> None:
        self._rows = {(r.rule, r.detail or None): r for r in rows}

    def off(self, rule: str, detail: str | None = None) -> bool:
        row = self._rows.get((rule, detail)) or (
            self._rows.get((rule, None)) if detail is not None else None)
        return row is not None and row.threshold is None

    def line(self, rule: str, default: float) -> float:
        row = self._rows.get((rule, None))
        return float(row.threshold) if row is not None and row.threshold is not None else default


async def for_device(session: AsyncSession, device_id: int | None) -> Overrides:
    if device_id is None:
        return Overrides([])
    rows = (await session.execute(
        select(AlertSuppression).where(AlertSuppression.device_id == device_id)
    )).scalars().all()
    return Overrides(list(rows))


async def listing(session: AsyncSession) -> list[dict]:
    rows = (await session.execute(
        select(AlertSuppression, Device)
        .join(Device, Device.id == AlertSuppression.device_id)
        .order_by(func.lower(func.coalesce(Device.friendly_name, Device.hostname,
                                           Device.primary_ip, "")),
                  AlertSuppression.rule)
    )).all()
    return [{"row": row, "device": device, "text": describe(row)} for row, device in rows]


async def save(session: AsyncSession, *, device_id: int | None, rule: str, mode: str,
               threshold: str, detail: str) -> AlertSuppression:
    """Add one, or change the one already there for this device and rule."""
    device = await session.get(Device, device_id) if device_id is not None else None
    if device is None:
        raise SuppressionError("Choose a device.")
    spec = RULES.get(rule)
    if spec is None:
        raise SuppressionError("Choose which alert.")
    detail = (detail or "").strip() if rule == "truenas_alerts" else ""
    if len(detail) > 64:
        raise SuppressionError("That TrueNAS alert type is longer than any there is.")
    value: float | None = OFF
    if mode == "line":
        if spec.unit is None:
            raise SuppressionError(f"“{spec.label}” has no line to move: it can only be off.")
        low, high = spec.bounds  # type: ignore[misc]
        try:
            value = float(int(str(threshold).strip()))
        except ValueError:
            raise SuppressionError(f"The line must be a whole number of {spec.unit}.") from None
        if not low <= value <= high:
            raise SuppressionError(f"The line must be between {low} and {high}{spec.unit}.")
    elif mode != "off":
        raise SuppressionError("Choose off, or its own line.")
    same_detail = (AlertSuppression.detail == detail) if detail else AlertSuppression.detail.is_(None)
    row = await session.scalar(select(AlertSuppression).where(
        AlertSuppression.device_id == device.id, AlertSuppression.rule == rule, same_detail))
    if row is None:
        row = AlertSuppression(device_id=device.id, rule=rule, detail=detail or None)
        session.add(row)
    row.threshold = value
    await session.flush()
    await reset(session, device.id, rule, detail or None)
    return row


async def remove(session: AsyncSession, row: AlertSuppression) -> None:
    device_id, rule, detail = row.device_id, row.rule, row.detail
    await session.delete(row)
    await session.flush()
    await reset(session, device_id, rule, detail)


async def reset(session: AsyncSession, device_id: int, rule: str, detail: str | None) -> None:
    """Start this rule afresh for this device: its state goes, and an incident
    open for it closes as suppressed. Without this an alert that fired below
    a line just raised would stand for ever in the band under it."""
    keys, prefixes = await _keys(session, device_id, rule, detail)
    now = utcnow()
    for key in keys:
        await session.execute(delete(AlertState).where(AlertState.key == key))
        await snmp_alerts.close_incidents(session, now, key=key, resolution=SUPPRESSED)
    for prefix in prefixes:
        await session.execute(delete(AlertState).where(AlertState.key.startswith(prefix)))
        await snmp_alerts.close_incidents(session, now, prefix=prefix, resolution=SUPPRESSED)


async def _keys(session: AsyncSession, device_id: int, rule: str,
                detail: str | None) -> tuple[list[str], list[str]]:
    snmp_row = await session.scalar(select(SnmpDevice.id).where(SnmpDevice.device_id == device_id))
    creds = list((await session.execute(
        select(ApiCredential).where(ApiCredential.device_id == device_id))).scalars())
    keys: list[str] = []
    prefixes: list[str] = []
    if snmp_row is not None:
        if rule in ("cpu", "memory", "temperature"):
            keys.append(f"{rule}:{snmp_row}")
        elif rule in ("port_down", "port_busy"):
            head = "port" if rule == "port_down" else "busy"
            ids = (await session.execute(
                select(SnmpInterface.id).where(SnmpInterface.snmp_device_id == snmp_row))).scalars()
            keys.extend(f"{head}:{i}" for i in ids)
        elif rule == "snmp_down":
            keys.append(f"snmpdown:{snmp_row}")
        elif rule == "pool_health":
            prefixes.append(f"pool:{snmp_row}:")
        elif rule == "pool_space":
            prefixes.append(f"space:{snmp_row}:pool:")
        elif rule == "disk_space":
            prefixes.append(f"space:{snmp_row}:fs:")
        elif rule == "drive_temperature":
            prefixes.append(f"drive:{snmp_row}:")
    for cred in creds:
        if rule == "api_down":
            keys.append(f"api:{cred.id}")
        elif rule == "drive_errors":
            prefixes.append(f"apidrive:{cred.id}:")
        elif rule == "truenas_alerts":
            if detail is None:
                prefixes.append(f"tnalert:{cred.id}:")
            else:
                for a in (cred.readings or {}).get("alerts") or []:
                    if a.get("klass") == detail:
                        keys.append(f"tnalert:{cred.id}:{a['uuid']}"[:64])
    return keys, prefixes


async def klass_of(session: AsyncSession, key: str) -> str | None:
    """The TrueNAS alert type behind a "tnalert:<credential>:<uuid>" key."""
    _, cred_id, uuid = (key.split(":", 2) + ["", ""])[:3]
    if not cred_id.isdigit():
        return None
    cred = await session.get(ApiCredential, int(cred_id))
    for a in ((cred.readings or {}).get("alerts") or []) if cred else []:
        if f"{a.get('uuid')}"[:len(uuid)] == uuid:
            return a.get("klass")
    return None
