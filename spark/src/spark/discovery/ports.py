"""Finding what is listening on the devices discovery already found.

A TCP connect scan, not a SYN scan: SPARK is a container, connect() needs no
privileges beyond the ones it already has, and the difference only matters to
someone trying not to be logged. SPARK is not trying not to be logged.

Two limits shape everything here, and both are about the same thing -- a scan
that is slow is a scan that gets switched off:

  * **Only devices already discovered are scanned.** Never a whole subnet. The
    sweep decides who exists; this decides what they are running. Scanning 254
    addresses on the chance something answers is how you turn a two-minute job
    into an hour.

  * **A curated port list, not a range.** An open port refuses immediately and
    a closed one refuses immediately; a *filtered* port -- a firewall dropping
    rather than rejecting -- costs the full timeout, every time.

    What that costs is bounded by the two semaphores below, and the arithmetic
    is worth doing rather than assuming serial probing -- an earlier version of
    this comment did assume it and overstated the cost roughly tenfold. One
    host takes ceil(ports / PER_HOST_CONCURRENCY) timeouts, and the scan as a
    whole cannot exceed ceil(ports * hosts / TOTAL_CONCURRENCY); whichever is
    larger wins. Measured against black-holed addresses: 67 hosts x 45 ports is
    12 seconds, and the same hosts at 85 ports is 23.

    A range is still the thing to avoid -- 1024 ports is 86 seconds on one
    filtered host, and far more across a network -- but the curated list is
    cheap enough that a person can add to it. `port_catalogue.py` is what
    decides the list at scan time; the dict below is only its default.

Nothing here writes to the database or reads it; it takes addresses and returns
observations, the same split that makes `sweep.py` testable without a network.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Per-host, so one slow device cannot monopolise the scan, and overall, so a
# hundred devices do not open six thousand sockets at once.
PER_HOST_CONCURRENCY = 12
TOTAL_CONCURRENCY = 256

# Short on purpose. A service that has not completed a TCP handshake in this
# long on a LAN is not a service you can usefully monitor, and every filtered
# port pays this in full.
CONNECT_TIMEOUT = 1.0

# The catalogue. Chosen for what homelabs run rather than for completeness --
# completeness is what a port range is for, and a range is what makes this
# unusably slow. Names are what the Devices page shows, so they are the name a
# person would use, not the IANA registration.
WELL_KNOWN: dict[int, str] = {
    21: "ftp",
    22: "ssh",
    23: "telnet",
    25: "smtp",
    53: "dns",
    80: "http",
    110: "pop3",
    111: "rpcbind",
    135: "msrpc",
    139: "netbios",
    143: "imap",
    389: "ldap",
    443: "https",
    445: "smb",
    465: "smtps",
    515: "printer",
    548: "afp",
    587: "smtp submission",
    631: "ipp",
    993: "imaps",
    995: "pop3s",
    1433: "mssql",
    1883: "mqtt",
    2049: "nfs",
    2375: "docker (plain)",
    2376: "docker (tls)",
    3000: "grafana",
    3306: "mysql",
    3389: "rdp",
    5000: "synology dsm",
    5432: "postgres",
    5601: "kibana",
    6379: "redis",
    8006: "proxmox",
    8080: "http-alt",
    8096: "jellyfin",
    8123: "home assistant",
    8443: "https-alt",
    9000: "portainer",
    9090: "prometheus",
    9100: "printer / node-exporter",
    9200: "elasticsearch",
    9443: "portainer (https)",
    27017: "mongodb",
    32400: "plex",
}

# Ports whose presence is worth saying out loud. Not a judgement on running
# them -- plenty of homelabs have a reason -- but each one is either
# unencrypted by design or a well-known way to own a host, and an inventory
# that notices is more useful than one that lists them like any other row.
NOTEWORTHY: dict[int, str] = {
    23: "telnet sends credentials in clear text",
    21: "ftp sends credentials in clear text",
    2375: "an unauthenticated Docker socket is root on this host",
    3389: "RDP exposed on the network is a common entry point",
    445: "SMB has a long history of wormable flaws",
    5432: "a database reachable from the LAN is worth checking is intentional",
    3306: "a database reachable from the LAN is worth checking is intentional",
    6379: "Redis defaults to no authentication",
    27017: "MongoDB has shipped with no authentication by default before",
    9200: "Elasticsearch defaults to no authentication",
}


def port_name(port: int) -> str | None:
    return WELL_KNOWN.get(port)


def concern(port: int) -> str | None:
    """Why this port might be worth a second look, or None."""
    return NOTEWORTHY.get(port)


@dataclass
class OpenPort:
    port: int
    name: str | None = None
    banner: str | None = None


@dataclass
class HostScan:
    """What one device was found to be running."""

    address: str
    device_id: int | None = None
    probed: int = 0
    open_ports: list[OpenPort] = field(default_factory=list)
    error: str | None = None
    # Which ports this scan actually looked at. Not the same question as
    # `probed`, which is a count for the summary line -- this is what lets the
    # recorder tell "checked and gone" apart from "not checked this time".
    #
    # It matters now that the port list is editable. Switching off SMB used to
    # mean the next scan closed every SMB service on the network, because the
    # recorder read "not in this scan's results" as "no longer listening". An
    # empty set therefore means nothing was covered, and closes nothing.
    covered: frozenset[int] = field(default_factory=frozenset)


async def probe(address: str, port: int, timeout: float = CONNECT_TIMEOUT) -> bool:
    """Is something accepting connections here.

    Never raises. Every failure mode -- refused, filtered, no route, a host
    that vanished between the sweep and now -- is the same answer to the only
    question being asked, and distinguishing them would only give the caller
    something it has no use for.
    """
    writer = None
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(address, port), timeout=timeout
        )
        return True
    except (asyncio.TimeoutError, OSError):
        return False
    except Exception:  # noqa: BLE001 - a scan must never take the job down
        log.debug("Unexpected error probing %s:%s", address, port, exc_info=True)
        return False
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001 - already got the answer
                pass


async def scan_host(
    address: str,
    ports: list[int] | None = None,
    *,
    device_id: int | None = None,
    timeout: float = CONNECT_TIMEOUT,
    limit: asyncio.Semaphore | None = None,
    names: dict[int, str | None] | None = None,
) -> HostScan:
    """Scan one device. Returns what answered, in port order.

    `names` is the caller's catalogue, so a port the person added and named
    themselves is recorded under that name rather than as a bare number.
    """
    ports = sorted(ports if ports is not None else WELL_KNOWN)
    result = HostScan(address=address, device_id=device_id)
    if not address or not ports:
        # probed stays 0 and covered stays empty: nothing was contacted, and a
        # summary claiming otherwise would overstate what the scan covered --
        # while a `covered` that lied would make the recorder close services it
        # never looked at.
        return result
    result.probed = len(ports)
    result.covered = frozenset(ports)

    host_limit = asyncio.Semaphore(PER_HOST_CONCURRENCY)

    async def one(port: int) -> int | None:
        async with host_limit:
            if limit is not None:
                async with limit:
                    found = await probe(address, port, timeout)
            else:
                found = await probe(address, port, timeout)
        return port if found else None

    found = await asyncio.gather(*(one(port) for port in ports))
    catalogue = WELL_KNOWN if names is None else names
    result.open_ports = [
        OpenPort(port=port, name=catalogue.get(port) or port_name(port))
        for port in found
        if port is not None
    ]
    return result


async def scan_hosts(
    targets: list[tuple[int, str]],
    ports: list[int] | None = None,
    *,
    timeout: float = CONNECT_TIMEOUT,
    names: dict[int, str | None] | None = None,
) -> list[HostScan]:
    """Scan several devices concurrently. `targets` is (device_id, address).

    Concurrent across hosts rather than sequential like the ICMP sweep: that
    one is serialised because overlapping sweeps fight over the same ARP table,
    and this has no such shared resource.
    """
    if not targets:
        return []
    limit = asyncio.Semaphore(TOTAL_CONCURRENCY)
    return list(
        await asyncio.gather(
            *(
                scan_host(address, ports, device_id=device_id,
                          timeout=timeout, limit=limit, names=names)
                for device_id, address in targets
            )
        )
    )


# --------------------------------------------------------------------------
# Detecting a network that answers on behalf of its hosts
# --------------------------------------------------------------------------

# Firewalls commonly intercept a port to every destination -- 53 to force DNS
# through the local resolver, 80 for a captive portal. A TCP connect scan
# cannot tell that apart from a real service: the handshake genuinely
# completes. From the scanner's seat every device is running DNS.
#
# The tell is that it works on addresses where nothing exists. So before
# scanning, probe a few addresses discovery has never found anything at. A port
# that answers on a host that is not there is not a service, it is the network
# talking, and reporting it would put a row in the inventory for something that
# does not exist.

# At least this many controls must agree. One is not enough: a single "free"
# address might be a host that appeared since the last sweep, and one live
# machine would then suppress every port it runs across the whole network.
MIN_CONTROLS = 2

# Controls are addresses with nothing at them, so most probes run to the full
# timeout. Shorter than a real scan to bound what this costs.
CONTROL_TIMEOUT = 0.6


def pick_control_addresses(
    candidates: list[str], known: set[str], count: int = 3
) -> list[str]:
    """Addresses in a subnet that discovery has never seen anything at.

    Spread across the range rather than taken from one end: the bottom of a
    subnet is where the infrastructure lives and the top is where a DHCP pool
    often ends, so a cluster at either end is likelier to hit something real.
    """
    free = [ip for ip in candidates if ip not in known]
    if len(free) < MIN_CONTROLS:
        return []
    count = min(count, len(free))
    step = max(1, len(free) // count)
    return [free[i * step] for i in range(count)]


async def find_interception(
    controls: list[str],
    ports: list[int] | None = None,
    *,
    timeout: float = CONTROL_TIMEOUT,
) -> set[int]:
    """Ports that answer on addresses where nothing exists.

    Unanimity is the rule: a port has to answer on *every* control before it is
    called intercepted. Anything less and one live machine that slipped into
    the control set would suppress the ports it runs for the entire network --
    hiding real services, which is a worse failure than showing false ones.
    """
    if len(controls) < MIN_CONTROLS:
        return set()

    ports = sorted(ports if ports is not None else WELL_KNOWN)
    limit = asyncio.Semaphore(TOTAL_CONCURRENCY)
    scans = await asyncio.gather(
        *(scan_host(address, ports, timeout=timeout, limit=limit) for address in controls)
    )

    answered = [set(p.port for p in scan.open_ports) for scan in scans]
    intercepted = set.intersection(*answered) if answered else set()
    if intercepted:
        log.warning(
            "Ports %s answer on addresses with no host on them; something on "
            "this network is responding for every address. Excluding them from "
            "the scan.",
            ", ".join(f"{p} ({port_name(p) or 'unknown'})" for p in sorted(intercepted)),
        )
    return intercepted
