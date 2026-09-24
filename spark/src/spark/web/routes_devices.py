"""The device inventory: what discovery found, and what to do about it.

The "Watch" button is the point of this page. Discovery answers "what is on my
network"; watching turns one of those answers into something the check engine
polls. Without that step the inventory is trivia.
"""

from __future__ import annotations

import math
from datetime import datetime
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .. import events
from .. import scheduler as scheduler_module
from .. import subnets as subnet_service
from ..config import Config
from ..db import get_setting, save_setting
from ..discovery.ports import concern
from ..discovery.runner import SWEEP_INTERVAL_CHOICES, last_port_scan, last_sweep
from ..discovery.services import services_for
from ..models import (
    CheckType,
    Device,
    HealthStatus,
    Service,
    SnmpDevice,
    SnmpPoll,
    Target,
    User,
    utcnow,
)
from .deps import get_config, get_session, redirect, require_user, templates

router = APIRouter()

DEFAULT_SWEEP_SECONDS = 900

# The filter value for "found on no configured subnet". A sentinel rather than
# an id because there is no row to point at, and the case is worth being able
# to select: it is how you notice a segment you forgot to configure.
UNASSIGNED = "none"

# How many devices one page may show. A fixed list rather than a free number,
# for the same reason the sweep interval is one: the value ends up in a URL
# that gets bookmarked and shared, and `?per_page=100000` is a way to make this
# page render for a minute on a network large enough to need paging at all.
PAGE_SIZE_CHOICES: tuple[int, ...] = (25, 50, 100, 250)
DEFAULT_PAGE_SIZE = 50

# "Show all", kept selectable because a small network never wanted paging and
# should be able to say so once and have it stick in the URL.
ALL_ON_ONE_PAGE = 0

# Most recently seen first, then by id. The id is not decoration: a sweep
# records everything it found in one batch, so a great many devices share a
# last_seen exactly, and the order of rows tied on the only sort column is
# undefined. Undefined order plus paging means a device can sit on the boundary
# and be shown twice, or not at all -- and a device nobody sees is a device
# nobody reviews, which is what this page is for.
#
# Lifted out of the query so it can be asserted on directly. SQLite happens to
# return tied rows in rowid order, so removing the tiebreaker breaks nothing a
# behavioural test can observe here; it would surface on a different engine, or
# on a query the planner satisfies from an index. A test that cannot fail is
# not a guard, so the guard is on the ordering itself.
DEVICE_ORDER = (Device.last_seen.desc().nullslast(), Device.id.asc())


def _page_size(value: str) -> int:
    """Validate a per-page value, falling back to the default.

    Anything not on the offered list did not come from this page, and the safe
    reading of that is the default rather than an error: a mangled URL should
    show devices, not a 422.
    """
    try:
        size = int(value)
    except (TypeError, ValueError):
        return DEFAULT_PAGE_SIZE
    if size == ALL_ON_ONE_PAGE or size in PAGE_SIZE_CHOICES:
        return size
    return DEFAULT_PAGE_SIZE


# The SNMP filter's values. Anything else shows everything.
SNMP_FILTERS = {"on": "Polled over SNMP", "off": "Not polled"}


def snmp_state(row: SnmpDevice | None, poll: SnmpPoll | None) -> dict | None:
    """How a device's SNMP polling reads in a list: a word and a pill kind.

    Only the two real statuses get status colours -- answering, and not.
    Paused and "not polled yet" are facts about SPARK, not the device.
    """
    if row is None:
        return None
    if not row.enabled:
        return {"label": "paused", "kind": "neutral"}
    if poll is None or poll.last_polled_at is None:
        return {"label": "waiting", "kind": "neutral"}
    if poll.last_error:
        return {"label": "no answer", "kind": "bad"}
    return {"label": "polling", "kind": "ok"}


