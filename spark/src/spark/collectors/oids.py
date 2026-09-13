"""OID catalogue.

Numeric OIDs only, deliberately. Compiling MIB files means shipping a MIB
compiler, a MIB search path, and a support burden every time a vendor ships a
file that doesn't parse. Numbers are stable and universal; the human-readable
names live here as data instead.

Grouped by capability rather than by MIB, because the question SPARK actually
asks a device is "can you tell me your temperature", not "do you implement
ENTITY-SENSOR-MIB".
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Scalars (SNMPv2-MIB system group) - RFC 3418
# --------------------------------------------------------------------------

SYS_DESCR = "1.3.6.1.2.1.1.1.0"
SYS_OBJECT_ID = "1.3.6.1.2.1.1.2.0"
SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
SYS_CONTACT = "1.3.6.1.2.1.1.4.0"
SYS_NAME = "1.3.6.1.2.1.1.5.0"
SYS_LOCATION = "1.3.6.1.2.1.1.6.0"
SYS_SERVICES = "1.3.6.1.2.1.1.7.0"

SYSTEM_SCALARS = {
    SYS_DESCR: "sysDescr",
    SYS_OBJECT_ID: "sysObjectID",
    SYS_UPTIME: "sysUpTime",
    SYS_CONTACT: "sysContact",
    SYS_NAME: "sysName",
    SYS_LOCATION: "sysLocation",
}

# --------------------------------------------------------------------------
# Interfaces - IF-MIB (RFC 2863)
# --------------------------------------------------------------------------

IF_DESCR = "1.3.6.1.2.1.2.2.1.2"
IF_TYPE = "1.3.6.1.2.1.2.2.1.3"
IF_MTU = "1.3.6.1.2.1.2.2.1.4"
IF_SPEED = "1.3.6.1.2.1.2.2.1.5"
IF_PHYS_ADDRESS = "1.3.6.1.2.1.2.2.1.6"
IF_ADMIN_STATUS = "1.3.6.1.2.1.2.2.1.7"
IF_OPER_STATUS = "1.3.6.1.2.1.2.2.1.8"
IF_LAST_CHANGE = "1.3.6.1.2.1.2.2.1.9"
IF_IN_OCTETS = "1.3.6.1.2.1.2.2.1.10"
IF_IN_ERRORS = "1.3.6.1.2.1.2.2.1.14"
IF_OUT_OCTETS = "1.3.6.1.2.1.2.2.1.16"
IF_OUT_ERRORS = "1.3.6.1.2.1.2.2.1.20"

# ifXTable - the 64-bit counters and better names. On anything faster than
# 100 Mbps the 32-bit counters in ifTable wrap too fast to trust between polls.
IF_NAME = "1.3.6.1.2.1.31.1.1.1.1"
IF_HC_IN_OCTETS = "1.3.6.1.2.1.31.1.1.1.6"
IF_HC_OUT_OCTETS = "1.3.6.1.2.1.31.1.1.1.10"
IF_HIGH_SPEED = "1.3.6.1.2.1.31.1.1.1.15"
IF_ALIAS = "1.3.6.1.2.1.31.1.1.1.18"

IF_OPER_STATUS_NAMES = {
    1: "up",
    2: "down",
    3: "testing",
    4: "unknown",
    5: "dormant",
    6: "notPresent",
    7: "lowerLayerDown",
}

IF_ADMIN_STATUS_NAMES = {1: "up", 2: "down", 3: "testing"}

# ifType values worth naming; everything else renders as its number.
IF_TYPE_NAMES = {
    1: "other",
    6: "ethernet",
    24: "loopback",
    53: "propVirtual",
    117: "gigabitEthernet",
    131: "tunnel",
    135: "l2vlan",
    136: "l3ipvlan",
    161: "ieee8023adLag",
}

# Interfaces that are almost never worth graphing.
BORING_IF_TYPES = {24, 53, 131}

# --------------------------------------------------------------------------
# CPU and memory
#
# There is no single standard here, which is the whole problem. Devices
# implement one of these, some, or none, so SPARK tries them in order and takes
# the first that answers.
# --------------------------------------------------------------------------

# HOST-RESOURCES-MIB (RFC 2790) - percent load per processor
HR_PROCESSOR_LOAD = "1.3.6.1.2.1.25.3.3.1.2"
HR_STORAGE_DESCR = "1.3.6.1.2.1.25.2.3.1.3"
HR_STORAGE_UNITS = "1.3.6.1.2.1.25.2.3.1.4"
HR_STORAGE_SIZE = "1.3.6.1.2.1.25.2.3.1.5"
HR_STORAGE_USED = "1.3.6.1.2.1.25.2.3.1.6"

# UCD-SNMP-MIB - net-snmp on Linux, which covers a lot of appliances
#
# ssCpuIdle/ssCpuUser/ssCpuSystem are the one-minute *average* scalars, and
# modern net-snmp does not serve them - verified against net-snmp: both
# 2021.11.11.0 and 2021.11.9.0 return nothing while the raw counters answer.
# Anything derived from them on pfSense, OPNsense or a Linux appliance is
# silently always null, so they are listed only to document the dead end.
UCD_CPU_IDLE_DEPRECATED = "1.3.6.1.4.1.2021.11.11.0"
UCD_CPU_USER_DEPRECATED = "1.3.6.1.4.1.2021.11.9.0"

# The raw counters that replaced them. These are cumulative ticks, so a
# percentage needs two samples and a delta of idle against the total - which
# needs somewhere to keep the previous sample. That belongs with the scheduler,
# so it is deliberately not done here yet.
UCD_CPU_RAW_USER = "1.3.6.1.4.1.2021.11.50.0"
UCD_CPU_RAW_NICE = "1.3.6.1.4.1.2021.11.51.0"
UCD_CPU_RAW_SYSTEM = "1.3.6.1.4.1.2021.11.52.0"
UCD_CPU_RAW_IDLE = "1.3.6.1.4.1.2021.11.53.0"

# laLoad - load average, as a DisplayString like "0.18". Not a CPU percentage
# and not presented as one, but it is stateless, it answers on every net-snmp
# device, and it is what a human actually reads.
UCD_LOAD_1MIN = "1.3.6.1.4.1.2021.10.1.3.1"
UCD_LOAD_5MIN = "1.3.6.1.4.1.2021.10.1.3.2"
UCD_LOAD_15MIN = "1.3.6.1.4.1.2021.10.1.3.3"

UCD_MEM_TOTAL_REAL = "1.3.6.1.4.1.2021.4.5.0"
UCD_MEM_AVAIL_REAL = "1.3.6.1.4.1.2021.4.6.0"

# --------------------------------------------------------------------------
# Temperature - ENTITY-SENSOR-MIB (RFC 3433)
# --------------------------------------------------------------------------

ENT_SENSOR_TYPE = "1.3.6.1.2.1.99.1.1.1.1"
ENT_SENSOR_SCALE = "1.3.6.1.2.1.99.1.1.1.2"
ENT_SENSOR_PRECISION = "1.3.6.1.2.1.99.1.1.1.3"
ENT_SENSOR_VALUE = "1.3.6.1.2.1.99.1.1.1.4"
ENT_SENSOR_STATUS = "1.3.6.1.2.1.99.1.1.1.5"

SENSOR_TYPE_CELSIUS = 8
SENSOR_STATUS_OK = 1

# entPhySensorScale is an exponent enum, not a multiplier.
SENSOR_SCALE_EXPONENTS = {
    1: -24, 2: -21, 3: -18, 4: -15, 5: -12, 6: -9, 7: -6, 8: -3,
    9: 0, 10: 3, 11: 6, 12: 9, 13: 12, 14: 15, 15: 18, 16: 21, 17: 24,
}

# ENTITY-MIB (RFC 4133) - hardware inventory, for model and serial
ENT_PHYSICAL_DESCR = "1.3.6.1.2.1.47.1.1.1.1.2"
ENT_PHYSICAL_CLASS = "1.3.6.1.2.1.47.1.1.1.1.5"
ENT_PHYSICAL_NAME = "1.3.6.1.2.1.47.1.1.1.1.7"
ENT_PHYSICAL_SERIAL = "1.3.6.1.2.1.47.1.1.1.1.11"
ENT_PHYSICAL_MODEL = "1.3.6.1.2.1.47.1.1.1.1.13"

ENT_CLASS_CHASSIS = 3

# --------------------------------------------------------------------------
# Topology - collected here, used when the map is built
# --------------------------------------------------------------------------

# BRIDGE-MIB - which MAC is on which bridge port
DOT1D_TP_FDB_PORT = "1.3.6.1.2.1.17.4.3.1.2"
DOT1D_BASE_PORT_IFINDEX = "1.3.6.1.2.1.17.1.4.1.2"
# Q-BRIDGE-MIB - the VLAN-aware version most modern switches use instead
DOT1Q_TP_FDB_PORT = "1.3.6.1.2.1.17.7.1.2.2.1.2"

# LLDP-MIB - neighbours, which give switch-to-switch links directly
LLDP_REM_SYS_NAME = "1.0.8802.1.1.2.1.4.1.1.9"
LLDP_REM_PORT_ID = "1.0.8802.1.1.2.1.4.1.1.7"
LLDP_REM_PORT_DESC = "1.0.8802.1.1.2.1.4.1.1.8"
LLDP_REM_CHASSIS_ID = "1.0.8802.1.1.2.1.4.1.1.5"

# IP-MIB - the ARP table. On a router this is the fix for routed VLANs:
# it returns MAC-to-IP for every segment the device routes, which SPARK
# cannot learn on its own from across a router boundary.
IP_NET_TO_MEDIA_PHYS = "1.3.6.1.2.1.4.22.1.2"
IP_NET_TO_PHYSICAL_PHYS = "1.3.6.1.2.1.4.35.1.4"

# --------------------------------------------------------------------------
# Vendor identification by sysObjectID prefix
# --------------------------------------------------------------------------

ENTERPRISE_PREFIXES: dict[str, str] = {
    "1.3.6.1.4.1.41112": "Ubiquiti (UniFi)",
    "1.3.6.1.4.1.10002": "Ubiquiti (AirOS)",
    "1.3.6.1.4.1.4413": "Broadcom / EdgeSwitch",
    "1.3.6.1.4.1.9": "Cisco",
    "1.3.6.1.4.1.14988": "MikroTik",
    "1.3.6.1.4.1.11": "HP / Aruba",
    "1.3.6.1.4.1.47196": "Aruba (HPE)",
    "1.3.6.1.4.1.4526": "Netgear",
    "1.3.6.1.4.1.171": "D-Link",
    "1.3.6.1.4.1.11863": "TP-Link",
    "1.3.6.1.4.1.8072": "Net-SNMP (Linux)",
    "1.3.6.1.4.1.2604": "Sophos",
    "1.3.6.1.4.1.12325": "pfSense / FreeBSD",
    "1.3.6.1.4.1.25623": "OPNsense",
    "1.3.6.1.4.1.2011": "Huawei",
    "1.3.6.1.4.1.6527": "Nokia",
    "1.3.6.1.4.1.30065": "Arista",
    "1.3.6.1.4.1.2636": "Juniper",
    "1.3.6.1.4.1.5951": "Netscaler",
    "1.3.6.1.4.1.674": "Dell",
    "1.3.6.1.4.1.1916": "Extreme Networks",
}


def identify_vendor(sys_object_id: str | None) -> str | None:
    """Map a sysObjectID to a vendor by longest matching enterprise prefix."""
    if not sys_object_id:
        return None
    oid = sys_object_id.lstrip(".")
    best: tuple[int, str] | None = None
    for prefix, vendor in ENTERPRISE_PREFIXES.items():
        if oid.startswith(prefix + ".") or oid == prefix:
            if best is None or len(prefix) > best[0]:
                best = (len(prefix), vendor)
    return best[1] if best else None


@dataclass(frozen=True)
class CapabilityProbe:
    """One thing SPARK wants to know, and where to look for it."""

    key: str
    label: str
    oids: tuple[str, ...]
    walk: bool = True
    notes: str = ""


# Ordered roughly by how much a homelab user cares.
CAPABILITY_PROBES: list[CapabilityProbe] = [
    CapabilityProbe("system", "System info (name, uptime, model)",
                    (SYS_DESCR, SYS_UPTIME, SYS_NAME), walk=False),
    CapabilityProbe("interfaces", "Interface table (ports, status, speed)",
                    (IF_DESCR,)),
    CapabilityProbe("if_counters_64", "64-bit interface counters",
                    (IF_HC_IN_OCTETS,),
                    notes="Needed for accurate throughput above 100 Mbps"),
    CapabilityProbe("if_counters_32", "32-bit interface counters",
                    (IF_IN_OCTETS,),
                    notes="Wraps quickly on fast links; fallback only"),
    CapabilityProbe("if_names", "Interface names and descriptions (ifXTable)",
                    (IF_NAME, IF_ALIAS)),
    CapabilityProbe("cpu_hr", "CPU load (HOST-RESOURCES-MIB)",
                    (HR_PROCESSOR_LOAD,)),
    CapabilityProbe("cpu_ucd_raw", "CPU counters (UCD-SNMP-MIB, raw ticks)",
                    (UCD_CPU_RAW_IDLE,), walk=False,
                    notes="Needs two samples to become a percentage"),
    CapabilityProbe("load_average", "Load average (UCD-SNMP-MIB)",
                    (UCD_LOAD_1MIN,), walk=False),
    CapabilityProbe("memory_hr", "Memory (HOST-RESOURCES-MIB)",
                    (HR_STORAGE_DESCR,)),
    CapabilityProbe("memory_ucd", "Memory (UCD-SNMP-MIB)",
                    (UCD_MEM_TOTAL_REAL,), walk=False),
    CapabilityProbe("temperature", "Temperature sensors (ENTITY-SENSOR-MIB)",
                    (ENT_SENSOR_VALUE,),
                    notes="Frequently absent on consumer and prosumer switches"),
    CapabilityProbe("entity", "Hardware inventory (model, serial)",
                    (ENT_PHYSICAL_MODEL,)),
    CapabilityProbe("lldp", "LLDP neighbours",
                    (LLDP_REM_SYS_NAME,),
                    notes="Gives switch-to-switch links for the topology map"),
    CapabilityProbe("bridge_fdb", "MAC address table (BRIDGE-MIB)",
                    (DOT1D_TP_FDB_PORT,),
                    notes="Maps each MAC to a switch port"),
    CapabilityProbe("qbridge_fdb", "MAC address table, VLAN-aware (Q-BRIDGE-MIB)",
                    (DOT1Q_TP_FDB_PORT,)),
    CapabilityProbe("arp", "ARP table",
                    (IP_NET_TO_MEDIA_PHYS,),
                    notes="On a router this supplies MAC-to-IP for every VLAN"),
]
