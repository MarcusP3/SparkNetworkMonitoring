"""Integrations: the systems SPARK reads through their own APIs, a page each.

TrueNAS, Proxmox and UniFi are API credentials (Settings -> Credentials,
credentials.py). Hermes is a Hermes check, or a dashboard the port scan
found that no check reads yet (hermes_scan.py): nothing to set up in
Settings. The menu under Integrations lists each kind with at least one,
and /integrations/<kind> shows every one of that kind, with the same cards
its device's page shows.

A new kind is an entry in KINDS, a count in menu(), and its cards on the
page (web/routes_integrations.py).
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import hermes_scan
from .models import ApiCredential, CheckType, Target

# slug -> name, in the order the menu lists them.
KINDS = {"hermes": "Hermes", "proxmox": "Proxmox", "truenas": "TrueNAS", "unifi": "UniFi"}

# What each page says it is, under its name.
ABOUT = {
    "hermes": "Hermes Agent: its gateway, messaging platforms, memory and disk.",
    "proxmox": "Proxmox VE: its nodes, VMs and containers, storage and drives.",
    "truenas": "TrueNAS: its pools, drives and TrueNAS's own alerts.",
    "unifi": "UniFi Network: the devices it manages and their clients.",
}


async def hermes_found(session: AsyncSession) -> list[dict]:
    """The found dashboards that have a card: all but a port that answered
    as something else (hermes_scan.cards)."""
    return hermes_scan.cards([s for s, _ in await hermes_scan.found(session)])


async def counts(session: AsyncSession) -> dict[str, int]:
    """How many of each kind there are."""
    out = dict((await session.execute(
        select(ApiCredential.kind, func.count(ApiCredential.id)).group_by(ApiCredential.kind))).all())
    checks = await session.scalar(
        select(func.count(Target.id)).where(Target.check_type == CheckType.HERMES)) or 0
    out["hermes"] = checks + len(await hermes_found(session))
    return {slug: out.get(slug, 0) for slug in KINDS}


async def menu(session: AsyncSession) -> list[dict]:
    """The kinds there is at least one of, for the menu: slug, name, count."""
    return [{"slug": slug, "label": KINDS[slug], "count": n}
            for slug, n in (await counts(session)).items() if n]
