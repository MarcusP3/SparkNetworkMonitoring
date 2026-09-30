"""Threshold alerts from SNMP polling: ports down or busy, CPU, memory, heat.

Decided in the poll's own transaction, like every other alert (alerts.py):
record_poll calls `evaluate` after writing what it saw, and whatever it
decides commits with that poll or not at all.

Each rule is a small state machine per device or port, kept in `alert_state`:

  * the condition must hold on every poll for the whole of its time -- "CPU
    over 90% for 10 minutes" -- or on two polls in a row for a port going
    down, so a single spike or a port bouncing through a reboot is not news;
  * it fires once, and says nothing more while it continues;
  * it clears only once the value is back under the line by a margin, so a
    CPU sitting at 89-91% does not send a message every few minutes;
  * a recovery is sent only if the alert was -- a muted device, or a rule
    switched off, fires silently and so recovers silently.

Ports alert only when starred on their device's page. The others apply to
every polled device, minus the mute list.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import alerts
from .charts import fmt_bps
from .db import get_setting, save_setting
from .engine.state import human_duration
from .models import DEFAULT_SETTINGS, AlertIncident, AlertState, Device, SnmpInterface, SnmpPoll

SETTING = "alert_rules"

# How far back under the line a value must come before a fired alert clears.
PERCENT_MARGIN = 5.0
CELSIUS_MARGIN = 5.0

# A breach streak broken by a gap longer than this many poll intervals (the
# device unreachable, SPARK restarting) starts again rather than counting
# the time it could not see.
GAP_POLLS = 3

# A port must be seen down on this many polls in a row.
PORT_DOWN_POLLS = 2

# Bounds for what the settings form accepts.
PERCENT_RANGE = (1, 100)
CELSIUS_RANGE = (30, 120)
MINUTES_RANGE = (1, 1440)


@dataclass(frozen=True)
class Metric:
    """A device-wide reading with a threshold: CPU, memory, temperature."""

    key: str            # rule name in settings, and the alert_state prefix
    field: str          # attribute on SnmpPoll
    limit_key: str      # the threshold's settings key
    unit: str
    margin: float
    what: str           # for messages: "CPU", "Memory"

    def show(self, value: float) -> str:
        return f"{value:.0f}{self.unit}"


METRICS = (
    Metric("cpu", "cpu_percent", "cpu_percent", "%", PERCENT_MARGIN, "CPU"),
    Metric("memory", "memory_percent", "memory_percent", "%", PERCENT_MARGIN, "Memory"),
    Metric("temperature", "temperature_max", "temperature_celsius", "°C", CELSIUS_MARGIN,
           "Temperature"),
)


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


async def load(session: AsyncSession) -> dict:
    """The saved rules over the defaults, so a rule added in a later version
    (storage, APIs) is on -- and shown ticked -- for a setup that saved the
    form before it existed."""
    return {**DEFAULT_SETTINGS[SETTING], **await get_setting(session, SETTING)}


class RuleError(ValueError):
    """A value the settings form cannot save. The message is for a person."""


def _bounded(raw: str, low: int, high: int, what: str) -> int:
    try:
        value = int(str(raw).strip())
    except ValueError:
        raise RuleError(f"{what} must be a whole number.") from None
    if not low <= value <= high:
        raise RuleError(f"{what} must be between {low} and {high}.")
    return value


async def save(session: AsyncSession, form: dict[str, str]) -> None:
    """Validate the whole form, then save it. Nothing is saved on an error."""
    rules = await load(session)
    checked = {
        "port_down": bool(form.get("port_down")),
        "port_busy": bool(form.get("port_busy")),
        "port_busy_percent": _bounded(form.get("port_busy_percent", ""), *PERCENT_RANGE,
                                      "Port traffic"),
        "port_busy_minutes": _bounded(form.get("port_busy_minutes", ""), *MINUTES_RANGE,
                                      "Port traffic minutes"),
        "cpu": bool(form.get("cpu")),
        "cpu_percent": _bounded(form.get("cpu_percent", ""), *PERCENT_RANGE, "CPU"),
        "cpu_minutes": _bounded(form.get("cpu_minutes", ""), *MINUTES_RANGE, "CPU minutes"),
        "memory": bool(form.get("memory")),
        "memory_percent": _bounded(form.get("memory_percent", ""), *PERCENT_RANGE, "Memory"),
        "memory_minutes": _bounded(form.get("memory_minutes", ""), *MINUTES_RANGE,
                                   "Memory minutes"),
        "temperature": bool(form.get("temperature")),
        "temperature_celsius": _bounded(form.get("temperature_celsius", ""), *CELSIUS_RANGE,
                                        "Temperature"),
        "temperature_minutes": _bounded(form.get("temperature_minutes", ""), *MINUTES_RANGE,
                                        "Temperature minutes"),
        # Storage (storage.py).
        "pool_health": bool(form.get("pool_health")),
        "pool_space": bool(form.get("pool_space")),
        "pool_space_percent": _bounded(form.get("pool_space_percent", ""), *PERCENT_RANGE,
                                       "Pool space"),
        "disk_space": bool(form.get("disk_space")),
        "disk_space_percent": _bounded(form.get("disk_space_percent", ""), *PERCENT_RANGE,
                                       "Disk space"),
        "drive_temperature": bool(form.get("drive_temperature")),
        "drive_celsius": _bounded(form.get("drive_celsius", ""), *CELSIUS_RANGE,
                                  "Drive temperature"),
        "drive_minutes": _bounded(form.get("drive_minutes", ""), *MINUTES_RANGE,
                                  "Drive temperature minutes"),
        # API credentials (credentials.py).
        "api_down": bool(form.get("api_down")),
        "drive_errors": bool(form.get("drive_errors")),
        "truenas_alerts": bool(form.get("truenas_alerts")),
    }
    rules.update(checked)
    await save_setting(session, SETTING, rules)


# --------------------------------------------------------------------------
# The state machine
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Record:
    """What the dashboard's incident says while a fired rule stands: the
    alert's own subject and body. None when the rule is switched off."""

    device_id: int | None
    title: str
    detail: str | None = None


