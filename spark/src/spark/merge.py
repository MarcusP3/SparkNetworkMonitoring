"""Merging a duplicate device into the real one.

The case it exists for: a firewall with a gateway address on every VLAN.
SPARK sees MACs only on the subnet it sits on, so across a router each of
those addresses is identified by IP alone, and became a device of its own.
Merging says "that address is this device":

  * the duplicate's addresses become extra addresses of the one kept, and
    later sweeps record anything found there against it (discovery/store.py);
  * its targets, with their history, move across;
  * its services move across, except a port the kept device already has;
  * devices connected below it on the service map are connected to the one
    kept instead;
  * its name, MAC and role fill in only what the kept device lacks;
  * the duplicate row is then deleted, and with it its own mute entry --
    muting the duplicate was about the duplicate.

Refused when both are polled over SNMP: two credential sets and two
histories for one box is a decision for a person, not a merge rule.

`plan` says all of that in advance, for the confirmation page, without
changing anything; `apply` does it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Device, DeviceAddress, DeviceRole, MapAuto, Service, SnmpDevice, Target


class MergeError(ValueError):
    """Why these two cannot be merged. The message is for a person."""


@dataclass
class Plan:
    keep: Device
    other: Device
    addresses: list[str] = field(default_factory=list)
    targets: list[Target] = field(default_factory=list)
    services: list[Service] = field(default_factory=list)       # move across
    services_dropped: list[Service] = field(default_factory=list)  # keep has the port
    children: int = 0
    takes_mac: bool = False
    takes_name: bool = False
    takes_snmp: bool = False


async def plan(session: AsyncSession, keep_id: int, other_id: int) -> Plan:
    if keep_id == other_id:
        raise MergeError("A device cannot be merged into itself.")
    keep = await session.get(Device, keep_id)
    other = await session.get(Device, other_id)
    if keep is None or other is None:
        raise MergeError("That device no longer exists.")
    if keep.mac and other.mac and keep.mac != other.mac:
        raise MergeError(
            f"Both have a MAC address, and they differ ({keep.mac}, {other.mac}): "
            "these look like two real devices, not one seen twice."
        )
    polled = {
        row.device_id for row in (await session.execute(
            select(SnmpDevice).where(SnmpDevice.device_id.in_([keep_id, other_id]))
        )).scalars()
    }
    if polled == {keep_id, other_id}:
        raise MergeError("Both are polled over SNMP. Remove one from the SNMP list first.")

    extra = list((await session.execute(
        select(DeviceAddress.ip).where(DeviceAddress.device_id == other_id)
    )).scalars())
    addresses = [ip for ip in [other.primary_ip, *extra] if ip and ip != keep.primary_ip]

    keep_ports = {(s.port, s.protocol) for s in (await session.execute(
        select(Service).where(Service.device_id == keep_id)
    )).scalars()}
    services = list((await session.execute(
        select(Service).where(Service.device_id == other_id).order_by(Service.port)
    )).scalars())

    return Plan(
        keep=keep, other=other, addresses=addresses,
        targets=list((await session.execute(
            select(Target).where(Target.device_id == other_id).order_by(Target.name)
        )).scalars()),
        services=[s for s in services if (s.port, s.protocol) not in keep_ports],
        services_dropped=[s for s in services if (s.port, s.protocol) in keep_ports],
        children=len((await session.execute(
            select(Device.id).where(Device.parent_device_id == other_id)
        )).all()),
        takes_mac=bool(other.mac and not keep.mac),
        takes_name=bool(other.friendly_name and not keep.friendly_name),
        takes_snmp=other_id in polled,
    )


async def apply(session: AsyncSession, keep_id: int, other_id: int) -> Plan:
    """Merge `other_id` into `keep_id`. The caller commits."""
    p = await plan(session, keep_id, other_id)
    keep, other = p.keep, p.other

    # Services first: a dropped duplicate may be what a target watches, and
    # that target should end up watching the kept device's copy of the port.
    kept_by_port = {(s.port, s.protocol): s for s in (await session.execute(
        select(Service).where(Service.device_id == keep_id)
    )).scalars()}
    for service in p.services_dropped:
        twin = kept_by_port[(service.port, service.protocol)]
        await session.execute(update(Target).where(Target.service_id == service.id)
                              .values(service_id=twin.id))
        await session.delete(service)
    for service in p.services:
        service.device_id = keep_id

    await session.execute(update(Target).where(Target.device_id == other_id)
                          .values(device_id=keep_id))
    await session.execute(update(Device).where(Device.parent_device_id == other_id)
                          .values(parent_device_id=keep_id))
    # What automatic mode placed under the duplicate is still automatic's.
    await session.execute(update(MapAuto).where(MapAuto.parent_device_id == other_id)
                          .values(parent_device_id=keep_id))
    if p.takes_snmp:
        await session.execute(update(SnmpDevice).where(SnmpDevice.device_id == other_id)
                              .values(device_id=keep_id))

    # Its extra addresses move with it; its primary one becomes an extra one.
    await session.execute(update(DeviceAddress).where(DeviceAddress.device_id == other_id)
                          .values(device_id=keep_id))
    mac, name = other.mac, other.friendly_name
    primary, last_seen = other.primary_ip, other.last_seen
    if keep.parent_device_id == other_id:
        keep.parent_device_id = other.parent_device_id
    if keep.role == DeviceRole.UNKNOWN and other.role != DeviceRole.UNKNOWN:
        keep.role = other.role
    await session.flush()
    await session.delete(other)
    await session.flush()   # the MAC and address are free once the row is gone

    if p.takes_mac:
        keep.mac = mac
    if p.takes_name:
        keep.friendly_name = name
    if primary and primary != keep.primary_ip:
        existing = await session.scalar(select(DeviceAddress).where(DeviceAddress.ip == primary))
        if existing is None:
            session.add(DeviceAddress(device_id=keep_id, ip=primary, last_seen=last_seen))
        else:
            existing.device_id = keep_id
    keep.acknowledged = True
    await session.flush()
    return p


async def addresses_of(session: AsyncSession, device_id: int) -> list[DeviceAddress]:
    return list((await session.execute(
        select(DeviceAddress).where(DeviceAddress.device_id == device_id)
        .order_by(DeviceAddress.ip)
    )).scalars())
