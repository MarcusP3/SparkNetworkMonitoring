"""Which peers are reverse proxies, and what their headers say.

Pure functions shared by the ASGI middleware (`web/hardening.py`), which
applies `X-Forwarded-*` from a trusted proxy to the request, and by `auth.py`,
which decides whether an identity header may be believed. Kept out of both so
neither has to import the other.

The rule is one rule: a proxy is trusted if -- and only if -- its TCP address
is in `auth.proxy.trusted_proxies`. uvicorn's own proxy-header handling is
switched off (`main.py`) because it trusted loopback by default, and with
host networking loopback is every container and process on the VM
(review finding #22).
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable

Networks = tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]


def parse_proxies(entries: Iterable[str] | None) -> Networks:
    """`trusted_proxies` as network objects, once, at startup.

    Bare addresses and CIDRs both work; `config.py` has already refused
    anything that is neither.
    """
    return tuple(ipaddress.ip_network(entry, strict=False) for entry in entries or ())


def is_trusted_proxy(peer: str | None, proxies: Networks) -> bool:
    """Whether a TCP peer address is one of the configured proxies."""
    if not peer or not proxies:
        return False
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return any(address in network for network in proxies)


def forwarded_client(headers: list[tuple[bytes, bytes]]) -> str | None:
    """The address a trusted proxy reports, or None if it reported nothing usable.

    The *last* entry of X-Forwarded-For, not the first. A proxy appends the
    address it saw to whatever the client already sent, so the first entry is
    the client's to write and the last is the proxy's own observation.
    """
    values = [value for key, value in headers if key == b"x-forwarded-for"]
    if not values:
        return None
    last = b",".join(values).decode("latin-1").split(",")[-1].strip()
    try:
        return str(ipaddress.ip_address(last))
    except ValueError:
        return None


def forwarded_scheme(headers: list[tuple[bytes, bytes]]) -> str | None:
    """`http` or `https` from X-Forwarded-Proto, or None for anything else."""
    for key, value in headers:
        if key == b"x-forwarded-proto":
            scheme = value.decode("latin-1").strip().lower()
            return scheme if scheme in ("http", "https") else None
    return None
