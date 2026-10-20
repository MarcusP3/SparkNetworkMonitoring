"""The Alerts page: what is firing now, everything that fired before, and
the messages SPARK sent about it.

Activity lists outages and alert-rule incidents together (incident_rows.py),
the same rows the dashboard's Recent incidents shows ten of. Messages is the
outbox (models.Notification): every alert SPARK decided to send, and whether
Discord got it. The rules themselves are still under Rules and Suppressions
(the cards that were Settings -> Alerts and -> Suppressions) render in this
page's layout from routes_settings.py, where their forms still post.

Acknowledge (and Undo) post here from the dashboard and from Firing now. An
acknowledged incident still shows and still counts; it stops colouring the
counts, which are for what nobody has looked at yet.

Maintenance lists the windows (maintenance.py) and schedules one; a device's
page starts one in a click, posting here too. Channels sets up ntfy, email
and a webhook beside Discord, and what each one gets (channels.py).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import func, select, union
from sqlalchemy.ext.asyncio import AsyncSession

from .. import alerts as alert_service, channels, events, limits, maintenance
from ..config import Config
from ..db import get_setting, save_setting
from .. import snmp_alerts
from ..hierarchy import descendants
from ..models import (AlertIncident, AlertSuppression, Delivery, Device, Incident, MaintenanceWindow,
                      Notification, NotificationStatus, Target, User, utcnow)
from . import incident_rows
from ..vault import vault_for
from .deps import ItemId, get_config, get_session, redirect, require_user, templates

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
    unhandled = incident_rows.unhandled(firing)
    week = now - timedelta(days=7)
    sent = await session.scalar(select(func.count(Notification.id)).where(
        Notification.status == NotificationStatus.SENT, Notification.created_at >= week)) or 0
    failed = await session.scalar(select(func.count(Notification.id)).where(
        Notification.status == NotificationStatus.FAILED, Notification.created_at >= week)) or 0
    alerting = await get_setting(session, "alerting")
    webhook = bool(alerting.get("discord_webhook_sealed"))
    # Anywhere at all to send to: Discord, or one of the other channels.
    set_up = channels.set_up(alerting)
    rules = await snmp_alerts.load(session)
    switches = [v for v in rules.values() if isinstance(v, bool)]
    if not set_up:
        rules_hint = "no channel"
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
            "unhandled": unhandled,
            "handled": incident_rows.handled_note(firing),
            "day": await incident_rows.opened_since(session, now - timedelta(days=1)),
            "week": await incident_rows.opened_since(session, week),
            "sent": sent,
            "failed": failed,
            "webhook": webhook,
            "channels": [channels.LABELS[k] for k in set_up],
        },
        "menu": [
            {"slug": "activity", "label": "Activity", "url": "/alerts",
             "hint": (f"{len(firing)} firing" if unhandled
                      else incident_rows.handled_hint(firing) if firing else "quiet"),
             "warn": bool(unhandled), "about": "Firing now, and every one before"},
            {"slug": "messages", "label": "Messages", "url": "/alerts/messages",
             "hint": f"{failed} failed" if failed else ("no channel" if not set_up else ""),
             "warn": bool(failed) or not set_up, "about": "What was sent, and where"},
            {"slug": "rules", "label": "Rules", "url": "/alerts/rules",
             "hint": rules_hint, "warn": not set_up or not alerting.get("enabled", True),
             "about": "Discord, rules, muted"},
            {"slug": "suppressions", "label": "Suppressions", "url": "/alerts/suppressions",
             "hint": str(suppressed), "warn": False, "about": "Quiet one rule on one device"},
            {"slug": "maintenance", "label": "Maintenance", "url": "/alerts/maintenance",
             "hint": await _maintenance_hint(session, now), "warn": False,
             "about": "Hold alerts while you work on something"},
            {"slug": "channels", "label": "Channels", "url": "/alerts/channels",
             "hint": str(len(set_up)) if set_up else "none", "warn": not set_up,
             "about": "Discord, ntfy, email, a webhook"},
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
        rows.append({"row": n, "pill": pill, "words": words, "deliveries": []})
    # How each channel did, for alerts sent to more than one place (or to
    # one other than Discord). An alert from before channels has none.
    by_id = {r["row"].id: r for r in rows}
    for d in (await session.execute(
            select(Delivery).where(Delivery.notification_id.in_(by_id))
            .order_by(Delivery.id))).scalars():
        pill, words = STATUS_WORDS.get(d.status, ("neutral", str(d.status)))
        by_id[d.notification_id]["deliveries"].append(
            {"label": channels.LABELS.get(d.channel, d.channel), "pill": pill, "words": words,
             "error": d.error})

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


# --------------------------------------------------------------------------
# Acknowledging
# --------------------------------------------------------------------------

# Where the buttons are, and where each returns to without JavaScript. With
# it they are data-stay forms and the page stays put.
BACK = {"/": "/", "/alerts": "/alerts#firing"}


@router.post("/alerts/incidents/{kind}/{incident_id}/acknowledge")
async def acknowledge(
    kind: Literal["target", "alert"],
    incident_id: ItemId,
    note: str = Form("", max_length=limits.NOTE),
    back: str = Form("", max_length=limits.URL),
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(require_user),
):
    if await incident_rows.acknowledge(session, kind, incident_id, note):
        await session.commit()
        events.publish({"kind": "acknowledged"})
    return redirect(BACK.get(back, BACK["/alerts"]))


@router.post("/alerts/incidents/{kind}/{incident_id}/unacknowledge")
async def unacknowledge(
    kind: Literal["target", "alert"],
    incident_id: ItemId,
    back: str = Form("", max_length=limits.URL),
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(require_user),
):
    if await incident_rows.unacknowledge(session, kind, incident_id):
        await session.commit()
        events.publish({"kind": "acknowledged"})
    return redirect(BACK.get(back, BACK["/alerts"]))


# --------------------------------------------------------------------------
# Maintenance
# --------------------------------------------------------------------------


async def _maintenance_hint(session: AsyncSession, now: datetime) -> str:
    tz = await maintenance.zone(session)
    on = planned = 0
    for window in (await session.execute(select(MaintenanceWindow))).scalars():
        upcoming = maintenance.next_span(window, now, tz)
        if upcoming is None:
            continue
        if upcoming.holds(now):
            on += 1
        else:
            planned += 1
    return f"{on} on" if on else (f"{planned} planned" if planned else "")


# Shown for a while after they end, then only in the incidents they explain.
ENDED_SHOWN = timedelta(days=7)


async def maintenance_rows(session: AsyncSession, now: datetime,
                           device_id: int | None = None) -> list[dict]:
    """Every window worth listing, on now first, then the next to begin, then
    the recently ended. For one device, its own."""
    tz = await maintenance.zone(session)
    query = select(MaintenanceWindow, Device).join(Device, Device.id == MaintenanceWindow.device_id)
    if device_id is not None:
        query = query.where(MaintenanceWindow.device_id == device_id)
    parents = dict((await session.execute(select(Device.id, Device.parent_device_id))).all())
    rows = []
    for window, device in (await session.execute(query)).all():
        upcoming = maintenance.next_span(window, now, tz)
        if upcoming is None:
            if now - window.ends_at > ENDED_SHOWN:
                continue
            state, span = "ended", maintenance.Span(window.starts_at, window.ends_at)
        else:
            state, span = ("on" if upcoming.holds(now) else "planned"), upcoming
        rows.append({
            "window": window, "device": device, "state": state, "span": span,
            "below": len(descendants(parents, device.id)) if window.include_below else 0,
        })
    order = {"on": 0, "planned": 1, "ended": 2}
    rows.sort(key=lambda r: (order[r["state"]],
                             r["span"].start if r["state"] != "ended" else now - r["span"].end))
    return rows


def _local_input(when: datetime, tz) -> str:  # type: ignore[no-untyped-def]
    """A time as a datetime-local field wants it, on the chosen clock."""
    return when.astimezone(tz).strftime("%Y-%m-%dT%H:%M")


def _from_input(value: str, tz) -> datetime | None:  # type: ignore[no-untyped-def]
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(timezone.utc)


async def _maintenance_page(request: Request, session: AsyncSession, config: Config, user: User,
                            *, device: str = "", form: dict | None = None, error: str | None = None,
                            status_code: int = 200):
    now = utcnow()
    tz = request.state.tz
    devices = sorted((await session.execute(
        select(Device).where(Device.ignored.is_(False)))).scalars(),
        key=lambda d: d.display_name.lower())
    start = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    chosen = limits.as_id(device) if device else None
    form = form or {
        "device_id": str(chosen or ""), "starts": _local_input(start, tz),
        "ends": _local_input(start + timedelta(hours=1), tz), "weekly": False, "below": True,
        "note": "",
    }
    return templates.TemplateResponse(request, "alerts.html", {
        "config": config, "user": user, "title": "Alerts · Maintenance", "section": "maintenance",
        **await common(session),
        "windows": await maintenance_rows(session, now),
        "devices": devices,
        "form": form,
        "error": error,
    }, status_code=status_code)


@router.get("/alerts/maintenance")
async def maintenance_page(
    request: Request,
    device: str = "",
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    return await _maintenance_page(request, session, config, user, device=device)


def _maintenance_back(back: str, device_id: int | None) -> str:
    """The device page a button was on, or the Maintenance list."""
    if device_id is not None and back == f"/devices/{device_id}":
        return back
    return "/alerts/maintenance"


@router.post("/alerts/maintenance")
async def add_maintenance(
    request: Request,
    device_id: str = Form("", max_length=limits.SHORT),
    minutes: str = Form("", max_length=limits.SHORT),
    starts: str = Form("", max_length=limits.SHORT),
    ends: str = Form("", max_length=limits.SHORT),
    weekly: str = Form("", max_length=limits.SHORT),
    below: str = Form("", max_length=limits.SHORT),
    note: str = Form("", max_length=limits.NOTE),
    back: str = Form("", max_length=limits.URL),
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    """Start one now, for a few minutes or hours (a device's page), or
    schedule one with a start and an end (the Maintenance list)."""
    now = utcnow()
    tz = request.state.tz
    chosen = limits.as_id(device_id)
    form = {"device_id": device_id, "starts": starts, "ends": ends, "weekly": weekly == "1",
            "below": below == "1", "note": note}
    if minutes:
        length = int(minutes) if minutes.isdigit() else 0
        if length not in maintenance.DURATIONS:
            return redirect(_maintenance_back(back, chosen))
        starts_at, ends_at, repeat = now, now + timedelta(minutes=length), False
    else:
        starts_at, ends_at, repeat = _from_input(starts, tz), _from_input(ends, tz), weekly == "1"
    try:
        if chosen is None:
            raise maintenance.MaintenanceError("Choose a device.")
        if starts_at is None or ends_at is None:
            raise maintenance.MaintenanceError("Give a start and an end.")
        await maintenance.add(session, device_id=chosen, starts_at=starts_at, ends_at=ends_at,
                              weekly=repeat, include_below=below == "1", note=note, now=now)
    except maintenance.MaintenanceError as exc:
        # Nothing was written: add() checks everything before it adds.
        if minutes:
            return redirect(_maintenance_back(back, chosen))
        return await _maintenance_page(request, session, config, user, form=form, error=str(exc),
                                       status_code=400)
    await session.commit()
    events.publish({"kind": "maintenance"})
    return redirect(_maintenance_back(back, chosen))


@router.post("/alerts/maintenance/{window_id}/end")
async def end_maintenance(
    window_id: ItemId,
    back: str = Form("", max_length=limits.URL),
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(require_user),
):
    """End a one-off window early. What it held that is still broken is sent."""
    window = await session.get(MaintenanceWindow, window_id)
    device_id = window.device_id if window else None
    if window is not None and await maintenance.end_now(session, window):
        await session.commit()
        events.publish({"kind": "maintenance"})
    return redirect(_maintenance_back(back, device_id))


@router.post("/alerts/maintenance/{window_id}/delete")
async def delete_maintenance(
    window_id: ItemId,
    back: str = Form("", max_length=limits.URL),
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(require_user),
):
    """Take a window away: cancel one to come, or stop a weekly one. One
    that is on is ended first."""
    window = await session.get(MaintenanceWindow, window_id)
    device_id = window.device_id if window else None
    if window is not None:
        await maintenance.remove(session, window)
        await session.commit()
        events.publish({"kind": "maintenance"})
    return redirect(_maintenance_back(back, device_id))


# --------------------------------------------------------------------------
# Channels
# --------------------------------------------------------------------------


def channel_view(settings: dict) -> dict:
    """What the Channels tab shows of each channel: never a secret, only
    whether one is saved."""
    view = {}
    for key in channels.KINDS:
        conf = channels.saved(settings, key)
        raw = settings.get(key) or {}
        view[key] = {
            "set_up": bool(conf),
            "gets": conf.get("gets", sorted(channels.ALL)) if conf else sorted(channels.ALL),
            "conf": {k: v for k, v in conf.items() if not k.endswith("_sealed")},
            "has_token": bool(raw.get("token_sealed")),
            "has_password": bool(raw.get("password_sealed")),
        }
    return view


async def _channels_page(request: Request, session: AsyncSession, config: Config, user: User, *,
                         error: str | None = None, notice: str | None = None,
                         which: str | None = None, form: dict | None = None,
                         status_code: int = 200):
    settings = await alert_service.load(session)
    return templates.TemplateResponse(request, "alerts.html", {
        "config": config, "user": user, "title": "Alerts · Channels", "section": "channels",
        **await common(session),
        "ch": channel_view(settings),
        "groups": channels.GROUPS,
        "security_choices": channels.SECURITY,
        # A form that was refused, as it was typed, and which card it was.
        "which": which, "form": form or {},
        "error": error, "notice": notice,
    }, status_code=status_code)


@router.get("/alerts/channels")
async def channels_page(
    request: Request,
    saved: str = "",
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    notice = f"{channels.LABELS[saved]} saved." if saved in channels.LABELS else None
    return await _channels_page(request, session, config, user, which=saved or None,
                                notice=notice)


def _gets(problems: str, recoveries: str, news: str, security: str) -> list[str]:
    ticked = {g for g, on in zip(channels.GROUPS, (problems, recoveries, news, security)) if on}
    return channels.gets_from(list(ticked))


def _saved(key: str):  # type: ignore[no-untyped-def]
    return redirect(f"/alerts/channels?saved={key}#{key}")


@router.post("/alerts/channels/ntfy")
async def save_ntfy(
    request: Request,
    url: str = Form("", max_length=limits.HOOK_URL),
    token: str = Form("", max_length=limits.SECRET),
    gets_problems: str = Form("", max_length=limits.SHORT),
    gets_recoveries: str = Form("", max_length=limits.SHORT),
    gets_news: str = Form("", max_length=limits.SHORT),
    gets_security: str = Form("", max_length=limits.SHORT),
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    """A blank address or token keeps the saved one: they are never shown."""
    settings = await alert_service.load(session)
    try:
        channels.seal_ntfy(settings, vault_for(config), url=url, token=token,
                           gets=_gets(gets_problems, gets_recoveries, gets_news, gets_security))
    except channels.ChannelError as exc:
        return await _channels_page(request, session, config, user, error=str(exc), which="ntfy",
                                    status_code=400)
    await save_setting(session, alert_service.SETTING, settings)
    await session.commit()
    return _saved("ntfy")


@router.post("/alerts/channels/webhook")
async def save_webhook(
    request: Request,
    url: str = Form("", max_length=limits.HOOK_URL),
    token: str = Form("", max_length=limits.SECRET),
    gets_problems: str = Form("", max_length=limits.SHORT),
    gets_recoveries: str = Form("", max_length=limits.SHORT),
    gets_news: str = Form("", max_length=limits.SHORT),
    gets_security: str = Form("", max_length=limits.SHORT),
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    settings = await alert_service.load(session)
    try:
        channels.seal_hook(settings, vault_for(config), url=url, token=token,
                           gets=_gets(gets_problems, gets_recoveries, gets_news, gets_security))
    except channels.ChannelError as exc:
        return await _channels_page(request, session, config, user, error=str(exc),
                                    which="webhook", status_code=400)
    await save_setting(session, alert_service.SETTING, settings)
    await session.commit()
    return _saved("webhook")


@router.post("/alerts/channels/email")
async def save_email(
    request: Request,
    host: str = Form("", max_length=limits.ADDRESS),
    port: str = Form("", max_length=limits.SHORT),
    security: str = Form("", max_length=limits.SHORT),
    username: str = Form("", max_length=limits.ADDRESS),
    password: str = Form("", max_length=limits.PASSWORD),
    sender: str = Form("", max_length=limits.ADDRESS),
    to: str = Form("", max_length=limits.EMAILS),
    gets_problems: str = Form("", max_length=limits.SHORT),
    gets_recoveries: str = Form("", max_length=limits.SHORT),
    gets_news: str = Form("", max_length=limits.SHORT),
    gets_security: str = Form("", max_length=limits.SHORT),
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    """A blank password keeps the saved one."""
    settings = await alert_service.load(session)
    current = settings.get("email") or {}
    try:
        checked = channels.validate_email(
            host=host, port=port, security=security, username=username, sender=sender, to=to,
            password_given=bool(password),
            password_saved=bool(current.get("password_sealed")) and username == current.get("username"))
        channels.seal_email(settings, vault_for(config), checked, password=password,
                            gets=_gets(gets_problems, gets_recoveries, gets_news, gets_security))
    except channels.ChannelError as exc:
        form = {"host": host, "port": port, "security": security, "username": username,
                "sender": sender, "to": to}
        return await _channels_page(request, session, config, user, error=str(exc), which="email",
                                    form=form, status_code=400)
    await save_setting(session, alert_service.SETTING, settings)
    await session.commit()
    return _saved("email")


@router.post("/alerts/channels/discord")
async def save_discord_gets(
    gets_problems: str = Form("", max_length=limits.SHORT),
    gets_recoveries: str = Form("", max_length=limits.SHORT),
    gets_news: str = Form("", max_length=limits.SHORT),
    gets_security: str = Form("", max_length=limits.SHORT),
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(require_user),
):
    """What Discord gets. Its webhook is saved under Rules."""
    settings = await alert_service.load(session)
    channels.set_gets(settings, "discord",
                      _gets(gets_problems, gets_recoveries, gets_news, gets_security))
    await save_setting(session, alert_service.SETTING, settings)
    await session.commit()
    return _saved("discord")


@router.post("/alerts/channels/{key}/test")
async def test_channel(
    request: Request,
    key: Literal["discord", "ntfy", "email", "webhook"],
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    """Send a test now and say what the channel answered."""
    result = await alert_service.send_test(session, vault_for(config), key)
    await session.commit()
    label = channels.LABELS[key]
    if result.ok:
        return await _channels_page(request, session, config, user, which=key,
                                    notice=f"Test sent to {label}. Check that it arrived.")
    return await _channels_page(request, session, config, user, which=key,
                                error=f"The test to {label} did not arrive: {result.error}",
                                status_code=400)


@router.post("/alerts/channels/{key}/delete")
async def remove_channel(
    key: Literal["ntfy", "email", "webhook"],
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(require_user),
):
    """Take a channel away. Alerts waiting for it are dropped, saying so."""
    settings = await alert_service.load(session)
    channels.remove(settings, key)
    await save_setting(session, alert_service.SETTING, settings)
    await session.commit()
    return redirect(f"/alerts/channels#{key}")
