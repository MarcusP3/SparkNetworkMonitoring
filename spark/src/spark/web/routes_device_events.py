"""The Events page: SNMP traps and syslog from devices, and the rules that
alert on them (receiver.py).

/device-events, not /events: that is the live-update stream.
"""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import events as live, limits, receiver
from ..config import Config
from ..models import Device, DeviceEvent, EventRule, User
from .deps import ItemId, get_config, get_session, redirect, require_user, templates

router = APIRouter()

PER_PAGE = 50
MAX_PAGE = 200

# Rules to start from, one click each. Severity 7 is any.
PRESETS = (
    {"name": "A UPS went on battery", "source": "trap", "contains": "upsTrapOnBattery",
     "max_severity": 7, "quiet_minutes": 30},
    {"name": "A link went down", "source": "trap", "contains": "linkDown",
     "max_severity": 7, "quiet_minutes": 15},
    {"name": "Errors and worse", "source": "syslog", "contains": "",
     "max_severity": 3, "quiet_minutes": 30},
    {"name": "Failed sign-ins", "source": "syslog", "contains": "failed password",
     "max_severity": 7, "quiet_minutes": 30},
)


class RuleError(ValueError):
    pass


async def _check_rule(session: AsyncSession, *, name: str, source: str, device_id: str,
                      contains: str, max_severity: str, quiet_minutes: str) -> dict:
    if not name.strip():
        raise RuleError("Give the alert a name: it is what the message says.")
    if source not in ("any", *receiver.SOURCES):
        raise RuleError("Choose traps, syslog or either.")
    device = None
    if device_id.strip():
        device = limits.as_id(device_id)
        if device is None or await session.get(Device, device) is None:
            raise RuleError("Choose a device from the list, or any.")
    if not max_severity.isdigit() or not 0 <= int(max_severity) <= 7:
        raise RuleError("Choose a severity from the list.")
    if not quiet_minutes.isdigit() or not 1 <= int(quiet_minutes) <= 1440:
        raise RuleError("Quiet minutes are from 1 to 1440.")
    if not contains.strip() and source == "any" and int(max_severity) == 7 and device is None:
        raise RuleError("That would alert on everything: narrow it by text, severity or device.")
    return {"name": name.strip(), "source": source, "device_id": device,
            "contains": contains.strip() or None, "max_severity": int(max_severity),
            "quiet_minutes": int(quiet_minutes)}


async def _page(request: Request, session: AsyncSession, config: Config, user: User, *,
                device: str = "", source: str = "", severity: str = "", page: str = "",
                error: str | None = None, form: dict | None = None, status_code: int = 200):
    devices = {d.id: d for d in (await session.execute(select(Device))).scalars()}
    chosen = limits.as_id(device) if device else None
    chosen = chosen if chosen in devices else None
    source = source if source in receiver.SOURCES else ""
    worst = int(severity) if severity.isdigit() and int(severity) <= 7 else 7
    query = select(DeviceEvent)
    if chosen is not None:
        query = query.where(DeviceEvent.device_id == chosen)
    if source:
        query = query.where(DeviceEvent.source == source)
    if worst < 7:
        query = query.where(DeviceEvent.severity <= worst)
    total = await session.scalar(select(func.count()).select_from(query.subquery())) or 0
    pages = max(1, -(-total // PER_PAGE))
    number = min(int(page) if page.isdigit() and int(page) >= 1 else 1, MAX_PAGE, pages)
    rows = list((await session.execute(
        query.order_by(DeviceEvent.received_at.desc(), DeviceEvent.id.desc())
        .offset((number - 1) * PER_PAGE).limit(PER_PAGE))).scalars())
    rules = list((await session.execute(select(EventRule).order_by(EventRule.name))).scalars())

    def link(**over: object) -> str:
        params = {"device": chosen or "", "source": source, "severity": worst if worst < 7 else "",
                  "page": "", **over}
        kept = {k: v for k, v in params.items() if v not in ("", None, 1)}
        return "/device-events" + (f"?{urlencode(kept)}" if kept else "")

    status = receiver.current.status() if receiver.current else None
    strangers = sorted(((addr, n, at) for addr, (n, at) in status["counts"].strangers.items()),
                       key=lambda x: -x[1])[:10] if status else []
    return templates.TemplateResponse(request, "device_events.html", {
        "config": config, "user": user, "title": "Events",
        "events_config": config.events, "status": status, "strangers": strangers,
        "rows": rows, "devices": devices, "total": total, "rules": rules,
        "severities": receiver.SEVERITIES, "presets": PRESETS,
        "filters": {"device": chosen, "source": source, "severity": worst},
        "paging": {"page": number, "pages": pages,
                   "prev": link(page=number - 1) if number > 1 else None,
                   "next": link(page=number + 1) if number < pages else None},
        "error": error, "form": form or {},
        "device_list": sorted(devices.values(), key=lambda d: d.display_name.lower()),
    }, status_code=status_code)


@router.get("/device-events")
async def events_page(
    request: Request,
    device: str = "",
    source: str = "",
    severity: str = "",
    page: str = "",
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    return await _page(request, session, config, user, device=device, source=source,
                       severity=severity, page=page)


@router.post("/device-events/rules")
async def add_rule(
    request: Request,
    name: str = Form("", max_length=limits.NAME),
    source: str = Form("any", max_length=limits.SHORT),
    device_id: str = Form("", max_length=limits.SHORT),
    contains: str = Form("", max_length=128),
    max_severity: str = Form("7", max_length=limits.SHORT),
    quiet_minutes: str = Form("30", max_length=limits.SHORT),
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    try:
        fields = await _check_rule(session, name=name, source=source, device_id=device_id,
                                   contains=contains, max_severity=max_severity,
                                   quiet_minutes=quiet_minutes)
    except RuleError as exc:
        form = {"name": name, "source": source, "device_id": device_id, "contains": contains,
                "max_severity": max_severity, "quiet_minutes": quiet_minutes}
        return await _page(request, session, config, user, error=str(exc), form=form,
                           status_code=400)
    session.add(EventRule(**fields))
    await session.commit()
    return redirect("/device-events#rules")


@router.post("/device-events/rules/{rule_id}/toggle")
async def toggle_rule(
    rule_id: ItemId,
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(require_user),
):
    rule = await session.get(EventRule, rule_id)
    if rule is not None:
        rule.enabled = not rule.enabled
        await session.commit()
    return redirect("/device-events#rules")


@router.post("/device-events/rules/{rule_id}/delete")
async def delete_rule(
    rule_id: ItemId,
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(require_user),
):
    """Take a rule away. Its open incidents close at the next minute's
    housekeeping, as no longer watched."""
    rule = await session.get(EventRule, rule_id)
    if rule is not None:
        await session.delete(rule)
        await session.commit()
        live.publish({"kind": "device-events"})
    return redirect("/device-events#rules")