RECOVERED = "recovered"
GONE = "no longer watched"


async def open_incident(session: AsyncSession, key: str, record: Record,
                        since: datetime) -> None:
    """The open incident for this rule, made if there is none yet -- also for
    an alert that fired before incidents were recorded."""
    found = await session.scalar(select(AlertIncident.id).where(
        AlertIncident.key == key, AlertIncident.closed_at.is_(None)).limit(1))
    if found is None:
        session.add(AlertIncident(
            key=key, device_id=record.device_id, title=record.title[:256],
            detail=(record.detail or "").replace("`", "")[:2000] or None, opened_at=since))


async def close_incidents(session: AsyncSession, now: datetime, *, key: str | None = None,
                          prefix: str | None = None, resolution: str = RECOVERED) -> None:
    query = select(AlertIncident).where(AlertIncident.closed_at.is_(None))
    if key is not None:
        query = query.where(AlertIncident.key == key)
    if prefix is not None:
        query = query.where(AlertIncident.key.startswith(prefix))
    for row in (await session.execute(query)).scalars():
        row.closed_at, row.resolution = now, resolution


@dataclass
class Outcome:
    fired: bool = False       # crossed into alerting on this poll
    cleared: bool = False     # a fired alert ended on this poll
    notified: bool = False    # the alert that just cleared had been sent
    since: datetime | None = None


async def step(session: AsyncSession, key: str, *, breached: bool, cleared: bool,
               value: float | None, now: datetime, hold: timedelta, min_polls: int,
               interval: int, record: Record | None = None) -> Outcome:
    """Advance one rule by one poll.

    `breached`: over the line now. `cleared`: back under it by the margin.
    Neither: in between, which holds a fired alert and breaks an unfired
    streak. The caller decides whether to send anything.

    `record`: while fired, keep an incident open for the dashboard; closed
    when the rule clears.
    """
    out = await _step(session, key, breached=breached, cleared=cleared, value=value, now=now,
                      hold=hold, min_polls=min_polls, interval=interval)
    if out.cleared:
        await close_incidents(session, now, key=key)
    elif record is not None and out.since is not None:
        state = await session.get(AlertState, key)
        if state is not None and state.fired:
            await open_incident(session, key, record, out.since)
    return out


