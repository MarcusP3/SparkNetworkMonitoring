"""Dashboard shell.

Increment 1 renders the frame and an honest empty state: the schema exists, the
web app and auth work, and nothing is being monitored yet because the check
engine and discovery workers land in the next increments.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Config
from ..db import get_setting
from ..engine.state import human_duration
from ..models import CheckResult, Device, HealthStatus, Incident, Service, Target, User
from .deps import get_config, get_session, require_user, templates

router = APIRouter()


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
        "open_incidents": await count(Incident, Incident.closed_at.is_(None)),
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

    incident_rows = await session.execute(
        select(Incident, Target.name)
        .join(Target, Target.id == Incident.target_id)
        .order_by(Incident.opened_at.desc())
        .limit(10)
    )
    incidents = [
        {
            "target_name": name,
            "opened_at": incident.opened_at,
            "closed_at": incident.closed_at,
            "duration": human_duration(incident.duration_seconds),
            "cause": incident.cause,
            "suppressed_by_dependency": incident.suppressed_by_dependency,
        }
        for incident, name in incident_rows.all()
    ]

    alerting = await get_setting(session, "alerting")
    warnings: list[str] = []
    if not alerting.get("discord_webhook_url"):
        warnings.append(
            "No Discord webhook configured yet, so nothing can alert you."
        )
    if not config.network.subnets:
        warnings.append(
            "No subnets configured, so discovery has nothing to scan. "
            "Add them under network.subnets in spark.yaml."
        )
    routed = [s for s in config.network.subnets if not s.attached]
    if routed:
        names = ", ".join(s.label for s in routed)
        warnings.append(
            f"{names} are marked as routed rather than directly attached. "
            "ARP can't reach across a router, so devices there will be "
            "identified by IP instead of MAC until SNMP collection arrives."
        )

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "config": config,
            "title": "Dashboard",
            "user": user,
            "stats": stats,
            "warnings": warnings,
            "subnets": config.network.subnets,
            "watched": watched,
            "incidents": incidents,
        },
    )
