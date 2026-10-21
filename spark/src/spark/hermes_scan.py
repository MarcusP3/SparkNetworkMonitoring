"""Hermes Agent dashboards the port scan found, read without being asked.

An open service on 9119 (the scan's "hermes dashboard"), or one named for
Hermes, is asked for its /api/status (checks/hermes.py). An answer as
Hermes is kept on the service (service.readings, migration 30) and shown
as the Hermes card on its device's page, as an API card is: nothing to set
up. Nothing alerts from this. Watch on the card makes it a Hermes check,
which does, and from then on the check reads it instead of this job.

Every 5 minutes once it has answered as Hermes, every 15 while it never
has (another app on 9119, or a Hermes listening only to itself): a port
that is not Hermes costs one request a quarter of an hour. Take 9119 off
the scan's list and the scan stops finding it, and this stops asking.
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
EVERY = timedelta(minutes=5)
UNTIL_IT_ANSWERS = timedelta(minutes=15)
TIMEOUT_SECONDS = 5.0
AT_ONCE = 8


def _when(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def due(readings: dict | None, now: datetime) -> bool:
    """Whether to ask again: never asked, or its wait is up."""
    tried = _when((readings or {}).get("tried_at"))
    if tried is None:
        return True
    return now - tried >= (EVERY if (readings or {}).get("read_at") else UNTIL_IT_ANSWERS)


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
            for (service_id, _), outcome in zip(todo, outcomes):
                service = await session.get(Service, service_id)
                if service is None:
                    continue
                stamp = now.isoformat()
                if outcome.readings is not None:
                    service.readings = {**outcome.readings, "read_at": stamp, "tried_at": stamp}
                else:              # what it said last time stays, with why not now
                    service.readings = {**(service.readings or {}), "tried_at": stamp,
                                        "error": outcome.detail}
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


def cards(services: list[Service]) -> list[dict]:
    """The Hermes cards for a device's found dashboards: those that have
    answered as Hermes at least once."""
    out = []
    for service in services:
        view = hermes.view(service.readings)
        if view is None:
            continue
        readings = service.readings or {}
        tried, read = _when(readings.get("tried_at")), view["read_at"]
        failed = bool(readings.get("error") and tried and read and tried > read)
        out.append({"service": service, "hermes": view, "every": "5 min",
                    "failed": failed, "error": readings.get("error") if failed else None})
    return out
