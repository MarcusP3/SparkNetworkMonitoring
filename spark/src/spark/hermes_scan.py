"""Hermes Agent dashboards the port scan found, read without being asked.

An open service on 9119 (the scan's "hermes dashboard"), or one named for
Hermes, is asked for its /api/status (checks/hermes.py). An answer as
Hermes is kept on the service (service.readings, migration 30) and shown
as the Hermes card on its device's page, as an API card is: nothing to set
up. Nothing alerts from this. Watch on the card makes it a Hermes check,
which does, and from then on the check reads it instead of this job.

The card is there from the moment the scan finds the port: "reading" until
the first answer, and when SPARK cannot read it, what went wrong, so a
found Hermes is never just missing. Only a port that answers as something
else (not Hermes's status, or no /api/status at all) has no card.

Every 5 minutes, answered or not; every 15 for a port that answered as
something else. Take 9119 off the scan's list and the scan stops finding
it, and this stops asking.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .checks import hermes
from .checks.base import CheckSpec
from .db import session_scope
from .discovery.services import OPEN
from .models import CheckType, Device, Service, Target, utcnow

log = logging.getLogger(__name__)

JOB_ID = "hermes:read"
TICK_SECONDS = 60
FIRST_TICK_SECONDS = 10           # soon after SPARK starts, not a minute on
EVERY = timedelta(minutes=5)
NOT_HERMES_EVERY = timedelta(minutes=15)
# In the background, so it can wait: a Hermes that has been idle can take
# seconds over its first status, and a short wait would miss it every time.
TIMEOUT_SECONDS = 20.0
AT_ONCE = 8


def _when(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def not_hermes(detail: str | None) -> bool:
    """An answer, but not a Hermes dashboard's: another app on the port."""
    return bool(detail) and (detail.startswith("not a Hermes dashboard")
                             or detail.startswith("HTTP 404"))


def due(readings: dict | None, now: datetime) -> bool:
    """Whether to ask again: never asked, or its wait is up."""
    readings = readings or {}
    tried = _when(readings.get("tried_at"))
    if tried is None:
        return True
    other = not readings.get("read_at") and readings.get("not_hermes")
    return now - tried >= (NOT_HERMES_EVERY if other else EVERY)


async def _watched(session: AsyncSession) -> tuple[set[int], set[str]]:
    """The services and addresses a Hermes check already reads."""
    rows = (await session.execute(select(Target.service_id, Target.address).where(
        Target.check_type == CheckType.HERMES))).all()
    return ({sid for sid, _ in rows if sid is not None},
            {hermes.status_url(address) for _, address in rows})


async def found(session: AsyncSession) -> list[tuple[Service, Device]]:
    """Open services that may be Hermes, on devices with an address, that
    no Hermes check reads already."""
    ids, urls = await _watched(session)
    rows = (await session.execute(
        select(Service, Device).join(Device, Service.device_id == Device.id).where(
            Service.state == OPEN, Service.ignored.is_(False),
            Device.ignored.is_(False), Device.primary_ip.is_not(None))
        .order_by(Service.id))).all()
    return [(s, d) for s, d in rows if hermes.likely(s.port, s.name) and s.id not in ids
            and hermes.status_url(f"{d.primary_ip}:{s.port}") not in urls]


async def read_due(now: datetime | None = None) -> int:
    """The minute's job: ask each one that is due. Returns how many were
    asked. Never raises."""
    now = now or utcnow()
    try:
        async with session_scope() as session:
            todo = [(s.id, f"{d.primary_ip}:{s.port}") for s, d in await found(session)
                    if due(s.readings, now)]
        if not todo:
            return 0
        gate = asyncio.Semaphore(AT_ONCE)

        async def ask(address: str):  # type: ignore[no-untyped-def]
            async with gate:
                return await hermes.check_hermes(CheckSpec(address=address,
                                                           timeout_seconds=TIMEOUT_SECONDS))
        # Asked outside any transaction: a slow dashboard holds no lock.
        outcomes = await asyncio.gather(*(ask(address) for _, address in todo))
        async with session_scope() as session:
            for (service_id, address), outcome in zip(todo, outcomes):
                service = await session.get(Service, service_id)
                if service is None:
                    continue
                stamp = now.isoformat()
                if outcome.readings is not None:
                    service.readings = {**outcome.readings, "read_at": stamp, "tried_at": stamp}
                else:              # what it said last time stays, with why not now
                    service.readings = {**(service.readings or {}), "tried_at": stamp,
                                        "error": outcome.detail,
                                        "not_hermes": not_hermes(outcome.detail)}
                    if not service.readings.get("read_at") and not service.readings["not_hermes"]:
                        log.info("%s looks like a Hermes dashboard but did not answer: %s",
                                 address, outcome.detail)
        return len(todo)
    except Exception:  # noqa: BLE001 - a job that raises is logged by APScheduler every minute
        log.exception("Reading Hermes dashboards failed")
        return 0



async def tcp_check(session: AsyncSession, service: Service, device: Device) -> Target | None:
    """The TCP check already watching this port, if one is: made by Watch
    before it knew Hermes, or by hand at the same address."""
    made = await session.scalar(select(Target).where(Target.service_id == service.id))
    if made is not None:
        return made if made.check_type == CheckType.TCP else None
    return await session.scalar(select(Target).where(
        Target.check_type == CheckType.TCP,
        Target.address == f"{device.primary_ip}:{service.port}",
        (Target.device_id == device.id) | Target.device_id.is_(None)).order_by(Target.id).limit(1))


def make_hermes(target: Target, service: Service, device: Device) -> None:
    """A TCP check becomes the Hermes check, keeping its id, its name, its
    history and its incidents. An interval left at TCP's default takes
    Hermes's; one somebody chose stays."""
    from .web.routes_targets import default_interval

    if target.interval_seconds == default_interval(CheckType.TCP):
        target.interval_seconds = default_interval(CheckType.HERMES)
    target.check_type = CheckType.HERMES
    target.address = f"{device.primary_ip}:{service.port}"
    target.params = {}              # a TCP check's port is in the address now
    target.readings = None
    target.service_id = service.id
    target.device_id = target.device_id or device.id


def cards(services: list[Service], address: str | None = None) -> list[dict]:
    """The Hermes cards for a device's found dashboards: every one but a
    port that answered as something else. Before its first answer the card
    says it is reading, or why it cannot."""
    out = []
    for service in services:
        readings = service.readings or {}
        view = hermes.view(readings)
        tried = _when(readings.get("tried_at"))
        if view is None:
            if readings.get("not_hermes"):
                continue
            out.append({"service": service, "hermes": None, "every": "5 min",
                        "reading": tried is None, "failed": tried is not None,
                        "tried_at": tried, "error": readings.get("error"),
                        "address": f"{address}:{service.port}"})
            continue
        read = view["read_at"]
        failed = bool(readings.get("error") and tried and read and tried > read)
        out.append({"service": service, "hermes": view, "every": "5 min",
                    "failed": failed, "error": readings.get("error") if failed else None})
    return out
