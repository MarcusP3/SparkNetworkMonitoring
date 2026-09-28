"""Turning observations into device rows.

All of the identity rules live here, separately from the network code, so they
can be tested without a network. They are worth testing: getting them wrong
does not crash anything, it quietly splits one device into two rows or merges
two devices into one, and you find out months later when a history looks wrong.

The rule, in one line: a device is its MAC address if we know it, and its IP
only if we don't.
"""

from __future__ import annotations

import logging

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Device, DeviceAddress
from .sweep import Observation

log = logging.getLogger(__name__)


async def record(session: AsyncSession, observation: Observation) -> tuple[Device, bool]:
    """Upsert one observation. Returns the device and whether it is new."""
    device = await _match(session, observation)
    created = device is None

    # Seen at one of its extra addresses (merged in by hand): the same
    # device, and its primary address and subnet stay what they are.
    if device is None and observation.mac is None:
        alias = await _by_extra_address(session, observation.ip)
        if alias is not None:
            device, row = alias
            row.last_seen = observation.seen_at
            device.last_seen = observation.seen_at
            await session.flush()
            return device, False

    if device is None:
        device = Device(
            mac=observation.mac,
            primary_ip=observation.ip,
            first_seen=observation.seen_at,
        )
        session.add(device)

    # A MAC-less row that we can now see a MAC for is the same device, not a
    # new one: it was found across a router before, or before ARP had resolved.
    if device.mac is None and observation.mac is not None:
        log.info(
            "Device at %s now has a MAC (%s); adopting it for stable identity",
            observation.ip, observation.mac,
        )
        device.mac = observation.mac

    if (created or device.primary_ip != observation.ip) and observation.ip:
        # This address is now this device's own. If it was listed as an
        # extra address of some other device, that listing is out of date.
        stale = delete(DeviceAddress).where(DeviceAddress.ip == observation.ip)
        if device.id is not None:
            stale = stale.where(DeviceAddress.device_id != device.id)
        await session.execute(stale)
    device.primary_ip = observation.ip
    device.last_seen = observation.seen_at
    if observation.subnet:
        device.subnet = observation.subnet
    if observation.hostname:
        device.hostname = observation.hostname
    if observation.vendor:
        device.vendor = observation.vendor

    # Deliberately NOT touching device.status. "Answered an ICMP sweep 40
    # seconds ago" is not the same claim as "is up", and the check engine owns
    # that column for anything actually being watched. Discovery's freshness
    # signal is last_seen, which the UI renders as an age.

    await session.flush()
    return device, created


async def _match(session: AsyncSession, observation: Observation) -> Device | None:
    if observation.mac:
        by_mac = await session.scalar(select(Device).where(Device.mac == observation.mac))
        if by_mac is not None:
            return by_mac
        # No MAC match: adopt an existing MAC-less row at this address rather
        # than creating a second row for a device we already know about.
        return await session.scalar(
            select(Device).where(
                Device.primary_ip == observation.ip, Device.mac.is_(None)
            )
        )

    # No MAC in hand (routed subnet). An existing row for this IP is this
    # device whether or not that row has a MAC -- creating a second one would
    # split its history in half.
    return await session.scalar(select(Device).where(Device.primary_ip == observation.ip))


async def _by_extra_address(session: AsyncSession, ip: str | None):  # type: ignore[no-untyped-def]
    if not ip:
        return None
    row = await session.scalar(select(DeviceAddress).where(DeviceAddress.ip == ip))
    if row is None:
        return None
    device = await session.get(Device, row.device_id)
    return (device, row) if device is not None else None


async def record_all(
    session: AsyncSession, observations: list[Observation]
) -> tuple[int, list[Device]]:
    """Record a whole sweep. Returns (seen, newly discovered)."""
    new: list[Device] = []
    for observation in observations:
        device, created = await record(session, observation)
        if created:
            new.append(device)
    return len(observations), new
