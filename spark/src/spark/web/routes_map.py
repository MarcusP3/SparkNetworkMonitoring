"""The network map (/map) and its SNMP suggestions, and the Services page
(/services): every service the port scan found, searchable."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.ext.asyncio import AsyncSession

from .. import diagram, limits, servicemap, topology
from .. import subnets as subnet_service
from ..discovery.ports import concern
from ..discovery.runner import last_port_scan
from ..models import utcnow
from ..config import Config
from ..models import User
from .deps import ItemId, get_config, get_session, redirect, require_user, templates

router = APIRouter()

# What the map's tiles count as network gear and as servers, by role.
GEAR = {"gateway", "switch", "access_point"}
SERVERS = {"host", "nas", "hypervisor"}


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
         # The Diagram view: the same tree, laid out top-down here.
         "diagram": diagram.layout(result.roots), "trouble": diagram.trouble,
         "short": diagram.short, "box_chars": diagram.BOX_NAME_CHARS,
         "discovery": discovery, "suggested": suggested,
         "mode": await topology.get_mode(session),
         "accepted": _count(accepted), "wiped": _count(wiped)},
    )


# The Services page's chips: which rows to show, after the search and the
# subnet have narrowed them.
SHOW = {"watched": "Watched", "unwatched": "Not watched", "look": "Worth a look"}
COMMON_PORTS = 8


def _services_url(q: str, subnet: str, show: str) -> str:
    parts = [(k, v) for k, v in (("q", q), ("subnet", subnet), ("show", show)) if v]
    return "/services" + (f"?{urlencode(parts)}" if parts else "")


def _ago(iso: str | None) -> str | None:
    """"12 min" since an ISO time, or None."""
    try:
        seconds = (utcnow() - datetime.fromisoformat(iso)).total_seconds()  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if seconds < 60:
        return "now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h"
    return f"{int(seconds // 86400)} d"


@router.get("/services")
async def services_page(
    request: Request,
    q: str = "",
    subnet: str = "",
    show: str = "",
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    # A search longer than anything it could match is not a search.
    query = q.strip()[:100]
    everything = await servicemap.build(session)
    result = await servicemap.build(session, query=query) if query else everything
    for row in everything.services + (result.services if result is not everything else []):
        row["concern"] = concern(row["service"].port)

    # The tiles, the second-look list and the common ports describe every
    # service, whatever the table below is narrowed to.
    every = everything.services
    tiles = {
        "services": len(every),
        "devices": len({r["device"].id for r in every}),
        "watched": sum(1 for r in every if r["target"]),
        "look": sum(1 for r in every if r["concern"]),
        "ports": len({r["service"].port for r in every}),
        "scan": _ago((await last_port_scan(session)).get("finished_at")),
    }
    by_port: dict[int, Counter] = {}
    for r in every:
        by_port.setdefault(r["service"].port, Counter())[r["service"].name or ""] += 1
    common = sorted(by_port.items(), key=lambda kv: (-sum(kv[1].values()), kv[0]))[:COMMON_PORTS]
    common_ports = [{"port": port, "name": names.most_common(1)[0][0],
                     "devices": sum(names.values())} for port, names in common]

    # The subnet each row's device is on, by address, for the subnet filter.
    known_subnets = await subnet_service.list_subnets(session)
    rows = result.services
    for r in rows:
        r["subnet"] = subnet_service.subnet_for(known_subnets, r["device"].primary_ip)
    chosen = None
    if subnet == "none":
        chosen = "none"
        rows = [r for r in rows if r["subnet"] is None]
    elif subnet.isdigit():
        chosen = next((s for s in known_subnets if s.id == int(subnet)), None)
        if chosen is not None:
            rows = [r for r in rows if r["subnet"] is not None and r["subnet"].id == chosen.id]
    subnet = subnet if chosen is not None else ""

    counts = {
        "all": len(rows),
        "watched": sum(1 for r in rows if r["target"]),
        "unwatched": sum(1 for r in rows if not r["target"]),
        "look": sum(1 for r in rows if r["concern"]),
    }
    show = show if show in SHOW else ""
    if show == "watched":
        rows = [r for r in rows if r["target"]]
    elif show == "unwatched":
        rows = [r for r in rows if not r["target"]]
    elif show == "look":
        rows = [r for r in rows if r["concern"]]

    return templates.TemplateResponse(
        request,
        "services.html",
        {"config": config, "user": user, "title": "Services", "map": result, "q": query,
         "rows": rows, "total": len(every), "tiles": tiles, "counts": counts,
         "show": show, "subnet": subnet, "subnets": known_subnets,
         "chips": [("", "All", counts["all"])] + [(k, v, counts[k]) for k, v in SHOW.items()],
         "chip_url": lambda value: _services_url(query, subnet, value),
         "here": _services_url(query, subnet, show),
         "look": [r for r in every if r["concern"]],
         "common_ports": common_ports},
    )


def _placed(result: servicemap.ServiceMap):  # type: ignore[no-untyped-def]
    """Every node on the map itself, not the unplaced list."""
    stack = list(result.roots)
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
