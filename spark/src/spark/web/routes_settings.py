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
from ..db import get_setting
from ..models import User
from .deps import get_config, get_session, redirect, require_user, templates

router = APIRouter()


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