async def _step(session: AsyncSession, key: str, *, breached: bool, cleared: bool,
                value: float | None, now: datetime, hold: timedelta, min_polls: int,
                interval: int) -> Outcome:
    state = await session.get(AlertState, key)
    if state is not None and not state.fired:
        if (now - state.seen_at).total_seconds() > GAP_POLLS * max(interval, 1):
            await session.delete(state)
            await session.flush()
            state = None

    if breached:
        if state is None:
            state = AlertState(key=key, since=now, seen_at=now, polls=0)
            session.add(state)
        state.polls = (state.polls or 0) + 1
        state.seen_at = now
        state.value = value
        if (not state.fired and state.polls >= min_polls
                and now - state.since >= hold):
            state.fired = True
            return Outcome(fired=True, since=state.since)
        return Outcome(since=state.since)

    if state is None:
        return Outcome()
    if state.fired and not cleared:
        state.seen_at = now      # still in the band: the alert stands
        return Outcome(since=state.since)
    outcome = Outcome(cleared=state.fired, notified=bool(state.notified), since=state.since)
    await session.delete(state)
    return outcome


async def _mark_notified(session: AsyncSession, key: str) -> None:
    state = await session.get(AlertState, key)
    if state is not None:
        state.notified = True


# --------------------------------------------------------------------------
# Evaluating one poll
# --------------------------------------------------------------------------


def utilisation(iface: SnmpInterface) -> float | None:
    """The busier direction as a percentage of the link speed, or None."""
    if not iface.speed_mbps or iface.in_bps is None or iface.out_bps is None:
        return None
    return max(iface.in_bps, iface.out_bps) / (iface.speed_mbps * 1_000_000) * 100


async def evaluate(session: AsyncSession, *, row_id: int, device: Device, poll: SnmpPoll,
                   interfaces: list[SnmpInterface], now: datetime, interval: int) -> None:
    """Every rule, for one answered poll of one device."""
    settings = await alerts.load(session)
    rules = await load(session)
    muted = await alerts.is_muted(session, device_id=device.id)
    sending = settings.get("enabled", True) and not muted
    recoveries = settings.get("notify_on_recovery", True)
    address = f"`{device.primary_ip or '—'}`"

    for metric in METRICS:
        value = getattr(poll, metric.field)
        if value is None:
            continue      # not reported this time: neither breach nor all-clear
        limit = float(rules.get(metric.limit_key) or 0)
        key = f"{metric.key}:{row_id}"
        on = bool(rules.get(metric.key, True))
        minutes = int(rules.get(f"{metric.key}_minutes") or 1)
        hot = "is running hot" if metric.key == "temperature" else "is high"
        subject = f"{device.display_name}: {metric.what} {hot} ({metric.show(value)})"
        body = (f"{address} — over {metric.show(limit)} for {minutes} minute"
                f"{'s' if minutes != 1 else ''}.")
        out = await step(
            session, key, breached=value > limit, cleared=value <= limit - metric.margin,
            value=value, now=now, hold=timedelta(minutes=minutes),
            min_polls=1, interval=interval,
            record=Record(device.id, subject, body) if on else None,
        )
        if out.fired and on and sending:
            await alerts.enqueue(
                session, kind=f"{metric.key}_high", tone="bad",
                subject=subject, body=body,
                dedupe_key=f"rule:{key}:{out.since.isoformat()}:fire",
            )
            await _mark_notified(session, key)
        elif out.cleared and out.notified and recoveries and sending:
            await alerts.enqueue(
                session, kind=f"{metric.key}_ok", tone="ok",
                subject=f"{device.display_name}: {metric.what} back to normal ({metric.show(value)})",
                body=f"{address} — was over {metric.show(limit)} for "
                     f"{human_duration((now - out.since).total_seconds())}.",
                dedupe_key=f"rule:{key}:{out.since.isoformat()}:clear",
            )

    for iface in interfaces:
        if not iface.starred:
            continue
        await _port_down(session, iface, device, rules, now, interval, address,
                         sending, recoveries)
        await _port_busy(session, iface, device, rules, now, interval, address,
                         sending, recoveries)


