"""Integrations: a page for each kind (integrations.py), listing every one of
that kind with the cards its device's page shows (integration_cards.py)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import credentials, integrations
from ..config import Config
from ..models import SnmpDevice, User
from . import integration_cards
from .deps import get_config, get_session, redirect, require_user, templates

router = APIRouter()

NO_STORAGE = {"pools": [], "drives": [], "filesystems": [], "read_at": None}


async def _snmp_storage(session: AsyncSession, device_id: int | None) -> dict:
    """A TrueNAS's pools' space and its filesystems, when SPARK polls its
    device over SNMP: the rest of its Storage card."""
    from .routes_device_page import _storage

    row = await session.scalar(select(SnmpDevice).where(SnmpDevice.device_id == device_id)) \
        if device_id else None
    return await _storage(session, row.id) if row else NO_STORAGE


@router.get("/integrations")
async def integrations_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    """Each kind there is at least one of, and the way to add one: what the
    sidebar's menu lists, for a phone's top bar, which has no room for it."""
    counts = await integrations.counts(session)
    states: dict[str, list[str]] = {}
    for entry in await credentials.listing(session):
        states.setdefault(entry["row"].kind, []).append(entry["state"])
    kinds = [{"slug": slug, "label": label, "about": integrations.ABOUT[slug],
              "count": counts[slug],
              "bad": sum(1 for s in states.get(slug, []) if s in ("bad", "waiting"))}
             for slug, label in integrations.KINDS.items() if counts[slug]]
    return templates.TemplateResponse(request, "integrations.html", {
        "config": config, "user": user, "title": "Integrations", "kind": None, "kinds": kinds,
    })


@router.get("/integrations/{kind}")
async def integration_page(
    request: Request,
    kind: str,
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    """Every one of a kind: a TrueNAS's API card and Storage card, a
    Proxmox's and a UniFi's API card and theirs, a Hermes's card."""
    if kind not in integrations.KINDS:
        return redirect("/integrations")
    apis: list[dict] = []
    hermes_cards: list[dict] = []
    if kind == "hermes":
        hermes_cards = await integration_cards.hermes_cards(session)
    else:
        apis = await integration_cards.api_cards(
            session, [e for e in await credentials.listing(session) if e["row"].kind == kind])
        for a in apis:
            a["storage"] = await _snmp_storage(session, a["row"].device_id) \
                if kind == "truenas" else NO_STORAGE
    return templates.TemplateResponse(request, "integrations.html", {
        "config": config, "user": user, "title": integrations.KINDS[kind],
        "kind": kind, "label": integrations.KINDS[kind], "about": integrations.ABOUT[kind],
        "apis": apis, "hermes_cards": hermes_cards,
        "count": len(apis) + len(hermes_cards),
        "back": request.url.path,
    })
