"""One device: what it is, what it runs, and -- if SPARK polls it -- how it has been.

The Devices list answers "what is on my network". This page answers "how is
this one doing", which for a device on the SNMP list means history: CPU,
memory and temperature over the chosen range, and every interface with its
traffic. For a device SPARK does not poll it is a short page that says so and
where to change that.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import alerts, hierarchy, limits, merge, snmp_alerts
from .. import charts
from .. import snmp_history as history
from ..discovery.oui import is_locally_administered
from ..discovery.services import services_for
from ..models import (
    ROLE_LABELS,
    AlertMute,
    Device,
    DeviceAddress,
    DeviceRole,
    SnmpDevice,
    SnmpInterface,
    SnmpPoll,
    SnmpProfile,
    Target,
    User,
    utcnow,
)
from ..config import Config
from .deps import ItemId, get_config, get_session, redirect, require_user, templates

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


def _health_charts(health: history.HealthHistory, zone: dict) -> list[dict]:
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
                describe=_describe(series, fmt), **zone,
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
    # A starred port is one someone said matters; down is news there.
    return iface.oper_status or "unknown", "bad" if iface.starred else "neutral"


def _traffic_chart(traffic: history.TrafficHistory, window: history.Window,
                   label: str, zone: dict):  # type: ignore[no-untyped-def]
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
        describe=describe, **zone,
    )


async def _snmp_section(session: AsyncSession, device: Device, range_name: str,
                        port: int | None, zone: dict) -> dict | None:
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
        selected_chart = _traffic_chart(selected["traffic"], window, selected["iface"].label,
                                        zone)

    return {
        "row": row,
        "profile": profile,
        "poll": poll,
        "window": window,
        "health": health,
        "health_charts": _health_charts(health, zone),
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
    device_id: ItemId,
    range: str = "",  # noqa: A002 - the query parameter's name in the URL
    port: str = "",
    merged: str = "",
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    device = await session.get(Device, device_id)
    if device is None:
        return redirect("/devices")
    range_name = history.parse_range(range)
    port_id = limits.as_id(port) if port else None

    services = (await services_for(session, [device.id])).get(device.id, [])
    watched = await session.scalar(
        select(func.count(Target.id)).where(Target.device_id == device.id)
    )
    # Charts label their time axes in the zone chosen under Preferences.
    zone = {"tz": request.state.tz, "tz_name": request.state.tz_name}
    snmp = await _snmp_section(session, device, range_name, port_id, zone)
    has_profiles = bool(await session.scalar(select(func.count(SnmpProfile.id))))
    muted = await alerts.is_muted(session, device_id=device.id)
    placement = await _placement(session, device)
    addresses = await merge.addresses_of(session, device.id)
    merge_choices = sorted(
        (d for d in (await session.execute(
            select(Device).where(Device.id != device.id, Device.ignored.is_(False))
        )).scalars()),
        # Devices known only by address first: those are the usual duplicates.
        key=lambda d: (d.mac is not None, d.display_name.lower()),
    )

    return templates.TemplateResponse(
        request,
        "device.html",
        {
            "config": config,
            "user": user,
            "title": device.display_name,
            "device": device,
            "randomised": is_locally_administered(device.mac),
            "services": services,
            "watched": watched or 0,
            "snmp": snmp,
            "has_profiles": has_profiles,
            "range": range_name,
            "ranges": [(key, RANGE_LABELS[key]) for key in history.RANGES],
            "port": port_id,
            "muted": muted,
            "placement": placement,
            "addresses": addresses,
            "merge_choices": merge_choices,
            "merged": merged if merged.isdigit() else "",
            "back": request.url.path + (f"?{request.url.query}" if request.url.query else ""),
        },
    )


async def _placement(session: AsyncSession, device: Device) -> dict:
    """The "On the map" card: this device's role and parent, and the choices.

    A device cannot be connected to itself or to anything below it -- that
    would be a loop -- so those are left out of the list.
    """
    rows = (await session.execute(
        select(Device.id, Device.parent_device_id).where(Device.ignored.is_(False))
    )).all()
    parents = {row.id: row.parent_device_id for row in rows}
    below = hierarchy.descendants(parents, device.id)
    options = [
        d for d in (await session.execute(
            select(Device).where(Device.ignored.is_(False), Device.id != device.id)
        )).scalars()
        if d.id not in below
    ]
    order = list(ROLE_LABELS)
    options.sort(key=lambda d: (order.index(d.role), d.display_name.lower()))
    parent = await session.get(Device, device.parent_device_id) if device.parent_device_id else None
    return {
        "roles": [(role.value, label) for role, label in ROLE_LABELS.items()],
        "options": [(d, ROLE_LABELS[d.role]) for d in options],
        "parent": parent,
        "children": len(below),
    }


def _back_to_device(device_id: int, back: str) -> str:
    """This device's page as it was (range, port), and nowhere else."""
    from .routes_auth import _safe_next

    target = _safe_next(back)
    page = f"/devices/{device_id}"
    return target if target == page or target.startswith(page + "?") else page


