"""The Alerts page: what is firing now, everything that fired before, and
the messages SPARK sent about it.

Activity lists outages and alert-rule incidents together (incident_rows.py),
the same rows the dashboard's Recent incidents shows ten of. Messages is the
outbox (models.Notification): every alert SPARK decided to send, and whether
Discord got it. The rules themselves are still under Rules and Suppressions
(the cards that were Settings -> Alerts and -> Suppressions) render in this
page's layout from routes_settings.py, where their forms still post.
"""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select, union
from sqlalchemy.ext.asyncio import AsyncSession

from .. import limits
from ..config import Config
from ..db import get_setting
from .. import snmp_alerts
from ..models import (AlertIncident, AlertSuppression, Device, Incident, Notification, NotificationStatus, Target,
                      User, utcnow)
from . import incident_rows
from .deps import get_config, get_session, require_user, templates

router = APIRouter()

PER_PAGE = 25
# Paging deeper than this is not a page anyone reads; it is a slow query.
MAX_PAGE = 400

STATUS_WORDS = {
    NotificationStatus.SENT: ("ok", "sent"),
    NotificationStatus.PENDING: ("neutral", "waiting"),
    NotificationStatus.HELD: ("neutral", "held, quiet hours"),
    NotificationStatus.FAILED: ("bad", "failed"),
    NotificationStatus.DROPPED: ("warn", "not sent"),
}
STATUS_FILTERS = {"failed": NotificationStatus.FAILED, "held": NotificationStatus.HELD,
                  "dropped": NotificationStatus.DROPPED, "sent": NotificationStatus.SENT}


def _page(value: str) -> int:
    return min(int(value), MAX_PAGE) if value.isdigit() and int(value) >= 1 else 1


def _url(path: str, **params: object) -> str:
    kept = {k: v for k, v in params.items() if v not in ("", None, 1)}
    return path + (f"?{urlencode(kept)}" if kept else "")


async def common(session: AsyncSession) -> dict:
    """The tiles and the menu, on every Alerts page."""
    now = utcnow()
    firing, left_out = await incident_rows.firing(session)
    week = now - timedelta(days=7)
    sent = await session.scalar(select(func.count(Notification.id)).where(
        Notification.status == NotificationStatus.SENT, Notification.created_at >= week)) or 0
    failed = await session.scalar(select(func.count(Notification.id)).where(
        Notification.status == NotificationStatus.FAILED, Notification.created_at >= week)) or 0
    alerting = await get_setting(session, "alerting")
    webhook = bool(alerting.get("discord_webhook_sealed"))
    rules = await snmp_alerts.load(session)
    switches = [v for v in rules.values() if isinstance(v, bool)]
    if not webhook:
        rules_hint = "no webhook"
    elif not alerting.get("enabled", True):
        rules_hint = "sending off"
    else:
        rules_hint = f"{sum(switches)} of {len(switches)} on"
    suppressed = await session.scalar(select(func.count(AlertSuppression.id))) or 0
    return {
        "firing": firing,
        "left_out": left_out,
        "tiles": {
            "firing": len(firing),
            "day": await incident_rows.opened_since(session, now - timedelta(days=1)),
            "week": await incident_rows.opened_since(session, week),
            "sent": sent,
            "failed": failed,
            "webhook": webhook,
        },
        "menu": [
            {"slug": "activity", "label": "Activity", "url": "/alerts",
             "hint": f"{len(firing)} firing" if firing else "quiet",
             "warn": bool(firing), "about": "Firing now, and every one before"},
            {"slug": "messages", "label": "Messages", "url": "/alerts/messages",
             "hint": f"{failed} failed" if failed else ("no webhook" if not webhook else ""),
             "warn": bool(failed) or not webhook, "about": "What went to Discord"},
            {"slug": "rules", "label": "Rules", "url": "/alerts/rules",
             "hint": rules_hint, "warn": not webhook or not alerting.get("enabled", True),
             "about": "Discord, rules, muted"},
            {"slug": "suppressions", "label": "Suppressions", "url": "/alerts/suppressions",
             "hint": str(suppressed), "warn": False, "about": "Quiet one rule on one device"},
        ],
    }


async def _devices_with_incidents(session: AsyncSession) -> list[Device]:
    ids = union(
        select(AlertIncident.device_id).where(AlertIncident.device_id.is_not(None)),
        select(Target.device_id).join(Incident, Incident.target_id == Target.id)
        .where(Target.device_id.is_not(None)),
    ).subquery()
    rows = (await session.execute(select(Device).where(Device.id.in_(select(ids))))).scalars()
    return sorted(rows, key=lambda d: d.display_name.lower())


@router.get("/alerts")
async def activity(
    request: Request,
    source: str = "",
    device: str = "",
    page: str = "",
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    source = source if source in incident_rows.SOURCE_CHOICES else ""
    devices = await _devices_with_incidents(session)
    chosen = limits.as_id(device) if device else None
    if chosen is not None and chosen not in {d.id for d in devices}:
        chosen = None
    asked = _page(page)
    found = await incident_rows.history(session, source=source, device_id=chosen,
                                        offset=(asked - 1) * PER_PAGE, limit=PER_PAGE)
    pages = max(1, -(-found.total // PER_PAGE))
    number = min(asked, pages)
    if number != asked:   # past the end: the last page instead
        found = await incident_rows.history(session, source=source, device_id=chosen,
                                            offset=(number - 1) * PER_PAGE, limit=PER_PAGE)

    def link(**over: object) -> str:
        params = {"source": source, "device": chosen or "", "page": 1, **over}
        return _url("/alerts", **params)

    return templates.TemplateResponse(request, "alerts.html", {
        "config": config, "user": user, "title": "Alerts", "section": "activity",
        **await common(session),
        "history": found.rows,
        "total": found.total,
        "filters": {
            "source": source,
            "sources": incident_rows.SOURCE_CHOICES,
            "device": chosen,
            "devices": devices,
            "any": bool(source or chosen),
            "source_urls": {s: link(source=s) for s in ("", *incident_rows.SOURCE_CHOICES)},
        },
        "paging": {
            "page": number, "pages": pages,
            "prev": link(page=number - 1) if number > 1 else None,
            "next": link(page=number + 1) if number < pages else None,
        },
    })


@router.get("/alerts/messages")
async def messages(
    request: Request,
    status: str = "",
    page: str = "",
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    status = status if status in STATUS_FILTERS else ""
    query = select(Notification)
    if status:
        query = query.where(Notification.status == STATUS_FILTERS[status])
    total = await session.scalar(select(func.count()).select_from(query.subquery())) or 0
    pages = max(1, -(-total // PER_PAGE))
    number = min(_page(page), pages)
    rows = []
    for n in (await session.execute(
            query.order_by(Notification.created_at.desc(), Notification.id.desc())
            .offset((number - 1) * PER_PAGE).limit(PER_PAGE))).scalars():
        pill, words = STATUS_WORDS.get(n.status, ("neutral", str(n.status)))
        rows.append({"row": n, "pill": pill, "words": words})

    def link(**over: object) -> str:
        return _url("/alerts/messages", **{"status": status, "page": 1, **over})

    return templates.TemplateResponse(request, "alerts.html", {
        "config": config, "user": user, "title": "Alerts · Messages", "section": "messages",
        **await common(session),
        "messages": rows,
        "total": total,
        "status": status,
        "status_urls": {s: link(status=s) for s in ("", *STATUS_FILTERS)},
        "paging": {
            "page": number, "pages": pages,
            "prev": link(page=number - 1) if number > 1 else None,
            "next": link(page=number + 1) if number < pages else None,
        },
    })
