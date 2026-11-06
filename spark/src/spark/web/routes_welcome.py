"""/welcome: the first-run wizard (onboarding.py), a page per step.

Subnets, SNMP, integrations, alerts, then Finish, which starts the first
scan. Each step saves with the same functions Settings uses, so what the
wizard makes is indistinguishable from what Settings would have made, and
each can be skipped. Errors come back on the page with what was typed,
never the secrets.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import alerts as alert_service
from .. import credentials, exclusions, limits, onboarding, snmp_config
from .. import scheduler as scheduler_module
from .. import subnets as subnet_service
from ..collectors.snmp import AUTH_PROTOCOLS, PRIV_PROTOCOLS
from ..config import Config
from ..db import get_setting
from ..models import ApiCredential, User
from ..vault import vault_for
from .deps import ItemId, get_config, get_session, redirect, require_user, templates

router = APIRouter()

BLANK_ROWS = 3        # for routed VLANs the host has no interface on
MAX_ROWS = 32
KIND_ORDER = ("truenas", "proxmox", "unifi")


def _rows(values: list[str], count: int) -> list[str]:
    return [(values[i] if i < len(values) else "").strip() for i in range(count)]


async def _context(session: AsyncSession, config: Config, step: str) -> dict:
    """What every step's page needs, plus what this one shows."""
    state = await onboarding.load(session)
    ctx: dict = {
        "config": config, "title": "Set up SPARK", "step": step,
        "steps": [(s, onboarding.TITLES[s], (state.get("steps") or {}).get(s))
                  for s in onboarding.STEPS],
        "state": state,
    }
    if step in ("subnets", "finish"):
        existing = await subnet_service.list_subnets(session)
        ctx["existing"] = existing
        ctx["excluded"] = await exclusions.entries(session)
        if step == "subnets":
            known = {s.cidr for s in existing}
            ctx["detected"] = [s for s in onboarding.host_subnets() if s.cidr not in known]
            ctx["blank_rows"] = BLANK_ROWS
    if step in ("snmp", "finish"):
        ctx["profiles"] = [(p, snmp_config.security_level(p))
                           for p in await snmp_config.list_profiles(session)]
        ctx["auth_protocols"], ctx["priv_protocols"] = AUTH_PROTOCOLS, PRIV_PROTOCOLS
    if step in ("integrations", "finish"):
        rows = (await session.execute(select(ApiCredential).order_by(ApiCredential.id))).scalars().all()
        by_kind: dict[str, list] = {k: [] for k in KIND_ORDER}
        for row in rows:
            key = credentials.state(row)
            pill, words = credentials.STATES[key]
            by_kind.setdefault(row.kind, []).append(
                {"row": row, "state": key, "pill": pill, "words": words})
        ctx["kinds"] = [(k, credentials.KINDS[k], by_kind.get(k, [])) for k in KIND_ORDER]
    if step in ("alerts", "finish"):
        ctx["has_webhook"] = bool((await alert_service.load(session)).get("discord_webhook_sealed"))
    return ctx


async def _undo(session: AsyncSession, user: User) -> None:
    """Throw away a half-saved step. A rollback expires every object in the
    session, the signed-in user included, and the page renders from it --
    so it is read again rather than left to fail a lazy load."""
    await session.rollback()
    await session.refresh(user)


def _page(request: Request, user: User, ctx: dict, status_code: int = 200, **extra):  # type: ignore[no-untyped-def]
    return templates.TemplateResponse(request, "welcome.html", {**ctx, "user": user, **extra},
                                      status_code=status_code)


async def _advance(session: AsyncSession, step: str, how: str):  # type: ignore[no-untyped-def]
    state = await onboarding.mark(session, step, how)
    await session.commit()
    return redirect(f"/welcome/{onboarding.next_step(state, after=step)}")


@router.get("/welcome")
async def welcome(
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(require_user),
):
    return redirect(f"/welcome/{onboarding.next_step(await onboarding.load(session))}")


