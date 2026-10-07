"""Dashboard shell.

Increment 1 renders the frame and an honest empty state: the schema exists, the
web app and auth work, and nothing is being monitored yet because the check
engine and discovery workers land in the next increments.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import subnets as subnet_service
from .. import suppressions
from . import incident_rows
from .. import internet
from ..config import Config
from ..db import get_setting
from ..models import (AlertIncident, CheckResult, Device, HealthStatus, Incident, Service, Target,
                      User)
from .deps import get_config, get_session, redirect, require_user, templates

router = APIRouter()

RECENT = 10

# Kept here too: older tests and code import them from the dashboard.
SOURCES, source_of = incident_rows.SOURCES, incident_rows.source_of


def _greeting() -> str:
    """Morning, afternoon or evening, by the server's clock.

    The server's, because that is the only clock this page has -- rendering
    happens before any browser is involved. On a homelab box sitting in the
    same house as the person reading it, that is the right answer; on a VPS in
    another timezone it will be wrong, and a greeting is a cheap enough thing
    to be wrong about that it is not worth a round trip to find out.
    """
    hour = datetime.now().astimezone().hour
    if hour < 12:
        return "Good morning"
    if hour < 18:
        return "Good afternoon"
    return "Good evening"


async def _open_alerts(session: AsyncSession) -> int:
    hidden = await suppressions.hidden_on_dashboard(session)
    total = 0
    for row in (await session.execute(
        select(AlertIncident).where(AlertIncident.closed_at.is_(None))
    )).scalars():
        rule = suppressions.rule_of(row.key)
        klass = (await suppressions.klass_of(session, row.key, row.detail)
                 if rule == "truenas_alerts" else None)
        total += not hidden(row, rule, klass)
    return total


@router.get("/healthz")
async def healthz():
    """Liveness probe. Deliberately unauthenticated and free of any detail."""
    return {"status": "ok"}


@router.get("/")
async def dashboard(
    request: Request,
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    async def count(model, *where) -> int:
        stmt = select(func.count()).select_from(model)
        for clause in where:
            stmt = stmt.where(clause)
        return await session.scalar(stmt) or 0

    stats = {
        "devices": await count(Device, Device.ignored.is_(False)),
        "services": await count(Service, Service.ignored.is_(False)),
        "targets": await count(Target, Target.enabled.is_(True)),
        "down": await count(Target, Target.status == HealthStatus.DOWN),
        "degraded": await count(Target, Target.status == HealthStatus.DEGRADED),
        # Outages and every other kind of alert standing now, less what is
        # suppressed (the list below leaves those out too).
        "open_incidents": await count(Incident, Incident.closed_at.is_(None))
        + await _open_alerts(session),
    }

    targets = list(
        (
            await session.execute(
                select(Target).where(Target.enabled.is_(True)).order_by(Target.name)
            )
        )
        .scalars()
        .all()
    )

    # One row per target with its most recent result, rather than a query per
    # target inside the template.
    latest: dict[int, tuple[float | None, str | None]] = {}
    if targets:
        newest = (
            select(
                CheckResult.target_id,
                func.max(CheckResult.ts).label("ts"),
            )
            .where(CheckResult.target_id.in_([t.id for t in targets]))
            .group_by(CheckResult.target_id)
            .subquery()
        )
        rows = await session.execute(
            select(CheckResult.target_id, CheckResult.latency_ms, CheckResult.detail).join(
                newest,
                (CheckResult.target_id == newest.c.target_id) & (CheckResult.ts == newest.c.ts),
            )
        )
        for target_id, latency_ms, detail in rows.all():
            latest[target_id] = (latency_ms, detail)

    watched = [
        {
            "id": t.id,
            "name": t.name,
            "address": t.address,
            "status": getattr(t.status, "value", t.status),
            "latency_ms": latest.get(t.id, (None, None))[0],
            "detail": latest.get(t.id, (None, None))[1],
        }
        for t in targets
    ]

    recent_outages = await session.execute(
        select(Incident, Target.name)
        .join(Target, Target.id == Incident.target_id)
        .order_by(Incident.opened_at.desc())
        .limit(RECENT)
    )
    incidents = [incident_rows.target_row(incident, name)
                 for incident, name in recent_outages.all()]
    # SNMP, storage and API alerts beside the outages, the newest ten of both.
    # Not what is suppressed: an alert closed by its suppression, or any from
    # a rule that is now off for its device -- that is expected, not news.
    hidden = await suppressions.hidden_on_dashboard(session)
    shown = 0
    for row in (await session.execute(
        select(AlertIncident).order_by(AlertIncident.opened_at.desc()).limit(RECENT * 10)
    )).scalars():
        if shown >= RECENT:
            break
        rule, klass = await incident_rows.rule_of(session, row)
        if hidden(row, rule, klass):
            continue
        shown += 1
        incidents.append(incident_rows.alert_row(row, rule, klass))
    incidents.sort(key=lambda i: i["opened_at"], reverse=True)
    incidents = incidents[:RECENT]

    alerting = await get_setting(session, "alerting")
    warnings: list[str] = []
    if not alerting.get("discord_webhook_sealed"):
        warnings.append(
            "No Discord webhook configured yet, so nothing can alert you. "
            "Add one under Settings → Alerts."
        )
    elif not alerting.get("enabled", True):
        warnings.append("Alerts are switched off in Settings, so nothing will reach Discord.")
    # From the database, not the config: spark.yaml seeds subnets once and is
    # never read again, so reading it here showed the file's idea of the
    # network rather than the one the Settings page edits.
    known_subnets = await subnet_service.list_subnets(session)
    # How many of the devices SPARK knows sit on each subnet, for the network
    # overview. By address, not the stored label (Subnet.contains says why).
    device_counts: Counter[int] = Counter()
    if known_subnets:
        for address in (await session.execute(
            select(Device.primary_ip).where(Device.ignored.is_(False))
        )).scalars():
            home = subnet_service.subnet_for(known_subnets, address)
            if home is not None:
                device_counts[home.id] += 1

    if not known_subnets:
        warnings.append(
            "No subnets configured, so discovery has nothing to scan. "
            "Add one under Settings."
        )
    asleep = [s for s in known_subnets if not s.enabled]
    if asleep:
        names = ", ".join(s.label for s in asleep)
        warnings.append(
            f"{names} are listed but not swept, so nothing there is being "
            "discovered. That is a setting, not a fault — untick Sweep only "
            "for segments you want on record without scanning."
        )
    # No banner for routed subnets: routed is a setting, not a fault, and the
    # network overview below already marks each one "Routed · IP identity only".

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "config": config,
            "title": "Dashboard",
            "user": user,
            "stats": stats,
            "warnings": warnings,
            "subnets": known_subnets,
            "device_counts": device_counts,
            "watched": watched,
            "incidents": incidents,
            "internet": await internet.view(session),
            "greeting": _greeting(),
            # Local time, not UTC. Everything stored is UTC on purpose, but a
            # header that greets you by time of day and then prints a clock
            # five hours off is worse than printing no clock at all.
            "now": datetime.now().astimezone(),
        },
    )


@router.post("/internet/enabled")
async def internet_enabled(
    on: str = Form("", max_length=8),
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(require_user),
):
    """Switch the Internet card's checks on or off (internet.py)."""
    await internet.set_enabled(session, on == "1")
    await session.commit()
    return redirect("/#internet")