def _query(subnet: str, per_page: int, page: int, snmp: str = "") -> str:
    """The Devices URL for one combination of filter, page size and page.

    Defaults are left out rather than spelled out, so the plain `/devices` link
    stays plain and a shared URL carries only what was actually chosen.
    """
    parts: list[tuple[str, str]] = []
    if subnet:
        parts.append(("subnet", subnet))
    if snmp in SNMP_FILTERS:
        parts.append(("snmp", snmp))
    if per_page != DEFAULT_PAGE_SIZE:
        parts.append(("per_page", str(per_page)))
    if page > 1:
        parts.append(("page", str(page)))
    return f"?{urlencode(parts)}" if parts else ""


def _paginate(rows, subnet: str, per_page: int, page: int,  # type: ignore[no-untyped-def]
              snmp: str = ""):
    """Cut the list down to one page. Returns (paging, rows).

    Sliced in Python rather than with LIMIT/OFFSET because the subnet a device
    is on is worked out from its address, not stored on the row -- so the filter
    this has to compose with cannot be expressed in SQL. At homelab scale the
    whole list is a few hundred dicts; when that stops being true the subnet
    would need to be denormalised onto the device first.

    The page number is clamped rather than honoured blindly. Devices arrive and
    leave between refreshes, so a bookmark or a live refresh pointing past the
    end should land on the last page -- an empty table reads as a network that
    vanished, which is the one thing this page must never say by accident.
    """
    total = len(rows)
    limited = per_page != ALL_ON_ONE_PAGE and total > per_page
    pages = max(1, math.ceil(total / per_page)) if limited else 1
    page = min(max(1, page), pages)

    if limited:
        start = (page - 1) * per_page
        window = rows[start:start + per_page]
    else:
        start = 0
        window = rows

    return {
        "limited": limited,
        "per_page": per_page,
        "choices": PAGE_SIZE_CHOICES,
        "all_value": ALL_ON_ONE_PAGE,
        # The dropdown is clutter on a network that fits on one page anyway --
        # but it has to stay visible once it has been touched, or there is no
        # way back from "25" on a 30-device network.
        "offer": total > min(PAGE_SIZE_CHOICES) or per_page != DEFAULT_PAGE_SIZE,
        "page": page,
        "pages": pages,
        "first": start + 1 if window else 0,
        "last": start + len(window),
        "total": total,
        "prev_url": _query(subnet, per_page, page - 1, snmp) if page > 1 else None,
        "next_url": _query(subnet, per_page, page + 1, snmp) if page < pages else None,
    }, window


def _apply_subnet_filter(rows, known_subnets, choice):  # type: ignore[no-untyped-def]
    """Narrow the device list to one subnet. Returns (selected, rows).

    An unrecognised value falls back to showing everything rather than showing
    nothing: a stale bookmark pointing at a deleted subnet should not look like
    a network that emptied out.
    """
    if not choice:
        return None, rows
    if choice == UNASSIGNED:
        return UNASSIGNED, [r for r in rows if r["subnet"] is None]
    try:
        wanted = int(choice)
    except (TypeError, ValueError):
        return None, rows
    selected = next((s for s in known_subnets if s.id == wanted), None)
    if selected is None:
        return None, rows
    return selected, [r for r in rows if r["subnet"] is not None and r["subnet"].id == wanted]


def _in_words(seconds: float) -> str:
    """A rough duration, because a precise one would be a lie.

    The sweep job carries up to 10% jitter, so "in 14m" claims an accuracy the
    schedule does not have.
    """
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "under a minute"
    minutes = seconds // 60
    if minutes < 60:
        return f"about {minutes} minute{'s' if minutes != 1 else ''}"
    hours = round(minutes / 60)
    return f"about {hours} hour{'s' if hours != 1 else ''}"


