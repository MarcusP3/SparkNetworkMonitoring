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
from ..models import Device, HealthStatus, Incident, Service, Target, User
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
        },
    )
