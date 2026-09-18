"""The device inventory: what discovery found, and what to do about it.

The "Watch" button is the point of this page. Discovery answers "what is on my
network"; watching turns one of those answers into something the check engine
polls. Without that step the inventory is trivia.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import events
from .. import scheduler as scheduler_module
from ..config import Config
from ..discovery.oui import is_locally_administered
from ..discovery.runner import last_sweep
from ..models import CheckType, Device, HealthStatus, Target, User, utcnow
from .deps import get_config, get_session, redirect, require_user, templates

router = APIRouter()


@router.get("/devices")
async def list_devices(
    request: Request,
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    devices = list(
        (
            await session.execute(
                select(Device)
                .where(Device.ignored.is_(False))
                .order_by(Device.last_seen.desc().nullslast())
            )
        )
        .scalars()
        .all()
    )
    ignored_count = await session.scalar(
        select(Device.id).where(Device.ignored.is_(True)).limit(1)
    )

    # Which devices already have something watching them, so the page can offer
    # "Watch" or say it is already watched rather than making duplicates.
    watched_ids = {
        row
        for row in (
            await session.execute(select(Target.device_id).where(Target.device_id.isnot(None)))
        )
        .scalars()
        .all()
    }

    now = utcnow()
    rows = []
    for device in devices:
        age = (now - device.last_seen).total_seconds() if device.last_seen else None
        rows.append(
            {
                "device": device,
                "age_seconds": age,
                "randomised": is_locally_administered(device.mac),
                "watched": device.id in watched_ids,
            }
        )

    sweep = await last_sweep(session)
    if sweep.get("finished_at"):
        try:
            finished = datetime.fromisoformat(sweep["finished_at"])
            sweep["age_seconds"] = (now - finished).total_seconds()
        except (ValueError, TypeError):
            sweep["age_seconds"] = None

    return templates.TemplateResponse(
        request,
        "devices.html",
        {
            "config": config,
            "user": user,
            "title": "Devices",
            "rows": rows,
            "has_ignored": ignored_count is not None,
            "subnets": config.network.subnets,
            "sweep": sweep,
        },
    )


@router.post("/devices/scan")
async def scan_now(
    config: Config = Depends(get_config),
    _user: User = Depends(require_user),
):
    scheduler_module.trigger_discovery_now(config)
    # Returns at once; the sweep publishes an event when it finishes and the
    # page updates itself.
    return redirect("/devices")


@router.post("/devices/{device_id}/name")
async def rename_device(
    device_id: int,
    friendly_name: str = Form(""),
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(require_user),
):
    device = await session.get(Device, device_id)
    if device is not None:
        device.friendly_name = friendly_name.strip() or None
        device.acknowledged = True
        await session.commit()
        events.publish({"kind": "device-renamed", "device_id": device_id})
    return redirect("/devices")


@router.post("/devices/{device_id}/ignore")
async def ignore_device(
    device_id: int,
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(require_user),
):
    device = await session.get(Device, device_id)
    if device is not None:
        device.ignored = not device.ignored
        await session.commit()
        events.publish({"kind": "device-ignored", "device_id": device_id})
    return redirect("/devices")


@router.post("/devices/{device_id}/watch")
async def watch_device(
    device_id: int,
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    _user: User = Depends(require_user),
):
    """Create a ping target for a discovered device.

    Addressed by IP rather than MAC because that is what you can ping -- but
    the target is linked to the device by id, so when DHCP moves the device the
    link survives even though the address in the target will need updating.
    That gap closes when discovery can rewrite a watched device's target
    address; for now it is honest to say the target follows the device row.
    """
    device = await session.get(Device, device_id)
    if device is None or not device.primary_ip:
        return redirect("/devices")

    existing = await session.scalar(select(Target).where(Target.device_id == device_id))
    if existing is not None:
        return redirect("/targets")

    target = Target(
        name=device.display_name,
        check_type=CheckType.PING,
        address=device.primary_ip,
        device_id=device.id,
        status=HealthStatus.UNKNOWN,
    )
    session.add(target)
    device.acknowledged = True
    await session.flush()
    await session.commit()

    events.publish({"kind": "created", "target_id": target.id})
    scheduler_module.schedule_target(target)
    await scheduler_module.run_now(target.id)
    return redirect("/targets")