@router.post("/devices/{device_id}/interfaces/{interface_id}/star")
async def star_interface(
    device_id: ItemId,
    interface_id: ItemId,
    starred: str = Form("", max_length=limits.SHORT),
    back: str = Form("", max_length=limits.URL),
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(require_user),
):
    """Star or unstar a port. Starred ports alert when they go down or stay busy."""
    iface = await session.get(SnmpInterface, interface_id)
    row = await session.get(SnmpDevice, iface.snmp_device_id) if iface else None
    if iface is not None and row is not None and row.device_id == device_id:
        iface.starred = starred == "1"
        if not iface.starred:
            # A later star starts from nothing, not from a half-counted streak.
            await snmp_alerts.forget(session, f"port:{iface.id}", f"busy:{iface.id}")
        await session.commit()
    return redirect(_back_to_device(device_id, back))


@router.post("/devices/{device_id}/mute")
async def mute_device(
    device_id: ItemId,
    muted: str = Form("", max_length=limits.SHORT),
    back: str = Form("", max_length=limits.URL),
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(require_user),
):
    """Put a device on the mute list, or take it off."""
    device = await session.get(Device, device_id)
    if device is not None:
        await set_device_muted(session, device_id, muted == "1")
        await session.commit()
    return redirect(_back_to_device(device_id, back))


async def set_device_muted(session: AsyncSession, device_id: int, muted: bool) -> None:
    row = await session.scalar(select(AlertMute).where(AlertMute.device_id == device_id))
    if muted and row is None:
        session.add(AlertMute(device_id=device_id))
    elif not muted and row is not None:
        await session.delete(row)



@router.post("/devices/{device_id}/place")
async def place_device(
    device_id: ItemId,
    role: str = Form("unknown", max_length=limits.SHORT),
    parent_id: str = Form("", max_length=limits.SHORT),
    back: str = Form("", max_length=limits.URL),
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(require_user),
):
    """Set where a device sits on the service map: its role and parent.

    A role or parent that is not one of the choices changes nothing; the page
    only offers valid ones, so anything else did not come from it.
    """
    device = await session.get(Device, device_id)
    if device is None:
        return redirect("/devices")
    try:
        new_role = DeviceRole(role)
    except ValueError:
        return redirect(_back_to_device(device_id, back))
    new_parent = limits.as_id(parent_id) if parent_id.strip() else None
    if new_parent is not None:
        parent = await session.get(Device, new_parent)
        rows = (await session.execute(select(Device.id, Device.parent_device_id))).all()
        if (parent is None or parent.ignored
                or not hierarchy.can_parent({r.id: r.parent_device_id for r in rows},
                                            device_id, new_parent)):
            return redirect(_back_to_device(device_id, back))
    elif parent_id.strip():
        return redirect(_back_to_device(device_id, back))
    device.role = new_role
    device.parent_device_id = new_parent
    await session.commit()
    return redirect(_back_to_device(device_id, back))



@router.get("/devices/{device_id}/merge")
async def merge_preview(
    request: Request,
    device_id: ItemId,
    other: str = "",
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    """Say exactly what merging would do, before doing it."""
    other_id = limits.as_id(other)
    if other_id is None:
        return redirect(f"/devices/{device_id}#merge")
    try:
        plan = await merge.plan(session, device_id, other_id)
        error = None
    except merge.MergeError as exc:
        plan, error = None, str(exc)
    return templates.TemplateResponse(
        request, "merge.html",
        {"config": config, "user": user, "title": "Merge devices", "plan": plan,
         "error": error, "device_id": device_id},
        status_code=400 if error else 200,
    )


@router.post("/devices/{device_id}/merge")
async def merge_devices(
    device_id: ItemId,
    other_id: str = Form("", max_length=limits.SHORT),
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(require_user),
):
    """Fold another device into this one. See merge.py for what moves."""
    other = limits.as_id(other_id)
    if other is None:
        return redirect(f"/devices/{device_id}")
    try:
        done = await merge.apply(session, device_id, other)
    except merge.MergeError:
        # The preview said why; a stale form resubmitted changes nothing.
        return redirect(f"/devices/{device_id}")
    await session.commit()
    return redirect(f"/devices/{device_id}?merged={len(done.addresses)}#addresses")


@router.post("/devices/{device_id}/addresses/{address_id}/delete")
async def remove_address(
    device_id: ItemId,
    address_id: ItemId,
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(require_user),
):
    """Stop treating an address as this device's. The next sweep that finds
    something there records it as a device of its own."""
    row = await session.get(DeviceAddress, address_id)
    if row is not None and row.device_id == device_id:
        await session.delete(row)
        await session.commit()
    return redirect(f"/devices/{device_id}#addresses")
