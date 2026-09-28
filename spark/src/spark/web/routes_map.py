"""The service map page: /map, and the placements SNMP suggests for it."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.ext.asyncio import AsyncSession

from .. import limits, servicemap, topology
from ..config import Config
from ..models import User
from .deps import ItemId, get_config, get_session, redirect, require_user, templates

router = APIRouter()


@router.get("/map")
async def service_map(
    request: Request,
    q: str = "",
    accepted: str = "",
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    # A search longer than anything it could match is not a search.
    query = q.strip()[:100]
    result = await servicemap.build(session, query=query)
    discovery, suggested = await topology.suggestions(session)
    return templates.TemplateResponse(
        request,
        "map.html",
        {"config": config, "user": user, "title": "Service map", "map": result, "q": query,
         "discovery": discovery, "suggested": suggested,
         "accepted": int(accepted) if accepted.isdigit() and len(accepted) < 6 else None},
    )


def _back(device_id: int, back: str) -> str:
    """The map, or this device's page: wherever the button was."""
    return back if back in {"/map#suggested", f"/devices/{device_id}#place"} else "/map#suggested"


async def _suggestion(session: AsyncSession, device_id: int) -> topology.Found | None:
    """The suggestion for this device as SPARK works it out now -- never a
    parent taken from the form, so a stale or edited page cannot place
    anything SNMP did not report."""
    _discovery, suggested = await topology.suggestions(session)
    return next((f for f in suggested if f.device.id == device_id), None)


@router.post("/map/suggestions/{device_id}/accept")
async def accept_suggestion(
    device_id: ItemId,
    back: str = Form("", max_length=limits.URL),
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(require_user),
):
    found = await _suggestion(session, device_id)
    if found is not None and await topology.accept(session, found):
        await session.commit()
    return redirect(_back(device_id, back))


@router.post("/map/suggestions/{device_id}/dismiss")
async def dismiss_suggestion(
    device_id: ItemId,
    back: str = Form("", max_length=limits.URL),
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(require_user),
):
    """"Not right": this placement is not suggested again."""
    found = await _suggestion(session, device_id)
    if found is not None:
        await topology.dismiss(session, found)
        await session.commit()
    return redirect(_back(device_id, back))


@router.post("/map/suggestions/accept-all")
async def accept_all(
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(require_user),
):
    """Every suggestion shown, top of the map first, so each parent is
    placed before what hangs off it."""
    _discovery, suggested = await topology.suggestions(session)
    accepted = 0
    for found in suggested:
        if await topology.accept(session, found):
            accepted += 1
    if accepted:
        await session.commit()
    return redirect(f"/map?accepted={accepted}#suggested")
