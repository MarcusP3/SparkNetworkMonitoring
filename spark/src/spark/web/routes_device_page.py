"""One device: what it is, what it runs, and -- if SPARK polls it -- how it has been.

The Devices list answers "what is on my network". This page answers "how is
this one doing", which for a device on the SNMP list means history: CPU,
memory and temperature over the chosen range, and every interface with its
traffic. For a device SPARK does not poll it is a short page that says so and
where to change that.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import charts
from .. import snmp_history as history
from ..discovery.services import services_for
from ..models import (
    Device,
    SnmpDevice,
    SnmpInterface,
    SnmpPoll,
    SnmpProfile,
    Target,
    User,
    utcnow,
)
from ..config import Config
from .deps import get_config, get_session, redirect, require_user, templates

router = APIRouter()

RANGE_LABELS = {"1h": "1 hour", "24h": "24 hours", "7d": "7 days", "30d": "30 days"}


def _peaks_worth_drawing(series: history.Series) -> bool:
    """Whether the peak line says anything the average line does not.

    At one-minute buckets each holds one poll and peak equals average; drawing
    both would just thicken the line.
    """
    return any(
        p is not None and a is not None and p > a * 1.001 + 1e-9
        for a, p in zip(series.avg, series.peak)
    )


def _describe(series: history.Series, fmt) -> callable:  # type: ignore[no-untyped-def,valid-type]
    def describe(i: int) -> str:
        avg, peak = series.avg[i], series.peak[i]
        if avg is None and peak is None:
            return ""
        if peak is not None and avg is not None and peak > avg * 1.001:
            return f"{fmt(avg)} average · {fmt(peak)} peak"
        return fmt(avg if avg is not None else peak)
    return describe


def _health_charts(health: history.HealthHistory) -> list[dict]:
    """The health charts worth showing, in a fixed order. Empty ones are left out."""
    window = health.window
    out = []

    def add(key, label, series, *, y_max, fmt, note=""):  # type: ignore[no-untyped-def]
        out.append({
            "key": key,
            "label": label,
            "note": note,
            "latest": fmt(series.latest()),
            "peak": fmt(series.overall_peak()),
            "svg": charts.chart(
                [charts.Line(series.avg,
                             series.peak if _peaks_worth_drawing(series) else None,
                             fill=True)],
                window, y_max=y_max, fmt=fmt, title=label,
                describe=_describe(series, fmt),
            ),
        })

    if health.cpu.has_data:
        add("cpu", "CPU", health.cpu, y_max=100.0, fmt=charts.fmt_pct)
    elif health.load.has_data:
        top = max(v for v in health.load.avg + health.load.peak if v is not None)
        add("load", "Load average (1 min)", health.load,
            y_max=charts.nice_max(top * 1.1, floor=1.0), fmt=charts.fmt_load,
            note="This device reports no CPU percentage. Load is not a "
                 "percentage: 1.0 means one core's worth of work waiting.")
    if health.memory.has_data:
        add("memory", "Memory", health.memory, y_max=100.0, fmt=charts.fmt_pct)
    if health.temperature.has_data:
        top = health.temperature.overall_peak() or 0.0
        add("temperature", "Temperature (hottest sensor)", health.temperature,
            y_max=charts.nice_max(top * 1.15, floor=10.0), fmt=charts.fmt_temp)
    return out


def _state(iface: SnmpInterface, last_ok) -> tuple[str, str]:  # type: ignore[no-untyped-def]
    """(label, pill kind) for an interface's status.

    Only "up" gets a status colour. A port with nothing plugged in reads
    "down" to SNMP, and a switch is mostly such ports -- red for each would be
    a page of alarms about nothing.
    """
    if last_ok is not None and (iface.last_seen is None or iface.last_seen < last_ok):
        return "gone", "neutral"
    if iface.admin_status == "down":
        return "disabled", "neutral"
    if iface.oper_status == "up":
        return "up", "ok"
    return iface.oper_status or "unknown", "neutral"


def _traffic_chart(traffic: history.TrafficHistory, window: history.Window,
                   label: str):  # type: ignore[no-untyped-def]
    inbound, outbound = traffic.inbound, traffic.outbound
    values = [v for v in inbound.avg + inbound.peak + outbound.avg + outbound.peak
              if v is not None]
    y_max = charts.nice_max(max(values, default=0.0) * 1.05, floor=1000.0)
    show_peaks = _peaks_worth_drawing(inbound) or _peaks_worth_drawing(outbound)

    def describe(i: int) -> str:
        parts = []
        for name, series in (("In", inbound), ("Out", outbound)):
            avg, peak = series.avg[i], series.peak[i]
            if avg is None:
                continue
            text = f"{name} {charts.fmt_bps(avg)}"
            if peak is not None and peak > avg * 1.001:
                text += f" (peak {charts.fmt_bps(peak)})"
            parts.append(text)
        return " · ".join(parts)

    return charts.chart(
        [
            charts.Line(inbound.avg, inbound.peak if show_peaks else None,
                        kind="primary", label="In", fill=True),
            charts.Line(outbound.avg, outbound.peak if show_peaks else None,
                        kind="secondary", label="Out"),
        ],
        window, y_max=y_max, fmt=charts.fmt_bps, title=f"Traffic on {label}",
        describe=describe,
    )


async def _snmp_section(session: AsyncSession, device: Device, range_name: str,
                        port: int | None) -> dict | None:
    row = await session.scalar(select(SnmpDevice).where(SnmpDevice.device_id == device.id))
    if row is None:
        return None
    profile = await session.get(SnmpProfile, row.profile_id)
    poll = await session.get(SnmpPoll, row.id)
    now = utcnow()
    window = history.Window.ending(now, range_name)

    health = await history.health_history(session, row.id, window)
    interfaces = list(
        (await session.execute(
            select(SnmpInterface)
            .where(SnmpInterface.snmp_device_id == row.id)
            .order_by(SnmpInterface.if_index)
        )).scalars()
    )
    traffic = await history.traffic_history(session, [i.id for i in interfaces], window)
    last_ok = poll.last_ok_at if poll else None

    entries = []
    for iface in interfaces:
        t = traffic.get(iface.id)
        label, kind = _state(iface, last_ok)
        entries.append({
            "iface": iface,
            "state": label,
            "kind": kind,
            "traffic": t,
            "spark": charts.sparkline(t.inbound.avg, t.outbound.avg) if t else None,
            "errors": (t.in_errors + t.out_errors) if t else 0,
            "peak_in": charts.fmt_bps(t.inbound.overall_peak()) if t else "—",
            "peak_out": charts.fmt_bps(t.outbound.overall_peak()) if t else "—",
            "now_in": charts.fmt_bps(iface.in_bps) if label == "up" else "—",
            "now_out": charts.fmt_bps(iface.out_bps) if label == "up" else "—",
        })

    with_data = [e for e in entries if e["traffic"] and e["traffic"].has_data]
    selected = next((e for e in entries if e["iface"].id == port), None)
    if selected is None and with_data:
        selected = max(with_data, key=lambda e: e["traffic"].busy)

    selected_chart = None
    if selected is not None and selected["traffic"] and selected["traffic"].has_data:
        selected_chart = _traffic_chart(selected["traffic"], window, selected["iface"].label)

    return {
        "row": row,
        "profile": profile,
        "poll": poll,
        "window": window,
        "health": health,
        "health_charts": _health_charts(health),
        "answered": health.answered_fraction,
        "interfaces": entries,
        "up": sum(1 for e in entries if e["state"] == "up"),
        "selected": selected,
        "selected_chart": selected_chart,
        "uptime": _uptime(poll.uptime_seconds) if poll and poll.last_ok_at else None,
        "polled_ago": _ago(poll.last_polled_at, now) if poll else None,
    }


def _uptime(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    total = int(seconds)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _ago(when, now) -> str | None:  # type: ignore[no-untyped-def]
    if when is None:
        return None
    seconds = max(0, int((now - when).total_seconds()))
    if seconds < 90:
        return f"{seconds}s ago"
    if seconds < 5400:
        return f"{round(seconds / 60)}m ago"
    if seconds < 129600:
        return f"{round(seconds / 3600)}h ago"
    return f"{round(seconds / 86400)}d ago"


@router.get("/devices/{device_id}")
async def device_page(
    request: Request,
    device_id: int,
    range: str = "",  # noqa: A002 - the query parameter's name in the URL
    port: str = "",
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    device = await session.get(Device, device_id)
    if device is None:
        return redirect("/devices")
    range_name = history.parse_range(range)
    try:
        port_id = int(port) if port else None
    except ValueError:
        port_id = None

    services = (await services_for(session, [device.id])).get(device.id, [])
    watched = await session.scalar(
        select(func.count(Target.id)).where(Target.device_id == device.id)
    )
    snmp = await _snmp_section(session, device, range_name, port_id)
    has_profiles = bool(await session.scalar(select(func.count(SnmpProfile.id))))

    return templates.TemplateResponse(
        request,
        "device.html",
        {
            "config": config,
            "user": user,
            "title": device.display_name,
            "device": device,
            "services": services,
            "watched": watched or 0,
            "snmp": snmp,
            "has_profiles": has_profiles,
            "range": range_name,
            "ranges": [(key, RANGE_LABELS[key]) for key in history.RANGES],
            "port": port_id,
        },
    )
