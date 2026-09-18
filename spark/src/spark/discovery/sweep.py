"""Finding what is on the network.

Three steps, in this order and for this reason:

  1. ICMP sweep every address in the subnet. Besides telling us who answers,
     it populates the kernel's ARP cache as a side effect.
  2. Read the ARP table. This is where MAC addresses come from, and it only
     works for directly-attached segments -- ARP does not cross a router.
  3. Reverse DNS for whatever answered, for a human-readable name.

The MAC is the prize. Device identity keys on it because DHCP reassigns
addresses, and a tool keyed on IP loses a device's history the first time a
lease churns. On a routed VLAN we get no MAC and fall back to IP identity,
which is why `attached` in the config is not a cosmetic flag.

Nothing here writes to the database; it returns observations and `store.py`
decides what they mean. That split is what makes the identity rules testable
without a network.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .oui import is_locally_administered, lookup, normalise

log = logging.getLogger(__name__)

ARP_TABLE = Path("/proc/net/arp")

# A /22 is 1022 addresses and already a slow sweep. Anything larger is almost
# certainly a misconfiguration, and silently scanning 65k addresses would be a
# worse answer than refusing.
MAX_HOSTS_PER_SUBNET = 1024

# Enough parallelism to sweep a /24 in a couple of seconds without flooding a
# small switch's ARP handling.
CONCURRENCY = 64


@dataclass
class Observation:
    """One address that answered, and whatever we could learn about it."""

    ip: str
    mac: str | None = None
    hostname: str | None = None
    vendor: str | None = None
    subnet: str | None = None
    randomised_mac: bool = False
    seen_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def read_arp_table(path: Path = ARP_TABLE) -> dict[str, str]:
    """IP to MAC from the kernel's ARP cache.

    /proc/net/arp rather than shelling out to `ip neigh`: no subprocess, no
    dependency on iproute2 being in the image, and a stable format. IPv4 only,
    which matches what the sweep covers.

    Flags 0x2 means the entry is complete. Incomplete entries have a MAC of
    00:00:00:00:00:00 and mean "we asked and nobody answered" -- recording that
    as a device's address would be actively wrong.
    """
    table: dict[str, str] = {}
    try:
        lines = path.read_text().splitlines()
    except OSError as exc:
        log.debug("Cannot read %s: %s", path, exc)
        return table

    for line in lines[1:]:  # first line is a header
        fields = line.split()
        if len(fields) < 4:
            continue
        ip, _hw_type, flags, mac = fields[0], fields[1], fields[2], fields[3]
        try:
            complete = int(flags, 16) & 0x2
        except ValueError:
            continue
        if not complete:
            continue
        normalised = normalise(mac)
        if normalised and normalised != "00:00:00:00:00:00":
            table[ip] = normalised
    return table


def hosts_in(cidr: str) -> list[str]:
    """Addresses worth probing, network and broadcast excluded."""
    network = ipaddress.ip_network(cidr, strict=False)
    if network.num_addresses > MAX_HOSTS_PER_SUBNET + 2:
        log.warning(
            "Subnet %s has %d addresses, more than the %d cap; skipping it. "
            "Split it into smaller ranges if you really want it swept.",
            cidr, network.num_addresses, MAX_HOSTS_PER_SUBNET,
        )
        return []
    return [str(host) for host in network.hosts()]


async def ping_sweep(addresses: list[str], timeout: float = 1.0) -> list[str]:
    """Which of these answer ICMP. Never raises."""
    if not addresses:
        return []
    try:
        from icmplib import async_multiping
    except ImportError:  # pragma: no cover - dependency is declared
        log.error("icmplib is not installed; cannot sweep")
        return []

    for privileged in (True, False):
        try:
            hosts = await async_multiping(
                addresses,
                count=1,
                timeout=timeout,
                privileged=privileged,
                concurrent_tasks=CONCURRENCY,
            )
            return [host.address for host in hosts if host.is_alive]
        except Exception as exc:  # noqa: BLE001
            log.debug("multiping (privileged=%s) failed: %s", privileged, exc)
    log.warning(
        "ICMP sweep failed both privileged and unprivileged. The container "
        "needs cap_add: [NET_RAW], or the host needs net.ipv4.ping_group_range."
    )
    return []


async def reverse_dns(ip: str, timeout: float = 1.0) -> str | None:
    """PTR lookup, best effort.

    Most addresses have no PTR record and the lookup simply times out, so this
    is bounded tightly: a sweep must not take a minute because a resolver is
    slow about saying "no".
    """
    loop = asyncio.get_running_loop()
    try:
        name, _ = await asyncio.wait_for(
            loop.getnameinfo((ip, 0), socket.NI_NAMEREQD), timeout=timeout
        )
    except (asyncio.TimeoutError, socket.gaierror, OSError):
        return None
    except Exception:  # noqa: BLE001
        return None
    return name if name and name != ip else None


async def sweep_subnet(
    cidr: str,
    name: str | None = None,
    attached: bool = True,
    timeout: float = 1.0,
    resolve_names: bool = True,
) -> list[Observation]:
    """Sweep one subnet and return what answered."""
    addresses = hosts_in(cidr)
    if not addresses:
        return []

    alive = await ping_sweep(addresses, timeout=timeout)
    if not alive:
        return []

    # Read ARP *after* the sweep: the pings are what populated it.
    arp = read_arp_table() if attached else {}
    if attached and not arp:
        log.info(
            "Subnet %s is marked attached but the ARP table is empty. Devices "
            "there will fall back to IP identity.", name or cidr,
        )

    names: dict[str, str | None] = {}
    if resolve_names:
        resolved = await asyncio.gather(
            *(reverse_dns(ip) for ip in alive), return_exceptions=True
        )
        names = {
            ip: (value if isinstance(value, str) else None)
            for ip, value in zip(alive, resolved)
        }

    observations = []
    for ip in alive:
        mac = arp.get(ip)
        observations.append(
            Observation(
                ip=ip,
                mac=mac,
                hostname=names.get(ip),
                vendor=lookup(mac),
                subnet=name or cidr,
                randomised_mac=is_locally_administered(mac),
            )
        )
    return observations


async def sweep_all(subnets) -> list[Observation]:
    """Sweep every enabled subnet in the config, one after another.

    Sequential on purpose: these run on the same NIC, and overlapping sweeps
    mostly compete with each other for the same ARP table.
    """
    results: list[Observation] = []
    for subnet in subnets:
        if not getattr(subnet, "enabled", True):
            continue
        found = await sweep_subnet(
            subnet.cidr, name=subnet.label, attached=subnet.attached
        )
        log.info("Swept %s: %d host(s) answered", subnet.label, len(found))
        results.extend(found)
    return results
