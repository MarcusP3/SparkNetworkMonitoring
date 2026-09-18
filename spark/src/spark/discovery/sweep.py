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
class SubnetResult:
    """What one subnet's sweep actually did.

    Kept even when it found nothing, because "swept 254 addresses and none
    answered" and "never swept it" are different problems with different fixes,
    and an empty device list cannot tell them apart on its own.
    """

    name: str
    cidr: str
    attached: bool
    probed: int = 0
    answered: int = 0
    with_mac: int = 0
    skipped: str | None = None
    observations: list["Observation"] = field(default_factory=list)


@dataclass
class SweepReport:
    """The outcome of a whole sweep, for the Devices page to show."""

    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    subnets: list[SubnetResult] = field(default_factory=list)
    icmp_available: bool = True
    error: str | None = None

    @property
    def observations(self) -> list["Observation"]:
        return [o for subnet in self.subnets for o in subnet.observations]

    @property
    def probed(self) -> int:
        return sum(s.probed for s in self.subnets)

    @property
    def answered(self) -> int:
        return sum(s.answered for s in self.subnets)

    @property
    def with_mac(self) -> int:
        return sum(s.with_mac for s in self.subnets)

    def as_dict(self) -> dict:
        """A JSON-safe summary, for storage and for the page."""
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "probed": self.probed,
            "answered": self.answered,
            "with_mac": self.with_mac,
            "icmp_available": self.icmp_available,
            "error": self.error,
            "subnets": [
                {
                    "name": s.name,
                    "cidr": s.cidr,
                    "attached": s.attached,
                    "probed": s.probed,
                    "answered": s.answered,
                    "with_mac": s.with_mac,
                    "skipped": s.skipped,
                }
                for s in self.subnets
            ],
        }


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


async def ping_sweep(
    addresses: list[str], timeout: float = 1.0
) -> tuple[list[str], bool]:
    """Which of these answer ICMP, and whether ICMP worked at all.

    The second value is the distinction that matters for diagnosis: an empty
    list because nothing replied is a network answer, and an empty list because
    the socket could not be opened is a configuration answer. Collapsing them
    into "found nothing" is what sends you to read logs.
    """
    if not addresses:
        return [], True
    try:
        from icmplib import async_multiping
    except ImportError:  # pragma: no cover - dependency is declared
        log.error("icmplib is not installed; cannot sweep")
        return [], False

    for privileged in (True, False):
        try:
            hosts = await async_multiping(
                addresses,
                count=1,
                timeout=timeout,
                privileged=privileged,
                concurrent_tasks=CONCURRENCY,
            )
            return [host.address for host in hosts if host.is_alive], True
        except Exception as exc:  # noqa: BLE001
            log.debug("multiping (privileged=%s) failed: %s", privileged, exc)
    log.warning(
        "ICMP sweep failed both privileged and unprivileged. The container "
        "needs cap_add: [NET_RAW], or the host needs net.ipv4.ping_group_range."
    )
    return [], False


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
) -> SubnetResult:
    """Sweep one subnet and report what happened."""
    result = SubnetResult(name=name or cidr, cidr=cidr, attached=attached)

    addresses = hosts_in(cidr)
    if not addresses:
        result.skipped = (
            f"too large: more than {MAX_HOSTS_PER_SUBNET} addresses. "
            "Split it into smaller ranges."
        )
        return result
    result.probed = len(addresses)

    alive, icmp_ok = await ping_sweep(addresses, timeout=timeout)
    if not icmp_ok:
        result.skipped = "ICMP unavailable"
        return result
    result.answered = len(alive)
    if not alive:
        return result

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

    for ip in alive:
        mac = arp.get(ip)
        if mac:
            result.with_mac += 1
        result.observations.append(
            Observation(
                ip=ip,
                mac=mac,
                hostname=names.get(ip),
                vendor=lookup(mac),
                subnet=name or cidr,
                randomised_mac=is_locally_administered(mac),
            )
        )
    return result


async def sweep_all(subnets) -> SweepReport:
    """Sweep every enabled subnet in the config, one after another.

    Sequential on purpose: these run on the same NIC, and overlapping sweeps
    mostly compete with each other for the same ARP table.
    """
    report = SweepReport()
    for subnet in subnets:
        if not getattr(subnet, "enabled", True):
            continue
        result = await sweep_subnet(
            subnet.cidr, name=subnet.label, attached=subnet.attached
        )
        report.subnets.append(result)
        if result.skipped:
            log.warning("Skipped %s: %s", result.name, result.skipped)
        else:
            log.info(
                "Swept %s: %d of %d answered, %d with a MAC",
                result.name, result.answered, result.probed, result.with_mac,
            )
    report.icmp_available = not any(
        s.skipped == "ICMP unavailable" for s in report.subnets
    )
    report.finished_at = datetime.now(timezone.utc)
    return report
