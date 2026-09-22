"""Turning a port scan into service rows.

The counterpart to `store.py`, which does the same job for devices, and it
inherits the same rule: what a scan found is evidence, not a verdict. A service
that stops answering is marked as such rather than deleted, because "this host
used to run Postgres and no longer does" is exactly the kind of thing you want
the inventory to be able to tell you, and a DELETE cannot.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Service, ServiceSource, utcnow
from .ports import HostScan, port_name

log = logging.getLogger(__name__)

# What `state` holds. A string rather than an enum because increment 1 defined
# the column that way and a migration to tidy a diagnostic is not worth it.
OPEN = "open"
CLOSED = "closed"


async def record_scan(session: AsyncSession, scan: HostScan) -> tuple[int, int]:
    """Record one device's scan. Returns (services seen, newly found).

    Services found by a scan never overwrite ones that came from somewhere
    else. A container inventory knows a service's name and image; a scan knows
    a port answered. Letting the weaker source win would quietly degrade the
    record every six hours.
    """
    if scan.device_id is None:
        return 0, 0

    now = utcnow()
    existing = {
        (row.port, row.protocol): row
        for row in (
            await session.execute(
                select(Service).where(Service.device_id == scan.device_id)
            )
        )
        .scalars()
        .all()
    }

    seen = 0
    new = 0
    open_keys: set[tuple[int, str]] = set()

    for found in scan.open_ports:
        key = (found.port, "tcp")
        open_keys.add(key)
        seen += 1
        service = existing.get(key)
        if service is None:
            session.add(
                Service(
                    device_id=scan.device_id,
                    port=found.port,
                    protocol="tcp",
                    name=found.name or port_name(found.port),
                    source=ServiceSource.SCAN,
                    state=OPEN,
                    first_seen=now,
                    last_seen=now,
                )
            )
            new += 1
            continue

        service.state = OPEN
        service.last_seen = now
        # Only fill a name in, never replace one: a name from Docker or typed
        # by a person is better than anything a port number can tell us.
        if not service.name:
            service.name = found.name or port_name(found.port)

    # Anything this scan covered and did not find is closed now. Scoped to
    # SCAN-sourced rows: a service known from a container inventory is not
    # absent just because its port was shut to us.
    #
    # "Covered" is now asked rather than assumed, and the difference is not
    # academic. The port list is editable, so a port can leave the scan while
    # the service on it is still running -- because you switched off SMB, or
    # because the control probe found the port intercepted and excluded it.
    # Reading "not in the results" as "not listening" would then mark a live
    # service closed on the strength of never having looked at it.
    for (port, protocol), service in existing.items():
        if (port, protocol) in open_keys:
            continue
        if service.source is not ServiceSource.SCAN:
            continue
        if port not in scan.covered:
            continue
        if service.state != CLOSED:
            log.info(
                "Service %s/%s on device %s stopped answering",
                port, protocol, scan.device_id,
            )
        service.state = CLOSED

    return seen, new


async def record_all(session: AsyncSession, scans: list[HostScan]) -> tuple[int, int]:
    total_seen = 0
    total_new = 0
    for scan in scans:
        seen, new = await record_scan(session, scan)
        total_seen += seen
        total_new += new
    return total_seen, total_new


async def services_for(session: AsyncSession, device_ids: list[int]) -> dict[int, list[Service]]:
    """Open services per device, port-ordered, for the Devices page.

    One query for every device on the page rather than one per row: the page
    renders every device it knows about, and a query per row is the N+1 that
    turns a fast page into a slow one the moment the inventory grows.
    """
    if not device_ids:
        return {}
    rows = (
        await session.execute(
            select(Service)
            .where(
                Service.device_id.in_(device_ids),
                Service.ignored.is_(False),
                Service.state != CLOSED,
            )
            .order_by(Service.device_id, Service.port)
        )
    ).scalars().all()

    grouped: dict[int, list[Service]] = {}
    for service in rows:
        grouped.setdefault(service.device_id, []).append(service)
    return grouped
