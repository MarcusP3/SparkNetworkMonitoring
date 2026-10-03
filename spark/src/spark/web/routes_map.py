"""The network map (/map) and its SNMP suggestions, and the Services page
(/services): every service the port scan found, searchable."""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.ext.asyncio import AsyncSession

from .. import limits, servicemap, topology
from ..config import Config
from ..models import User
from .deps import ItemId, get_config, get_session, redirect, require_user, templates

router = APIRouter()

# What the map's tiles count as network gear and as servers, by role.
GEAR = {"gateway", "switch", "access_point"}
SERVERS = {"host", "nas"}


@router.get("/map")
async def network_map(
    request: Request,
    q: str = "",
    accepted: str = "",
    wiped: str = "",
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    if q.strip():
        # The services search lived here before it had a page of its own;
        # an old bookmark or link still finds it.
        return redirect("/services?" + urlencode({"q": q.strip()[:100]}))
    result = await servicemap.build(session)
    discovery, suggested = await topology.suggestions(session)
    placed = list(_placed(result))
    roles = [str(getattr(n.device.role, "value", n.device.role)) for n in placed]
    # The tiles sit outside the live region (beside the Find box, which must
    # keep its text), so the page script recounts them from the tree after
    # each refresh; these are the numbers for the first paint, and for a
    # browser without scripts.
    tiles = {
        "placed": len(placed),
        "gear": sum(r in GEAR for r in roles),
        "servers": sum(r in SERVERS for r in roles),
        "unplaced": len(result.unplaced),
        "hints": len(suggested),
    }
    return templates.TemplateResponse(
        request,
        "map.html",
        {"config": config, "user": user, "title": "Network map", "map": result,
         "problems": sum(1 for n in placed if n.problem), "tiles": tiles,
         "discovery": discovery, "suggested": suggested,
         "mode": await topology.get_mode(session),
         "accepted": _count(accepted), "wiped": _count(wiped)},
    )


@router.get("/services")
async def services_page(
    request: Request,
    q: str = "",
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    # A search longer than anything it could match is not a search.
    query = q.strip()[:100]
    result = await servicemap.build(session, query=query)
    return templates.TemplateResponse(
        request,
        "services.html",
        {"config": config, "user": user, "title": "Services", "map": result, "q": query,
         "total": sum(len(n.services) for n in _nodes(result))},
    )


def _placed(result: servicemap.ServiceMap):  # type: ignore[no-untyped-def]
    """Every node on the map itself, not the unplaced list."""
    stack = list(result.roots)
    while stack:
        node = stack.pop()
        yield node
        stack.extend(node.children)


def _nodes(result: servicemap.ServiceMap):  # type: ignore[no-untyped-def]
    """Every node in the map, placed or not."""
    stack = list(result.roots) + list(result.unplaced)
    while stack:
        node = stack.pop()
        yield node
        stack.extend(node.children)


def _count(value: str) -> int | None:
    return int(value) if value.isdigit() and len(value) < 6 else None


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
    """"Not right": this placement is not suggested again -- and if automatic
    mode already made it, the device is taken back off."""
    found = await _suggestion(session, device_id)
    if found is not None:
        await topology.dismiss(session, found)
        await session.commit()
    else:
        seen = (await topology.discover(session)).found.get(device_id)
        if seen is not None and await topology.take_back(session, seen):
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