@router.get("/welcome/{step}")
async def welcome_step(
    request: Request,
    step: str,
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    if step not in onboarding.STEPS:
        return redirect("/welcome")
    return _page(request, user, await _context(session, config, step))


# --------------------------------------------------------------------------
# Subnets
# --------------------------------------------------------------------------


@router.post("/welcome/subnets")
async def save_subnets(
    request: Request,
    action: str = Form("next", max_length=limits.SHORT),
    cidr: list[str] = Form([], max_length=MAX_ROWS),
    name: list[str] = Form([], max_length=MAX_ROWS),
    vlan: list[str] = Form([], max_length=MAX_ROWS),
    include: list[str] = Form([], max_length=MAX_ROWS),
    attached: list[str] = Form([], max_length=MAX_ROWS),
    exclude: str = Form("", max_length=limits.NOTE * 8),
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    if action == "skip":
        return await _advance(session, "subnets", "skipped")
    count = min(len(cidr), MAX_ROWS)
    cidrs, names, vlans = _rows(cidr, count), _rows(name, count), _rows(vlan, count)
    on, wired = set(include), set(attached)
    typed = [{"cidr": cidrs[i], "name": names[i], "vlan": vlans[i],
              "include": str(i) in on, "attached": str(i) in wired} for i in range(count)]
    error = None
    if any(len(v) > limits.NAME for row in typed for v in (row["cidr"], row["name"])):
        error = f"Keep each network and name under {limits.NAME} characters."
    specs = [line.strip() for line in exclude.splitlines() if line.strip()]
    added = 0
    if error is None:
        try:
            for row in typed:
                if row["include"] and row["cidr"]:
                    await subnet_service.create(session, cidr=row["cidr"], name=row["name"],
                                                vlan=row["vlan"], attached=row["attached"])
                    added += 1
            for spec in specs:
                await exclusions.add(session, spec[: limits.NAME + 36], "Added during setup")
        except (subnet_service.SubnetError, exclusions.ExclusionError) as exc:
            error = str(exc)
    if error is not None:
        await _undo(session, user)
        ctx = await _context(session, config, "subnets")
        return _page(request, user, ctx, status_code=400, error=error, typed=typed, exclude=exclude)
    if not added and not await subnet_service.list_subnets(session) and action != "skip":
        ctx = await _context(session, config, "subnets")
        return _page(request, user, ctx, status_code=400, typed=typed, exclude=exclude,
                     error="Tick at least one network, or press Skip for now.")
    await session.flush()
    settings = await get_setting(session, "discovery")
    await scheduler_module.schedule_discovery(
        config, settings, subnet_count=await subnet_service.count_enabled(session))
    return await _advance(session, "subnets", "done")


@router.post("/welcome/subnets/{subnet_id}/delete")
async def remove_subnet(
    subnet_id: ItemId,
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    _user: User = Depends(require_user),
):
    """Take off a subnet already listed -- most often the example one
    spark.yaml seeded before anyone edited it."""
    await subnet_service.delete(session, subnet_id)
    await session.flush()
    settings = await get_setting(session, "discovery")
    await scheduler_module.schedule_discovery(
        config, settings, subnet_count=await subnet_service.count_enabled(session))
    await session.commit()
    return redirect("/welcome/subnets")


# --------------------------------------------------------------------------
# SNMP
# --------------------------------------------------------------------------


@router.post("/welcome/snmp")
async def save_snmp(
    request: Request,
    action: str = Form("next", max_length=limits.SHORT),
    version: str = Form("", max_length=limits.SHORT),
    community: str = Form("", max_length=limits.SECRET),
    username: str = Form("", max_length=limits.NAME),
    auth_protocol: str = Form("SHA", max_length=limits.SHORT),
    auth_key: str = Form("", max_length=limits.SECRET),
    priv_protocol: str = Form("AES", max_length=limits.SHORT),
    priv_key: str = Form("", max_length=limits.SECRET),
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    if action == "skip":
        return await _advance(session, "snmp", "skipped")
    if action == "continue":
        return await _advance(session, "snmp", "done")
    version = version if version in ("v2c", "v3") else ""
    if not version:
        if await snmp_config.list_profiles(session):
            return await _advance(session, "snmp", "done")
        return await _advance(session, "snmp", "skipped")
    taken = {p.name.lower() for p in await snmp_config.list_profiles(session)}
    base = "SNMP v3" if version == "v3" else "SNMP v2c"
    profile_name, n = base, 2
    while profile_name.lower() in taken:
        profile_name, n = f"{base} {n}", n + 1
    data = snmp_config.ProfileInput(
        name=profile_name, version=version, community=community, username=username,
        auth_protocol=auth_protocol, auth_key=auth_key, priv_protocol=priv_protocol,
        priv_key=priv_key)
    try:
        await snmp_config.save_profile(session, vault_for(config), data)
    except snmp_config.ProfileError as exc:
        ctx = await _context(session, config, "snmp")
        return _page(request, user, ctx, status_code=400, error=str(exc),
                     typed={"version": version, "username": username,
                            "auth_protocol": auth_protocol, "priv_protocol": priv_protocol})
    return await _advance(session, "snmp", "done")


# --------------------------------------------------------------------------
# Integrations
# --------------------------------------------------------------------------


async def _free_name(session: AsyncSession, label: str) -> str:
    taken = {n.lower() for n in (await session.execute(select(ApiCredential.name))).scalars()}
    name, n = label, 2
    while name.lower() in taken:
        name, n = f"{label} {n}", n + 1
    return name


@router.post("/welcome/integrations")
async def save_integrations(
    request: Request,
    action: str = Form("connect", max_length=limits.SHORT),
    use: list[str] = Form([], max_length=len(KIND_ORDER)),
    truenas_host: str = Form("", max_length=limits.ADDRESS),
    truenas_key: str = Form("", max_length=limits.SECRET),
    proxmox_host: str = Form("", max_length=limits.ADDRESS),
    proxmox_token_id: str = Form("", max_length=limits.NAME * 2),
    proxmox_secret: str = Form("", max_length=limits.SECRET),
    unifi_host: str = Form("", max_length=limits.ADDRESS),
    unifi_key: str = Form("", max_length=limits.SECRET),
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    if action == "skip":
        return await _advance(session, "integrations", "skipped")
    if action == "next":
        return await _advance(session, "integrations", "done")
    vault = vault_for(config)
    chosen = [k for k in KIND_ORDER if k in use]
    existing = set((await session.execute(select(ApiCredential.kind))).scalars())
    typed = {"use": chosen, "truenas_host": truenas_host, "proxmox_host": proxmox_host,
             "proxmox_token_id": proxmox_token_id, "unifi_host": unifi_host}
    given = {
        "truenas": (truenas_host, truenas_key, ""),
        "proxmox": (proxmox_host, proxmox_secret, proxmox_token_id),
        "unifi": (unifi_host, unifi_key, ""),
    }
    made: list[ApiCredential] = []
    try:
        for kind in chosen:
            host, key, token_id = (v.strip() for v in given[kind])
            if kind in existing and not (host or key or token_id):
                continue                       # already added; nothing new typed
            if not (host or key or token_id):
                continue                       # chosen for later: a banner keeps it
            if not host:
                raise credentials.CredentialError(
                    f"{credentials.KINDS[kind]}: enter its address -- SPARK has found no devices yet.")
            row = await credentials.add(
                session, vault, kind=kind, name=await _free_name(session, credentials.KINDS[kind]),
                device_id=None, host=host, api_key=key, token_id=token_id)
            row.options = {**(row.options or {}), onboarding.LINK_FLAG: True}
            made.append(row)
    except credentials.CredentialError as exc:
        await _undo(session, user)
        ctx = await _context(session, config, "integrations")
        return _page(request, user, ctx, status_code=400, error=str(exc), typed=typed)
    await onboarding.set_chosen(session, chosen)
    # Each straight to a Test, as Settings does: it fetches the certificate
    # (sending nothing) so the page can ask for it to be trusted.
    for row in made:
        await credentials.test(session, vault, row)
    await session.commit()
    return redirect("/welcome/integrations")


@router.post("/welcome/integrations/{cred_id}/trust")
async def trust(
    request: Request,
    cred_id: ItemId,
    fingerprint: str = Form("", max_length=limits.NAME * 2),
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    row = await session.get(ApiCredential, cred_id)
    if row is None:
        return redirect("/welcome/integrations")
    try:
        await credentials.trust(session, vault_for(config), row, fingerprint)
    except credentials.CredentialError as exc:
        ctx = await _context(session, config, "integrations")
        return _page(request, user, ctx, status_code=400, error=str(exc))
    await session.commit()
    return redirect(f"/welcome/integrations#cred-{cred_id}")


@router.post("/welcome/integrations/{cred_id}/test")
async def test_again(
    cred_id: ItemId,
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    _user: User = Depends(require_user),
):
    row = await session.get(ApiCredential, cred_id)
    if row is not None:
        await credentials.test(session, vault_for(config), row)
        await session.commit()
    return redirect(f"/welcome/integrations#cred-{cred_id}")


@router.post("/welcome/integrations/{cred_id}/delete")
async def remove_credential(
    cred_id: ItemId,
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(require_user),
):
    row = await session.get(ApiCredential, cred_id)
    if row is not None:
        await credentials.forget_alert(session, row.id)
        await session.delete(row)
        await session.commit()
    return redirect("/welcome/integrations")


# --------------------------------------------------------------------------
# Alerts
# --------------------------------------------------------------------------


@router.post("/welcome/alerts")
async def save_alerts(
    request: Request,
    action: str = Form("next", max_length=limits.SHORT),
    webhook: str = Form("", max_length=limits.WEBHOOK),
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    if action == "skip":
        return await _advance(session, "alerts", "skipped")
    vault = vault_for(config)
    if webhook.strip():
        try:
            await alert_service.set_webhook(session, vault, webhook.strip())
        except alert_service.AlertError as exc:
            ctx = await _context(session, config, "alerts")
            return _page(request, user, ctx, status_code=400, error=str(exc))
    if action == "test":
        await session.flush()
        result = await alert_service.send_test(session, vault)
        await session.commit()
        ctx = await _context(session, config, "alerts")
        if result.ok:
            return _page(request, user, ctx, notice="Test alert sent. Check the channel.")
        return _page(request, user, ctx, status_code=400,
                     error=f"The test did not arrive: {result.error}")
    has = bool((await alert_service.load(session)).get("discord_webhook_sealed"))
    return await _advance(session, "alerts", "done" if has else "skipped")


# --------------------------------------------------------------------------
# Finish
# --------------------------------------------------------------------------


@router.post("/welcome/finish")
async def finish(
    find_snmp: str = Form("", max_length=limits.SHORT),
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    _user: User = Depends(require_user),
):
    """Done. With subnets, the first scan starts now; Find SNMP, if asked
    for, runs once that scan has found something to ask (onboarding.after_sweep)."""
    has_subnets = await subnet_service.count_enabled(session) > 0
    # Without a subnet there is no scan, and nothing for Find SNMP to ask.
    wants_snmp = (has_subnets and find_snmp == "1"
                  and bool(await snmp_config.list_profiles(session)))
    await onboarding.finish(session, find_snmp=wants_snmp)
    await session.commit()
    if has_subnets:
        scheduler_module.trigger_discovery_now(config)
        return redirect("/devices")
    return redirect("/")


# --------------------------------------------------------------------------
# Banners (onboarding.banners), on every page
# --------------------------------------------------------------------------


@router.post("/banners/dismiss")
async def dismiss_banner(
    key: str = Form("", max_length=limits.NAME),
    sig: str = Form("", max_length=limits.NAME),
    next: str = Form("/", max_length=limits.URL),
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(require_user),
):
    from .routes_auth import _safe_next

    if await onboarding.dismiss(session, key, sig):
        await session.commit()
    return redirect(_safe_next(next))