async def _port_down(session, iface, device, rules, now, interval, address,  # type: ignore[no-untyped-def]
                     sending, recoveries) -> None:
    key = f"port:{iface.id}"
    down = (iface.oper_status or "").lower() != "up"
    on = bool(rules.get("port_down", True))
    disabled = (iface.admin_status or "").lower() == "down"
    subject = f"{device.display_name}: port {iface.label} is down"
    body = (f"{address} — " + ("switched off on the device (admin down)." if disabled
                               else f"link {iface.oper_status or 'lost'}."))
    out = await step(session, key, breached=down, cleared=not down, value=None, now=now,
                     hold=timedelta(0), min_polls=PORT_DOWN_POLLS, interval=interval,
                     record=Record(device.id, subject, body) if on else None)
    if out.fired and on and sending:
        await alerts.enqueue(
            session, kind="port_down", tone="bad", subject=subject, body=body,
            dedupe_key=f"rule:{key}:{out.since.isoformat()}:fire",
        )
        await _mark_notified(session, key)
    elif out.cleared and out.notified and recoveries and sending:
        await alerts.enqueue(
            session, kind="port_up", tone="ok",
            subject=f"{device.display_name}: port {iface.label} is back up",
            body=f"{address} — down for {human_duration((now - out.since).total_seconds())}.",
            dedupe_key=f"rule:{key}:{out.since.isoformat()}:clear",
        )


async def _port_busy(session, iface, device, rules, now, interval, address,  # type: ignore[no-untyped-def]
                     sending, recoveries) -> None:
    key = f"busy:{iface.id}"
    pct = utilisation(iface) if (iface.oper_status or "").lower() == "up" else None
    if pct is None:
        return
    limit = float(rules.get("port_busy_percent") or 80)
    minutes = int(rules.get("port_busy_minutes") or 10)
    on = bool(rules.get("port_busy", True))
    speed = fmt_bps(iface.speed_mbps * 1_000_000)
    subject = f"{device.display_name}: port {iface.label} is busy ({pct:.0f}% of {speed})"
    body = (f"{address} — over {limit:.0f}% for {minutes} minute{'s' if minutes != 1 else ''}. "
            f"In {fmt_bps(iface.in_bps)}, out {fmt_bps(iface.out_bps)}.")
    out = await step(session, key, breached=pct > limit, cleared=pct <= limit - PERCENT_MARGIN,
                     value=pct, now=now, hold=timedelta(minutes=minutes), min_polls=1,
                     interval=interval, record=Record(device.id, subject, body) if on else None)
    if out.fired and on and sending:
        await alerts.enqueue(
            session, kind="port_busy", tone="bad", subject=subject, body=body,
            dedupe_key=f"rule:{key}:{out.since.isoformat()}:fire",
        )
        await _mark_notified(session, key)
    elif out.cleared and out.notified and recoveries and sending:
        await alerts.enqueue(
            session, kind="port_calm", tone="ok",
            subject=f"{device.display_name}: port {iface.label} traffic back to normal ({pct:.0f}%)",
            body=f"{address} — was over {limit:.0f}% for "
                 f"{human_duration((now - out.since).total_seconds())}.",
            dedupe_key=f"rule:{key}:{out.since.isoformat()}:clear",
        )


async def forget(session: AsyncSession, *keys: str) -> None:
    """Drop rule state, e.g. when a port is unstarred: a later star starts
    fresh, and an incident open for it closes as no longer watched."""
    from .models import utcnow

    for key in keys:
        state = await session.get(AlertState, key)
        if state is not None:
            await session.delete(state)
        await close_incidents(session, utcnow(), key=key, resolution=GONE)
