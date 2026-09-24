"""Device collectors.

SNMP is the vendor-neutral baseline. Vendor-specific collectors (UniFi first,
since its API reports CPU, memory and temperature that its SNMP agent doesn't)
implement the same interface, so nothing upstream needs to know the difference.
"""

from .base import (
    AuthFailed,
    CapabilityResult,
    CipherUnavailable,
    Collector,
    CollectorError,
    DeviceHealth,
    InterfaceStat,
    ProbeReport,
    TemperatureReading,
    Unreachable,
)
from .snmp import SnmpCollector, SnmpCredential, probe_host

__all__ = [
    "AuthFailed",
    "CapabilityResult",
    "CipherUnavailable",
    "Collector",
    "CollectorError",
    "DeviceHealth",
    "InterfaceStat",
    "ProbeReport",
    "SnmpCollector",
    "SnmpCredential",
    "TemperatureReading",
    "Unreachable",
    "probe_host",
]
