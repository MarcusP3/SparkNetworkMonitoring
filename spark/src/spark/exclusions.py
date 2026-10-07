"""Addresses SPARK's scans never touch (Settings -> Subnets -> Excluded
addresses).

For a box that must not be probed: one that logs every ping or port probe as
an attack, a fragile embedded device, something another team owns. An
excluded address is skipped by the ping sweep (and so by reverse DNS, which
only follows an answer), by the port scan and its control probes, and by
"Find SNMP". What a person set up on purpose still runs: a target, SNMP
polling of a listed device, an API credential. Excluding is about the
automatic scanning, not about things you asked for by name.

An entry is one address ("10.0.0.25") or a start-end range
("10.0.0.100-10.0.0.150"), IPv4 or IPv6, with an optional note. Stored as a
setting: a short list, read once per scan.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_setting, save_setting

KEY = "scan_exclusions"
MAX_ENTRIES = 200
NOTE_LENGTH = 80

Address = ipaddress.IPv4Address | ipaddress.IPv6Address


class ExclusionError(ValueError):
    """Why an entry was refused. The message is for a person."""


def parse(spec: str) -> tuple[Address, Address]:
    """The first and last address an entry covers."""
    text = spec.strip()
    if not text:
        raise ExclusionError("Enter an address, or a range like 10.0.0.100-10.0.0.150.")
    first_text, dash, last_text = text.partition("-")
    try:
        first = ipaddress.ip_address(first_text.strip())
        last = ipaddress.ip_address(last_text.strip()) if dash else first
    except ValueError:
        raise ExclusionError(
            f"“{text[:64]}” is not an address or a range. Use 10.0.0.25 or "
            "10.0.0.100-10.0.0.150.") from None
    if first.version != last.version:
        raise ExclusionError("A range's two ends must both be IPv4 or both IPv6.")
    if last < first:
        raise ExclusionError(f"The range runs backwards: {first} is after {last}.")
    return first, last


def canonical(spec: str) -> str:
    first, last = parse(spec)
    return str(first) if first == last else f"{first}-{last}"


@dataclass(frozen=True)
class Excluded:
    """The excluded ranges, to test addresses against."""

    ranges: tuple[tuple[Address, Address], ...] = ()

    def __contains__(self, address: object) -> bool:
        try:
            ip = ipaddress.ip_address(str(address).strip())
        except ValueError:
            return False
        return any(first.version == ip.version and first <= ip <= last
                   for first, last in self.ranges)

    def __bool__(self) -> bool:
        return bool(self.ranges)

    def keep(self, addresses):  # type: ignore[no-untyped-def]
        """`addresses` without the excluded ones, order kept."""
        return [a for a in addresses if a not in self] if self else list(addresses)


async def entries(session: AsyncSession) -> list[dict]:
    """[{"spec": "10.0.0.25", "note": "..."}], as saved."""
    return list((await get_setting(session, KEY)).get("entries") or [])


async def load(session: AsyncSession) -> Excluded:
    ranges = []
    for entry in await entries(session):
        try:
            ranges.append(parse(entry.get("spec", "")))
        except ExclusionError:
            continue   # saved before a rule tightened: skip, never crash a scan
    return Excluded(tuple(ranges))


async def add(session: AsyncSession, spec: str, note: str = "") -> str:
    spec = canonical(spec)
    current = await entries(session)
    if any(e.get("spec") == spec for e in current):
        raise ExclusionError(f"{spec} is already excluded.")
    if len(current) >= MAX_ENTRIES:
        raise ExclusionError(f"That is {MAX_ENTRIES} entries already; use a range.")
    current.append({"spec": spec, "note": note.strip()[:NOTE_LENGTH]})
    await save_setting(session, KEY, {"entries": current})
    return spec


async def remove(session: AsyncSession, spec: str) -> bool:
    current = await entries(session)
    kept = [e for e in current if e.get("spec") != spec]
    if len(kept) == len(current):
        return False
    await save_setting(session, KEY, {"entries": kept})
    return True