async def _scan_schedule(session: AsyncSession, subnet_count: int) -> dict:
    """What the Devices page needs to describe and edit automatic scanning.

    `next_run` comes from the scheduler rather than from the settings, because
    the settings say what was asked for and the scheduler says what is actually
    going to happen. Those disagree in exactly the case worth surfacing: the
    box is ticked but no subnets are configured, so nothing is scheduled.
    """
    settings = await get_setting(session, "discovery")
    enabled = bool(settings.get("enabled", True))
    seconds = int(settings.get("sweep_interval_seconds", DEFAULT_SWEEP_SECONDS)
                  or DEFAULT_SWEEP_SECONDS)

    next_run = scheduler_module.discovery_next_run()
    next_in = None
    if next_run is not None:
        next_in = max(0.0, (next_run - datetime.now(next_run.tzinfo)).total_seconds())

    if not enabled:
        text = "Automatic scanning is off."
    elif not subnet_count:
        text = "No subnets are configured, so there is nothing to scan."
    elif next_in is None:
        text = "Not scheduled — restart SPARK if this persists."
    else:
        text = f"Next scan in {_in_words(next_in)}."

    return {
        "enabled": enabled,
        "interval_minutes": max(1, seconds // 60),
        "choices": SWEEP_INTERVAL_CHOICES,
        "next_in": None if next_in is None else int(next_in),
        "interval_seconds": seconds,
        "text": text,
    }


@router.get("/devices")
async def list_devices(
    request: Request,
    subnet: str = "",
    per_page: str = "",
    page: str = "",
    snmp: str = "",
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    devices = list(
        (
            await session.execute(
                select(Device)
                .where(Device.ignored.is_(False))
                .order_by(*DEVICE_ORDER)
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

    known_subnets = await subnet_service.list_subnets(session)

    # SNMP status for every device in one query: which are on the SNMP list,
    # and how their last poll went. It is what the SNMP column and filter
    # show, so nobody has to open each device to find out.
    snmp_rows = {
        row.device_id: (row, poll)
        for row, poll in (
            await session.execute(
                select(SnmpDevice, SnmpPoll)
                .outerjoin(SnmpPoll, SnmpPoll.snmp_device_id == SnmpDevice.id)
            )
        ).all()
    }

    now = utcnow()
    rows = []
    for device in devices:
        age = (now - device.last_seen).total_seconds() if device.last_seen else None
        rows.append(
            {
                "device": device,
                "age_seconds": age,
                "watched": device.id in watched_ids,
                # Worked out from the address rather than read from the label
                # recorded at discovery time, so renaming a subnet does not
                # orphan its devices and adding one classifies what is already
                # there.
                "subnet": subnet_service.subnet_for(known_subnets, device.primary_ip),
                "snmp": snmp_state(*snmp_rows.get(device.id, (None, None))),
                "services": [],
            }
        )

    # Counted before filtering: "3 of 41" is the useful reading, and a filtered
    # count that shrinks as you narrow tells you nothing.
    total = len(rows)
    selected, rows = _apply_subnet_filter(rows, known_subnets, subnet)
    snmp = snmp if snmp in SNMP_FILTERS else ""
    if snmp == "on":
        rows = [r for r in rows if r["snmp"] is not None]
    elif snmp == "off":
        rows = [r for r in rows if r["snmp"] is None]

    # Counted before paging, deliberately. "Mark all 12 reviewed" acts on every
    # unacknowledged device, so a number that shrank to what happens to be on
    # screen would understate what the button does.
    unreviewed = sum(1 for row in rows if not row["device"].acknowledged)

    size = _page_size(per_page)
    try:
        wanted_page = int(page)
    except (TypeError, ValueError):
        wanted_page = 1
    paging, rows = _paginate(rows, subnet, size, wanted_page, snmp)

    # Services are fetched after paging rather than before: one query either
    # way, but for the devices actually being shown rather than for every
    # device that exists.
    by_device = await services_for(session, [r["device"].id for r in rows])
    for row in rows:
        row["services"] = [
            {"service": svc, "concern": concern(svc.port)}
            for svc in by_device.get(row["device"].id, [])
        ]

    port_scan = await last_port_scan(session)
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
            "unreviewed": unreviewed,
            "subnets": known_subnets,
            "subnet_filter": {
                "selected": selected,
                "value": subnet,
                # No "shown" here. How many the filter matched is `paging.total`
                # -- one number with one owner. Keeping a second copy is how it
                # ends up quietly meaning "how many are on screen", which is the
                # failure paging introduces to every count on this page.
                "total": total,
                "unassigned": sum(
                    1 for r in rows if r["subnet"] is None
                ) if subnet == UNASSIGNED else None,
            },
            "paging": paging,
            "snmp_filter": {
                "value": snmp,
                "choices": SNMP_FILTERS,
                "listed": len(snmp_rows),
            },
            "filtered": bool(selected) or bool(snmp),
            "sweep": sweep,
            "port_scan": port_scan,
            "scan": await _scan_schedule(
                session, sum(1 for s in known_subnets if s.enabled)
            ),
        },
    )


@router.post("/devices/schedule")
async def set_scan_schedule(
    auto: str = Form(""),
    interval_minutes: str = Form(""),
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    _user: User = Depends(require_user),
):
    """Turn automatic scanning on or off and set how often it runs.

    Applied to the running scheduler rather than only written to the database,
    so it takes effect now. A setting that needs a restart to mean anything is
    a setting people stop believing.

    The interval is validated against the offered list instead of being coerced
    into a range: a value that is not one of the choices did not come from the
    page, and the safe reading of that is to keep what is already there.
    """
    settings = await get_setting(session, "discovery")
    settings["enabled"] = auto == "1"
    try:
        minutes = int(interval_minutes)
    except (TypeError, ValueError):
        minutes = 0
    if minutes in SWEEP_INTERVAL_CHOICES:
        settings["sweep_interval_seconds"] = minutes * 60

    await save_setting(session, "discovery", settings)
    await session.commit()

    # Settings passed in rather than re-read: this request may still hold the
    # write lock, and a second session reading it is how the page hung before.
    await scheduler_module.schedule_discovery(config, settings)
    return redirect("/devices")


@router.post("/devices/scan-ports")
async def scan_ports_now(_user: User = Depends(require_user)):
    """Queue a port scan now. Returns at once; the page updates itself."""
    scheduler_module.trigger_port_scan_now()
    return redirect("/devices")


@router.post("/devices/services/{service_id}/watch")
async def watch_service(
    service_id: int,
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(require_user),
):
    """Turn a discovered service into a TCP target.

    A TCP check rather than a ping: the point of watching a service is to know
    whether the service answers, and a host that pings while its database is
    dead is exactly the outage you wanted to catch.
    """
    service = await session.get(Service, service_id)
    if service is None:
        return redirect("/devices")
    device = await session.get(Device, service.device_id)
    if device is None or not device.primary_ip:
        return redirect("/devices")

    existing = await session.scalar(
        select(Target).where(Target.service_id == service.id)
    )
    if existing is not None:
        return redirect("/targets")

    label = service.name or f"port {service.port}"
    target = Target(
        name=f"{device.display_name} {label}",
        check_type=CheckType.TCP,
        address=f"{device.primary_ip}:{service.port}",
        device_id=device.id,
        service_id=service.id,
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
    """Set or clear a device's friendly name.

    Naming a device does acknowledge it -- taking the trouble to label
    something is review -- but pressing Save on an untouched row does not.
    Previously it did, which meant the "new" badge could be cleared by a
    button that appeared to do nothing, and the badge is the security signal
    on this page: an unfamiliar MAC appearing at 2am is the thing you want to
    notice.
    """
    device = await session.get(Device, device_id)
    if device is not None:
        name = friendly_name.strip()
        device.friendly_name = name or None
        if name:
            device.acknowledged = True
        await session.commit()
        events.publish({"kind": "device-renamed", "device_id": device_id})
    return redirect("/devices")


@router.post("/devices/acknowledge-all")
async def acknowledge_all(
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(require_user),
):
    """Mark every currently-listed device as reviewed.

    The first sweep of a real network produces a screenful of badges at once.
    Dismissing them one at a time teaches you to ignore the badge, which is
    the opposite of what it is for.
    """
    await session.execute(
        update(Device).where(Device.acknowledged.is_(False)).values(acknowledged=True)
    )
    await session.commit()
    events.publish({"kind": "devices-acknowledged"})
    return redirect("/devices")


@router.post("/devices/{device_id}/acknowledge")
async def acknowledge_device(
    device_id: int,
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(require_user),
):
    """Dismiss the "new" badge without naming the device."""
    device = await session.get(Device, device_id)
    if device is not None:
        device.acknowledged = True
        await session.commit()
        events.publish({"kind": "device-acknowledged", "device_id": device_id})
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
