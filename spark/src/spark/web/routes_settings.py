"""Settings, which for now means the subnets SPARK discovers on.

One page with sections rather than a page per setting: retention, discovery and
alerting all belong here eventually, and a nav that grows an entry per option is
how a homelab tool starts feeling like an enterprise console.

Errors come back on the page with the values still in the form. A validation
failure that clears what you typed is a worse outcome than the typo.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import port_catalogue
from .. import scheduler as scheduler_module
from .. import snmp_config
from .. import subnets as subnet_service
from ..config import Config
from ..db import get_setting, save_setting
from ..discovery.runner import PORT_SCAN_INTERVAL_CHOICES
from ..collectors.snmp import AUTH_PROTOCOLS, PRIV_PROTOCOLS
from ..models import Device, SnmpDevice, User
from ..vault import vault_for
from .deps import get_config, get_session, redirect, require_user, templates

router = APIRouter()


async def _port_catalogue(session: AsyncSession) -> dict:
    """Everything the Port scanning card needs to describe and edit the list.

    The device count is in here because the cost of a port is not a property of
    the port -- adding one to a network of six devices is free and adding one
    to a network of two hundred is not, and the page can say which it is rather
    than leaving it to be found out.
    """
    from ..discovery.ports import NOTEWORTHY, WELL_KNOWN

    catalogue = await port_catalogue.load(session)
    effective = catalogue.ports()
    devices = await session.scalar(
        select(func.count(Device.id)).where(Device.ignored.is_(False))
    )
    devices = int(devices or 0)

    return {
        "custom": [
            {"port": entry.port, "name": entry.name,
             "shadows": WELL_KNOWN.get(entry.port)}
            for entry in sorted(catalogue.custom, key=lambda e: e.port)
        ],
        "builtins": [
            {"port": port, "name": name,
             "on": port not in catalogue.disabled,
             "concern": NOTEWORTHY.get(port)}
            for port, name in sorted(WELL_KNOWN.items())
        ],
        "builtin_total": len(WELL_KNOWN),
        "builtin_on": len(WELL_KNOWN) - len(catalogue.disabled),
        "custom_count": len(catalogue.custom),
        "max_custom": port_catalogue.MAX_CUSTOM,
        "effective": len(effective),
        "devices": devices,
        "worst_case": port_catalogue.worst_case_seconds(len(effective), devices),
    }


async def _snmp(session: AsyncSession) -> dict:
    """Everything the SNMP card needs. No secret ever leaves this function.

    Profiles are described by what they protect, not by what they contain --
    the page shows "v3 authPriv · SHA / AES" and whether a secret is set, never
    the secret. Candidate devices are the discovered ones not already listed.
    """
    profiles = await snmp_config.list_profiles(session)
    in_use = dict(
        (
            await session.execute(
                select(SnmpDevice.profile_id, func.count(SnmpDevice.id))
                .group_by(SnmpDevice.profile_id)
            )
        ).all()
    )
    listed = await snmp_config.list_devices(session)
    listed_ids = {device.id for _, device, _ in listed}
    candidates = [
        d for d in (
            await session.execute(
                select(Device).where(Device.ignored.is_(False), Device.primary_ip.isnot(None))
            )
        ).scalars().all()
        if d.id not in listed_ids
    ]
    candidates.sort(key=lambda d: _ip_sort_key(d.primary_ip))
    return {
        "profiles": [
            {
                "profile": p,
                "level": snmp_config.security_level(p),
                "devices": in_use.get(p.id, 0),
                "has_community": bool(p.community_sealed),
                "has_auth": bool(p.auth_key_sealed),
                "has_priv": bool(p.priv_key_sealed),
            }
            for p in profiles
        ],
        "devices": [
            {"row": row, "device": device, "profile": profile, "probe": row.last_probe or {}}
            for row, device, profile in listed
        ],
        "candidates": candidates,
        "auth_protocols": AUTH_PROTOCOLS,
        "priv_protocols": PRIV_PROTOCOLS,
        "min_key": snmp_config.MIN_V3_KEY,
    }


def _ip_sort_key(address: str | None) -> tuple:
    import ipaddress

    try:
        return (0, int(ipaddress.ip_address(address or "")))
    except ValueError:
        return (1, address or "")


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
    port_error: str | None = None,
    port_form: dict | None = None,
    snmp_error: str | None = None,
    snmp_form: dict | None = None,
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
            },
            "ports": await _port_catalogue(session),
            "port_error": port_error,
            "port_form": port_form or {},
            "snmp": await _snmp(session),
            "snmp_error": snmp_error,
            "snmp_form": snmp_form or {},
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


@router.post("/settings/ports")
async def add_custom_port(
    request: Request,
    port: str = Form(""),
    name: str = Form(""),
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    """Add one port to the scan.

    A rejected entry comes back on the page with the typing intact, like a bad
    CIDR does. Clearing the field is a worse outcome than the typo was.
    """
    error = await port_catalogue.add_custom(session, port, name)
    if error is not None:
        return await _render(
            request, session, config, user,
            port_error=error,
            port_form={"port": port, "name": name},
            status_code=400,
        )
    await session.commit()
    return redirect("/settings")


@router.post("/settings/ports/{port}/delete")
async def remove_custom_port(
    port: str,
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(require_user),
):
    await port_catalogue.remove_custom(session, port)
    await session.commit()
    return redirect("/settings")


@router.post("/settings/ports/builtins")
async def set_builtin_ports(
    request: Request,
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(require_user),
):
    """Record which of the built-in ports are still scanned.

    Read from the raw form rather than through a declared parameter, because
    this is a variable number of checkboxes under one name and an unticked box
    submits nothing at all -- so what arrives is the list of ports to keep, and
    the complement is what gets stored.

    Nothing already discovered is touched. Switching a port off stops SPARK
    checking it; it does not mean the service stopped answering, and marking it
    closed on the strength of not having looked would be an invention.
    """
    form = await request.form()
    await port_catalogue.set_builtins(session, form.getlist("keep"))
    await session.commit()
    return redirect("/settings")


# --------------------------------------------------------------------------
# SNMP
# --------------------------------------------------------------------------

SNMP_ANCHOR = "/settings#snmp"


def _profile_form(**fields: str) -> tuple[snmp_config.ProfileInput, dict]:
    """The submitted profile, and the part of it safe to put back on a page.

    The second value drops every secret. A form that fails validation comes
    back with the name, version, user and protocols still filled in -- and the
    community string and keys empty, like any password field. Echoing a secret
    into HTML to save someone retyping it is the trade the other way round.
    """
    data = snmp_config.ProfileInput(**fields)
    safe = {k: v for k, v in fields.items() if k not in ("community", "auth_key", "priv_key")}
    return data, safe


@router.post("/settings/snmp/profiles")
async def add_snmp_profile(
    request: Request,
    name: str = Form(""),
    version: str = Form("v2c"),
    community: str = Form(""),
    username: str = Form(""),
    auth_protocol: str = Form("SHA"),
    auth_key: str = Form(""),
    priv_protocol: str = Form("AES"),
    priv_key: str = Form(""),
    port: str = Form("161"),
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    data, safe = _profile_form(
        name=name, version=version, community=community, username=username,
        auth_protocol=auth_protocol, auth_key=auth_key, priv_protocol=priv_protocol,
        priv_key=priv_key, port=port,
    )
    try:
        await snmp_config.save_profile(session, vault_for(config), data)
    except snmp_config.ProfileError as exc:
        return await _render(request, session, config, user, snmp_error=str(exc),
                             snmp_form=safe, status_code=400)
    await session.commit()
    return redirect(SNMP_ANCHOR)


@router.post("/settings/snmp/profiles/{profile_id}")
async def edit_snmp_profile(
    request: Request,
    profile_id: int,
    name: str = Form(""),
    version: str = Form("v2c"),
    community: str = Form(""),
    username: str = Form(""),
    auth_protocol: str = Form("SHA"),
    auth_key: str = Form(""),
    priv_protocol: str = Form("AES"),
    priv_key: str = Form(""),
    port: str = Form("161"),
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    data, _safe = _profile_form(
        name=name, version=version, community=community, username=username,
        auth_protocol=auth_protocol, auth_key=auth_key, priv_protocol=priv_protocol,
        priv_key=priv_key, port=port,
    )
    try:
        await snmp_config.save_profile(session, vault_for(config), data, profile_id)
    except snmp_config.ProfileError as exc:
        return await _render(request, session, config, user, snmp_error=str(exc),
                             status_code=400)
    await session.commit()
    return redirect(SNMP_ANCHOR)


@router.post("/settings/snmp/profiles/{profile_id}/delete")
async def delete_snmp_profile(
    request: Request,
    profile_id: int,
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    try:
        await snmp_config.delete_profile(session, profile_id)
    except snmp_config.ProfileError as exc:
        return await _render(request, session, config, user, snmp_error=str(exc),
                             status_code=400)
    await session.commit()
    return redirect(SNMP_ANCHOR)


@router.post("/settings/snmp/devices")
async def add_snmp_device(
    request: Request,
    device_id: str = Form(""),
    profile_id: str = Form(""),
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    try:
        await snmp_config.add_device(session, int(device_id), int(profile_id))
    except ValueError as exc:  # ProfileError, or int() of an empty select
        message = str(exc) if isinstance(exc, snmp_config.ProfileError) else "Choose a device and a profile."
        return await _render(request, session, config, user, snmp_error=message,
                             status_code=400)
    await session.commit()
    return redirect(SNMP_ANCHOR)


@router.post("/settings/snmp/devices/{row_id}/profile")
async def change_snmp_device_profile(
    request: Request,
    row_id: int,
    profile_id: str = Form(""),
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    try:
        await snmp_config.set_device_profile(session, row_id, int(profile_id))
    except ValueError as exc:
        message = str(exc) if isinstance(exc, snmp_config.ProfileError) else "Choose a profile."
        return await _render(request, session, config, user, snmp_error=message,
                             status_code=400)
    await session.commit()
    return redirect(SNMP_ANCHOR)


@router.post("/settings/snmp/devices/{row_id}/delete")
async def remove_snmp_device(
    row_id: int,
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(require_user),
):
    await snmp_config.remove_device(session, row_id)
    await session.commit()
    return redirect(SNMP_ANCHOR)


@router.post("/settings/snmp/devices/{row_id}/test")
async def test_snmp_device(
    request: Request,
    row_id: int,
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    """Ask the device what it supports, and record the answer.

    Synchronous on purpose: it is an explicit button, the answer is what you
    pressed it for, and a probe is bounded by the profile's timeout. The
    service layer ends its transaction before touching the network, so the
    request is not sitting on an open snapshot while it waits.
    """
    try:
        await snmp_config.probe_device(session, vault_for(config), row_id)
    except snmp_config.ProfileError as exc:
        return await _render(request, session, config, user, snmp_error=str(exc),
                             status_code=400)
    await session.commit()
    return redirect(SNMP_ANCHOR)
