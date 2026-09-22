"""Settings, which for now means the subnets SPARK discovers on.

One page with sections rather than a page per setting: retention, discovery and
alerting all belong here eventually, and a nav that grows an entry per option is
how a homelab tool starts feeling like an enterprise console.

Errors come back on the page with the values still in the form. A validation
failure that clears what you typed is a worse outcome than the typo.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.ext.asyncio import AsyncSession

from .. import scheduler as scheduler_module
from .. import subnets as subnet_service
from ..config import Config
from ..db import get_setting, save_setting
from ..discovery.runner import PORT_SCAN_INTERVAL_CHOICES
from ..models import User
from .deps import get_config, get_session, redirect, require_user, templates

router = APIRouter()


def _port_count() -> int:
    from ..discovery.ports import WELL_KNOWN

    return len(WELL_KNOWN)


def _checked(value: str) -> bool:
    """An unticked checkbox is not submitted at all, so absence means false."""
    return value == "1"


async def _render(
    request: Request,
    session: AsyncSession,
    config: Config,
    user: User,
    *,
    error: str | None = None,
    form: dict | None = None,
    status_code: int = 200,
):
    rows = await subnet_service.list_subnets(session)
    discovery = await get_setting(session, "discovery")
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "config": config,
            "user": user,
            "title": "Settings",
            "subnets": [
                {
                    "subnet": subnet,
                    "oversized": subnet_service.too_large_to_sweep(subnet.cidr),
                    "unparseable": subnet.network is None,
                }
                for subnet in rows
            ],
            "error": error,
            "form": form or {},
            "max_vlan": subnet_service.MAX_VLAN,
            "scan": {
                "enabled": bool(discovery.get("port_scan_enabled", True)),
                "interval_hours": max(
                    1, int(discovery.get("port_scan_interval_seconds", 21600) or 21600) // 3600
                ),
                "choices": PORT_SCAN_INTERVAL_CHOICES,
                "port_count": _port_count(),
            },
        },
        status_code=status_code,
    )


async def _apply_to_scheduler(session: AsyncSession, config: Config) -> None:
    """Make the running sweep match what was just saved.

    Adding the first subnet has to start the sweep, and removing the last has
    to stop it, without a restart. Settings and the count are read here, in the
    request's own session, and handed to the scheduler rather than letting it
    open a second one while this request may still hold the write lock.
    """
    settings = await get_setting(session, "discovery")
    count = await subnet_service.count_enabled(session)
    await scheduler_module.schedule_discovery(config, settings, subnet_count=count)


@router.get("/settings")
async def settings_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    return await _render(request, session, config, user)


@router.post("/settings/subnets")
async def add_subnet(
    request: Request,
    cidr: str = Form(""),
    name: str = Form(""),
    vlan: str = Form(""),
    attached: str = Form(""),
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    try:
        await subnet_service.create(
            session, cidr=cidr, name=name, vlan=vlan, attached=_checked(attached)
        )
    except subnet_service.SubnetError as exc:
        return await _render(
            request, session, config, user,
            error=str(exc),
            form={"cidr": cidr, "name": name, "vlan": vlan, "attached": _checked(attached)},
            status_code=400,
        )

    await session.commit()
    await _apply_to_scheduler(session, config)
    return redirect("/settings")


@router.post("/settings/subnets/{subnet_id}")
async def edit_subnet(
    request: Request,
    subnet_id: int,
    cidr: str = Form(""),
    name: str = Form(""),
    vlan: str = Form(""),
    attached: str = Form(""),
    enabled: str = Form(""),
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    try:
        await subnet_service.update(
            session,
            subnet_id,
            cidr=cidr,
            name=name,
            vlan=vlan,
            attached=_checked(attached),
            enabled=_checked(enabled),
        )
    except subnet_service.SubnetError as exc:
        return await _render(request, session, config, user, error=str(exc), status_code=400)

    await session.commit()
    await _apply_to_scheduler(session, config)
    return redirect("/settings")


@router.post("/settings/subnets/{subnet_id}/delete")
async def remove_subnet(
    subnet_id: int,
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    _user: User = Depends(require_user),
):
    await subnet_service.delete(session, subnet_id)
    await session.commit()
    await _apply_to_scheduler(session, config)
    return redirect("/settings")


@router.post("/settings/port-scan")
async def set_port_scan(
    enabled: str = Form(""),
    interval_hours: str = Form(""),
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(require_user),
):
    """Turn the port scan on or off and set how often it runs.

    Validated against the offered list for the same reason the sweep interval
    is: a value that is not one of the choices did not come from this page, and
    keeping what is already stored is the safe reading of that.
    """
    settings = await get_setting(session, "discovery")
    settings["port_scan_enabled"] = _checked(enabled)
    try:
        hours = int(interval_hours)
    except (TypeError, ValueError):
        hours = 0
    if hours in PORT_SCAN_INTERVAL_CHOICES:
        settings["port_scan_interval_seconds"] = hours * 3600

    await save_setting(session, "discovery", settings)
    await session.commit()

    # Settings handed straight in: this request may still hold the write lock.
    await scheduler_module.schedule_port_scan(settings)
    return redirect("/settings")
