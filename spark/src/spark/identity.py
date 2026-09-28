"""Recognising one device at several addresses, from what SNMP devices report.

Every 15 minutes each device on the SNMP list is asked for its own addresses,
its ARP table, its MAC table and its LLDP neighbours. The last two are for the
map (topology.py); this module is about the first two:

  * **its own addresses** (IP-MIB). A firewall lists its gateway on every
    VLAN. A device SPARK found at one of those addresses is the firewall seen
    again.
  * **its ARP table**. Across a router SPARK sees no MACs, so those devices
    are known by IP alone; the router's ARP table has the MAC for each.

What comes of it:

  * **Merges are suggested, never made.** A person confirms each one on the
    usual merge preview (merge.py), or says "Not the same" and the suggestion
    stays gone. Two kinds:
      - "own": device X is at one of polled device P's own addresses.
      - "mac": device X has no MAC, the ARP table gives one for its address,
        and another device already has that MAC. (A server with VLAN
        interfaces on one NIC has one MAC on several VLANs.)
  * **A missing MAC is filled in** when the ARP table gives one for exactly
    one MAC-less device and no device has it yet. That is not a merge -- the
    device just gains the identity a sweep on its own subnet would have
    given it. Never for an address a polled device says is its own: that
    device is a duplicate awaiting a merge, and a MAC of its own would block
    the merge (two different MACs are two devices, merge.py).

The ARP table is also what the automatic map will need to place devices on
routed VLANs. That map is next on the backlog; the table is kept for it.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .collectors import SnmpCollector
from .db import get_setting, save_setting, session_scope
from .discovery import oui
from .models import Device, SnmpAddress, SnmpDevice, SnmpProfile, utcnow
from .snmp_config import credential_for
from .vault import SecretUnavailable, vault_for

log = logging.getLogger(__name__)

JOB_ID = "snmp-identity"
REFRESH_MINUTES = 15
FIRST_RUN_SECONDS = 45     # after start, and after a device joins the SNMP list
TIMEOUT = 3.0

DISMISSED = "merge_dismissed"
MAX_DISMISSED = 500

OWN, ARP = "own", "arp"


# --------------------------------------------------------------------------
# Reading the devices
# --------------------------------------------------------------------------


async def refresh(config) -> int:  # type: ignore[no-untyped-def]
    """The scheduler's job: ask every polled device, one at a time.

    Returns how many answered. Never raises; a device that fails keeps what
    it said last time.
    """
    try:
        async with session_scope() as session:
            rows = list((await session.execute(
                select(SnmpDevice.id).where(SnmpDevice.enabled.is_(True)).order_by(SnmpDevice.id)
            )).scalars())
        answered = 0
        for row_id in rows:
            if await refresh_one(config, row_id):
                answered += 1
        async with session_scope() as session:
            await fill_in_macs(session)
        return answered
    except Exception:  # noqa: BLE001 - the scheduler must keep running
        log.exception("Reading addresses over SNMP failed")
        return 0


async def refresh_one(config, row_id: int) -> bool:  # type: ignore[no-untyped-def]
    """Ask one device; the same three phases as a poll (snmp_poll.py)."""
    async with session_scope() as session:
        row = await session.get(SnmpDevice, row_id)
        if row is None or not row.enabled:
            return False
        device = await session.get(Device, row.device_id)
        profile = await session.get(SnmpProfile, row.profile_id)
        address = device.primary_ip if device else None
        if not address or profile is None:
            return False
        try:
            credential = credential_for(profile, vault_for(config), timeout=TIMEOUT, retries=1)
        except SecretUnavailable:
            return False

    started = time.monotonic()
    collector = SnmpCollector(address, credential)
    own: list[str] | None = None
    arp: dict[str, str] | None = None
    fdb = lldp = None
    try:
        try:
            own = await collector.own_addresses()
        except Exception as exc:  # noqa: BLE001
            log.info("%s: no address list over SNMP (%s)", address, exc)
        if own is not None:   # no answer at all: do not wait out more timeouts
            arp = await _ask(collector.arp_table, address, "ARP table")
            fdb = await _ask(collector.bridge_table, address, "MAC table")
            lldp = await _ask(collector.lldp_neighbours, address, "LLDP neighbours")
    finally:
        await collector.close()
    log.debug("Read addresses from %s in %.1fs", address, time.monotonic() - started)

    from . import topology

    async with session_scope() as session:
        if await session.get(SnmpDevice, row_id) is None:
            return False
        await store(session, row_id, own=own, arp=arp)
        await topology.store(session, row_id, fdb=fdb, lldp=lldp)
    return own is not None


async def _ask(method, address: str, what: str):  # type: ignore[no-untyped-def]
    """One table; None (keep the last answer) if it could not be read."""
    try:
        return await method()
    except Exception as exc:  # noqa: BLE001
        log.info("%s: no %s over SNMP (%s)", address, what, exc)
        return None


async def store(session: AsyncSession, row_id: int, *, own: list[str] | None,
                arp: dict[str, str] | None) -> None:
    """Replace what this device said last time. None means "did not answer":
    the old rows stay, so one lost packet does not make suggestions flicker."""
    now = utcnow()
    for kind, found in ((OWN, own), (ARP, arp)):
        if found is None:
            continue
        await session.execute(delete(SnmpAddress).where(
            SnmpAddress.snmp_device_id == row_id, SnmpAddress.kind == kind))
        pairs = found.items() if isinstance(found, dict) else ((ip, None) for ip in found)
        session.add_all(SnmpAddress(snmp_device_id=row_id, kind=kind, ip=ip, mac=mac,
                                    seen_at=now) for ip, mac in pairs)
    await session.flush()


# --------------------------------------------------------------------------
# What the reports say, together
# --------------------------------------------------------------------------


@dataclass
class Reports:
    own: dict[str, int]                  # ip -> device id of the device that holds it
    arp: dict[str, tuple[str, int]]      # ip -> (mac, device id of the router that saw it)
    polled: set[int]
    read_at: dict[int, dict]             # device id -> {"own": n, "arp": n, "at": datetime}


async def reports(session: AsyncSession) -> Reports:
    rows = (await session.execute(
        select(SnmpAddress, SnmpDevice.device_id)
        .join(SnmpDevice, SnmpDevice.id == SnmpAddress.snmp_device_id)
        .order_by(SnmpDevice.device_id, SnmpAddress.ip)
    )).all()
    own: dict[str, int] = {}
    arp: dict[str, tuple[str, int]] = {}
    disputed: set[str] = set()
    read_at: dict[int, dict] = {}
    for row, device_id in rows:
        summary = read_at.setdefault(device_id, {OWN: 0, ARP: 0, "at": row.seen_at})
        summary[row.kind] += 1
        summary["at"] = max(summary["at"], row.seen_at)
        if row.kind == OWN:
            own.setdefault(row.ip, device_id)
        elif row.mac:
            # Two routers that disagree about an address: trust neither.
            seen = arp.setdefault(row.ip, (row.mac, device_id))
            if seen[0] != row.mac:
                disputed.add(row.ip)
    for ip in disputed:
        arp.pop(ip, None)
    polled = set((await session.execute(select(SnmpDevice.device_id))).scalars())
    return Reports(own=own, arp=arp, polled=polled, read_at=read_at)


async def fill_in_macs(session: AsyncSession) -> list[Device]:
    """Give MAC-less devices the MAC the ARP table has for them. See the top."""
    r = await reports(session)
    devices = list((await session.execute(select(Device).order_by(Device.id))).scalars())
    in_use = {d.mac for d in devices if d.mac}
    wanting: dict[str, list[Device]] = {}
    for device in devices:
        if device.mac or not device.primary_ip or device.primary_ip in r.own:
            continue
        found = r.arp.get(device.primary_ip)
        if found:
            wanting.setdefault(found[0], []).append(device)
    filled = []
    for mac, candidates in wanting.items():
        if len(candidates) == 1 and mac not in in_use:
            device = candidates[0]
            device.mac = mac
            device.vendor = device.vendor or oui.lookup(mac)
            filled.append(device)
            log.info("%s: MAC %s from the ARP table", device.primary_ip, mac)
    await session.flush()
    return filled


@dataclass
class Suggestion:
    keep: Device
    other: Device
    kind: str             # OWN or ARP
    source: Device        # the device whose report says so
    mac: str | None = None

    @property
    def key(self) -> str:
        return dismissal_key(self.keep.id, self.other.primary_ip)


def dismissal_key(keep_id: int, ip: str | None) -> str:
    # By address, not device id: a device deleted and found again gets a new
    # id, and "that address is not this device" is still the answer given.
    return f"{keep_id}:{ip}"


async def suggestions(session: AsyncSession) -> list[Suggestion]:
    """Every merge the SNMP reports point to that nobody has ruled out.

    Leaves out what merge.py would refuse anyway (two different MACs, both
    polled), ignored devices, and anything dismissed. One suggestion per
    duplicate; its own address beats an ARP match.
    """
    r = await reports(session)
    if not r.own and not r.arp:
        return []
    devices = {d.id: d for d in (await session.execute(select(Device))).scalars()}
    by_ip: dict[str, Device] = {}
    by_mac: dict[str, Device] = {}
    for device in sorted(devices.values(), key=lambda d: d.id):
        if device.primary_ip:
            by_ip.setdefault(device.primary_ip, device)
        if device.mac:
            by_mac[device.mac] = device
    dismissed = set((await get_setting(session, DISMISSED)).get("pairs", []))
    found: dict[int, Suggestion] = {}

    def offer(keep: Device | None, other: Device | None, kind: str, source: Device | None,
              mac: str | None = None) -> None:
        if keep is None or other is None or source is None or keep.id == other.id:
            return
        if other.id in found or keep.ignored or other.ignored:
            return
        if keep.mac and other.mac and keep.mac != other.mac:
            return
        if keep.id in r.polled and other.id in r.polled:
            return
        suggestion = Suggestion(keep, other, kind, source, mac)
        if suggestion.key not in dismissed:
            found[other.id] = suggestion

    for ip, owner_id in r.own.items():
        owner = devices.get(owner_id)
        offer(owner, by_ip.get(ip), OWN, owner)

    unowned: dict[str, list[tuple[Device, int]]] = {}
    for ip, (mac, router_id) in r.arp.items():
        other = by_ip.get(ip)
        if other is None or other.mac:
            continue
        if mac in by_mac:
            offer(by_mac[mac], other, ARP, devices.get(router_id), mac)
        else:
            unowned.setdefault(mac, []).append((other, router_id))
    # Several MAC-less devices behind one MAC nobody has yet: the same
    # device at several addresses. The oldest row is the one kept.
    for mac, group in unowned.items():
        group.sort(key=lambda pair: pair[0].id)
        keep = group[0][0]
        for other, router_id in group[1:]:
            offer(keep, other, ARP, devices.get(router_id), mac)

    return sorted(found.values(),
                  key=lambda s: (s.keep.display_name.lower(), _ip_order(s.other.primary_ip)))


def _ip_order(ip: str | None) -> tuple:
    try:
        return tuple(int(part) for part in (ip or "").split("."))
    except ValueError:
        return (ip or "",)


def why(s: Suggestion) -> str:
    """One sentence, for a person deciding whether to merge."""
    if s.kind == OWN:
        return (f"{s.keep.display_name} reports {s.other.primary_ip} as one of its own "
                f"addresses.")
    return (f"{s.source.display_name}'s ARP table shows {s.other.primary_ip} and "
            f"{s.keep.primary_ip or s.keep.display_name} at the same MAC, {s.mac}.")


async def dismiss(session: AsyncSession, keep_id: int, ip: str | None) -> None:
    value = await get_setting(session, DISMISSED)
    pairs = [p for p in value.get("pairs", []) if isinstance(p, str)]
    key = dismissal_key(keep_id, ip)
    if key not in pairs:
        pairs.append(key)
    await save_setting(session, DISMISSED, {"pairs": pairs[-MAX_DISMISSED:]})


def next_run_soon(scheduler) -> None:  # type: ignore[no-untyped-def]
    """A device just joined the SNMP list: read it in a moment, not in 15 minutes."""
    job = scheduler.get_job(JOB_ID) if scheduler is not None else None
    if job is not None:
        soon = utcnow() + timedelta(seconds=FIRST_RUN_SECONDS)
        if job.next_run_time is None or job.next_run_time > soon:
            job.modify(next_run_time=soon)
