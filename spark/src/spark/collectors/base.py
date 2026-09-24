"""Collector interface and the normalised shapes every collector returns.

SNMP is the first implementation and the one that matters most, because it's
the only vendor-neutral option. A UniFi collector follows behind this same
interface, because UniFi's own API reports CPU, memory and temperature that
their SNMP agent does not.

The point of normalising here is that everything upstream - storage, alerting,
the UI - never learns which protocol the data came from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class TemperatureReading:
    name: str
    celsius: float


@dataclass
class DeviceHealth:
    """A point-in-time snapshot of one device."""

    reachable: bool = False
    collected_at: datetime = field(default_factory=_utcnow)

    name: str | None = None
    description: str | None = None
    location: str | None = None
    contact: str | None = None
    vendor: str | None = None
    model: str | None = None
    serial: str | None = None

    uptime_seconds: float | None = None

    # None means "this device does not report an instantaneous CPU percentage",
    # not "0%". Kept distinct from the load averages below: a load of 1.4 on a
    # four-core box is not 140% CPU, and showing it as one would be a lie.
    cpu_percent: float | None = None
    load_1min: float | None = None
    load_5min: float | None = None
    load_15min: float | None = None
    memory_percent: float | None = None
    memory_total_bytes: int | None = None
    memory_used_bytes: int | None = None
    temperatures: list[TemperatureReading] = field(default_factory=list)

    # Where each metric came from, so the UI can be honest about it. A device
    # reporting CPU via UCD-SNMP and one reporting it via HOST-RESOURCES are
    # not measuring quite the same thing.
    sources: dict[str, str] = field(default_factory=dict)
    error: str | None = None

    @property
    def max_temperature(self) -> float | None:
        if not self.temperatures:
            return None
        return max(t.celsius for t in self.temperatures)

    @property
    def uptime_human(self) -> str | None:
        if self.uptime_seconds is None:
            return None
        total = int(self.uptime_seconds)
        days, rem = divmod(total, 86400)
        hours, rem = divmod(rem, 3600)
        minutes = rem // 60
        if days:
            return f"{days}d {hours}h {minutes}m"
        if hours:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"


@dataclass
class InterfaceStat:
    index: int
    name: str | None = None
    descr: str | None = None
    alias: str | None = None
    mac: str | None = None
    type: int | None = None
    type_name: str | None = None

    admin_status: str | None = None
    oper_status: str | None = None
    speed_mbps: int | None = None

    in_octets: int | None = None
    out_octets: int | None = None
    in_errors: int | None = None
    out_errors: int | None = None
    counters_are_64bit: bool = False

    @property
    def label(self) -> str:
        """Prefer what a human configured over what the driver reported."""
        return self.alias or self.name or self.descr or f"if{self.index}"

    @property
    def is_up(self) -> bool:
        return self.oper_status == "up"


@dataclass
class CapabilityResult:
    key: str
    label: str
    supported: bool
    sample_count: int = 0
    sample: str | None = None
    notes: str = ""
    error: str | None = None


@dataclass
class ProbeReport:
    """What a device actually supports, as opposed to what its datasheet claims.

    This exists because vendor SNMP support is wildly uneven and frequently
    misdocumented. Rather than guessing, SPARK asks the device and records the
    answer.
    """

    host: str
    reachable: bool = False
    duration_seconds: float = 0.0
    error: str | None = None

    sys_name: str | None = None
    sys_descr: str | None = None
    sys_object_id: str | None = None
    vendor: str | None = None
    uptime_human: str | None = None

    capabilities: list[CapabilityResult] = field(default_factory=list)

    @property
    def supported(self) -> list[CapabilityResult]:
        return [c for c in self.capabilities if c.supported]

    @property
    def unsupported(self) -> list[CapabilityResult]:
        return [c for c in self.capabilities if not c.supported]

    def has(self, key: str) -> bool:
        return any(c.key == key and c.supported for c in self.capabilities)


@runtime_checkable
class Collector(Protocol):
    """What every collector must provide.

    Deliberately small. Anything protocol-specific - community strings, API
    keys, session cookies - is the implementation's own business.
    """

    name: str

    async def probe(self) -> ProbeReport:
        """Ask the device what it supports. Must not raise."""
        ...

    async def collect_health(self) -> DeviceHealth:
        """One health snapshot. Must not raise; set `error` instead."""
        ...

    async def collect_interfaces(self) -> list[InterfaceStat]:
        """Per-interface state and counters. Returns [] if unsupported."""
        ...

    async def close(self) -> None:
        ...


class CollectorError(Exception):
    pass


class Unreachable(CollectorError):
    """Device did not answer at all - timeout, refused, no route."""


class AuthFailed(CollectorError):
    """Device answered but rejected the credentials."""


class CipherUnavailable(CollectorError):
    """SPARK itself cannot encrypt SNMPv3 traffic.

    A fault on this side, not the device's. pysnmp needs the `cryptography`
    package for v3 privacy and quietly disables encryption without it; every
    authPriv request then fails before it leaves the box. It used to surface as
    a timeout, which sends you checking the network, the firewall and the
    credentials -- everything except the actual cause.
    """
