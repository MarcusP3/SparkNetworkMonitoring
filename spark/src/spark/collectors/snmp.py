"""SNMP collector.

Vendor-neutral by design: everything here is a standard MIB, so it works the
same against a MikroTik, a Cisco, or a UniFi switch. Where vendors disagree —
and they disagree most about CPU, memory and temperature — SPARK tries each
known source in order and records which one answered rather than pretending
there's a single right OID.

Two things are load-bearing:

  * 64-bit counters are preferred. The 32-bit ifTable counters wrap in under
    six minutes on a saturated gigabit link, which turns throughput graphs into
    fiction.
  * Nothing here raises on an unreachable device. A monitoring tool that throws
    when the thing it monitors goes down is not much use.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData,
    ContextData,
    ObjectIdentity,
    ObjectType,
    SnmpEngine,
    UdpTransportTarget,
    UsmUserData,
    bulk_walk_cmd,
    get_cmd,
)

from . import oids as O
from .base import (
    CapabilityResult,
    DeviceHealth,
    InterfaceStat,
    ProbeReport,
    TemperatureReading,
)

log = logging.getLogger(__name__)

HR_STORAGE_TYPE = "1.3.6.1.2.1.25.2.3.1.2"
HR_STORAGE_TYPE_RAM = "1.3.6.1.2.1.25.2.1.2"

AUTH_PROTOCOLS = ("MD5", "SHA", "SHA224", "SHA256", "SHA384", "SHA512")
PRIV_PROTOCOLS = ("DES", "3DES", "AES", "AES192", "AES256")


@dataclass
class SnmpCredential:
    """SNMP connection details.

    v2c is the common case in a homelab and is fine on a trusted VLAN, but it
    is unauthenticated and unencrypted — the community string crosses the wire
    in clear text. Use read-only communities, and never reuse a password as one.
    """

    version: Literal["v2c", "v3"] = "v2c"
    community: str = "public"

    username: str = ""
    auth_protocol: str = "SHA"
    auth_key: str = ""
    priv_protocol: str = "AES"
    priv_key: str = ""

    port: int = 161
    timeout: float = 3.0
    retries: int = 1

    def build_auth(self) -> CommunityData | UsmUserData:
        if self.version == "v2c":
            # mpModel=1 selects v2c; 0 would be v1, which lacks bulk operations
            # and 64-bit counters.
            return CommunityData(self.community, mpModel=1)

        from pysnmp.hlapi.v3arch.asyncio import auth as _auth  # noqa: F401

        return UsmUserData(
            self.username,
            authKey=self.auth_key or None,
            privKey=self.priv_key or None,
            authProtocol=_resolve_auth_protocol(self.auth_protocol),
            privProtocol=_resolve_priv_protocol(self.priv_protocol),
        )


def _resolve_auth_protocol(name: str):  # type: ignore[no-untyped-def]
    import pysnmp.hlapi.v3arch.asyncio as h

    table = {
        "MD5": "usmHMACMD5AuthProtocol",
        "SHA": "usmHMACSHAAuthProtocol",
        "SHA224": "usmHMAC128SHA224AuthProtocol",
        "SHA256": "usmHMAC192SHA256AuthProtocol",
        "SHA384": "usmHMAC256SHA384AuthProtocol",
        "SHA512": "usmHMAC384SHA512AuthProtocol",
    }
    attr = table.get(name.upper(), "usmHMACSHAAuthProtocol")
    return getattr(h, attr, getattr(h, "usmHMACSHAAuthProtocol"))


def _resolve_priv_protocol(name: str):  # type: ignore[no-untyped-def]
    import pysnmp.hlapi.v3arch.asyncio as h

    table = {
        "DES": "usmDESPrivProtocol",
        "3DES": "usm3DESEDEPrivProtocol",
        "AES": "usmAesCfb128Protocol",
        "AES192": "usmAesCfb192Protocol",
        "AES256": "usmAesCfb256Protocol",
    }
    attr = table.get(name.upper(), "usmAesCfb128Protocol")
    return getattr(h, attr, getattr(h, "usmAesCfb128Protocol"))


# --------------------------------------------------------------------------
# Value handling
# --------------------------------------------------------------------------


def _to_python(value: Any) -> Any:
    """Convert a pysnmp value into something the rest of SPARK can store."""
    if value is None:
        return None
    cls = value.__class__.__name__
    if cls in ("NoSuchObject", "NoSuchInstance", "EndOfMibView"):
        return None
    try:
        if cls == "OctetString":
            # Some string-typed values are genuinely binary (MAC addresses,
            # chassis IDs), so fall back to hex rather than mangling them.
            raw = bytes(value)
            try:
                text = raw.decode("utf-8").strip()
            except UnicodeDecodeError:
                return raw.hex(":")
            if any(ord(ch) < 32 and ch not in "\t\n\r" for ch in text):
                return raw.hex(":")
            return text
        if cls in ("Integer", "Integer32", "Counter32", "Counter64", "Gauge32", "Unsigned32"):
            return int(value)
        if cls == "TimeTicks":
            return int(value) / 100.0
        if cls == "ObjectIdentity" or cls == "ObjectIdentifier":
            return str(value)
        if cls == "IpAddress":
            return str(value.prettyPrint())
    except Exception:  # noqa: BLE001 - never let a decode failure kill a poll
        return None
    return str(value.prettyPrint()) if hasattr(value, "prettyPrint") else str(value)


def _format_mac(value: Any) -> str | None:
    """Normalise a physical address to aa:bb:cc:dd:ee:ff."""
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.replace("-", ":").replace(".", ":").strip()
        parts = [p for p in cleaned.split(":") if p]
        if len(parts) == 6:
            try:
                return ":".join(f"{int(p, 16):02x}" for p in parts)
            except ValueError:
                return None
        hex_only = "".join(ch for ch in cleaned if ch in "0123456789abcdefABCDEF")
        if len(hex_only) == 12:
            return ":".join(hex_only[i : i + 2] for i in range(0, 12, 2)).lower()
        return None
    try:
        raw = bytes(value)
        if len(raw) == 6:
            return ":".join(f"{b:02x}" for b in raw)
    except Exception:  # noqa: BLE001
        pass
    return None


def scale_sensor_value(raw: float, scale: int | None, precision: int | None) -> float:
    """Apply ENTITY-SENSOR-MIB scaling (RFC 3433).

    Two separate corrections, and skipping either produces readings wrong by
    orders of magnitude:

      entPhySensorScale     an exponent *enum*, not a multiplier - 9 means 10^0
      entPhySensorPrecision digits after the decimal point in the raw integer

    A switch reporting 425 with scale=units(9) and precision=1 is at 42.5 °C,
    not 425 °C and not 4250 °C.
    """
    exponent = O.SENSOR_SCALE_EXPONENTS.get(int(scale if scale is not None else 9), 0)
    digits = int(precision or 0)
    return float(raw) * (10**exponent) / (10**digits)


def _index_of(oid: str, base: str) -> str:
    """The table index portion of a walked OID."""
    oid = oid.lstrip(".")
    base = base.lstrip(".")
    if oid.startswith(base + "."):
        return oid[len(base) + 1 :]
    return oid


# --------------------------------------------------------------------------
# Collector
# --------------------------------------------------------------------------


class SnmpCollector:
    name = "snmp"

    def __init__(self, host: str, credential: SnmpCredential | None = None) -> None:
        self.host = host
        self.credential = credential or SnmpCredential()
        self._engine: SnmpEngine | None = None
        self._transport: Any = None
        self._auth: Any = None

    async def _ensure(self) -> tuple[Any, Any, Any]:
        if self._engine is None:
            self._engine = SnmpEngine()
            self._auth = self.credential.build_auth()
            self._transport = await UdpTransportTarget.create(
                (self.host, self.credential.port),
                timeout=self.credential.timeout,
                retries=self.credential.retries,
            )
        return self._engine, self._auth, self._transport

    async def close(self) -> None:
        engine = self._engine
        self._engine = None
        self._transport = None
        self._auth = None
        if engine is not None:
            try:
                engine.close_dispatcher()
            except Exception:  # noqa: BLE001 - best effort
                pass

    # ---------------- low-level operations ----------------

    async def get(self, *oid_list: str) -> dict[str, Any]:
        """GET one or more scalars. Missing values come back absent, not raised."""
        engine, auth, transport = await self._ensure()
        error_indication, error_status, _idx, var_binds = await get_cmd(
            engine,
            auth,
            transport,
            ContextData(),
            *[ObjectType(ObjectIdentity(oid)) for oid in oid_list],
        )
        if error_indication:
            raise TimeoutError(str(error_indication))
        if error_status:
            raise RuntimeError(error_status.prettyPrint())

        out: dict[str, Any] = {}
        for name, value in var_binds:
            converted = _to_python(value)
            if converted is not None:
                out[str(name)] = converted
        return out

    async def walk(self, base_oid: str, max_rows: int = 4096) -> dict[str, Any]:
        """Walk a subtree, returning {index: value}.

        Uses GETBULK, and stops at the end of the subtree rather than running on
        through the rest of the MIB.
        """
        engine, auth, transport = await self._ensure()
        results: dict[str, Any] = {}
        count = 0

        async for error_indication, error_status, _idx, var_binds in bulk_walk_cmd(
            engine,
            auth,
            transport,
            ContextData(),
            0,
            25,
            ObjectType(ObjectIdentity(base_oid)),
            lexicographicMode=False,
        ):
            if error_indication:
                raise TimeoutError(str(error_indication))
            if error_status:
                raise RuntimeError(error_status.prettyPrint())
            for name, value in var_binds:
                oid = str(name)
                if not oid.lstrip(".").startswith(base_oid.lstrip(".")):
                    return results
                converted = _to_python(value)
                if converted is not None:
                    results[_index_of(oid, base_oid)] = converted
                count += 1
                if count >= max_rows:
                    log.warning(
                        "Walk of %s on %s hit the %d row cap; results truncated",
                        base_oid, self.host, max_rows,
                    )
                    return results
        return results

    async def walk_raw(self, base_oid: str, max_rows: int = 4096) -> dict[str, Any]:
        """Like walk(), but keeps pysnmp's own objects (needed for MAC bytes)."""
        engine, auth, transport = await self._ensure()
        results: dict[str, Any] = {}
        count = 0
        async for error_indication, error_status, _idx, var_binds in bulk_walk_cmd(
            engine, auth, transport, ContextData(), 0, 25,
            ObjectType(ObjectIdentity(base_oid)), lexicographicMode=False,
        ):
            if error_indication:
                raise TimeoutError(str(error_indication))
            if error_status:
                raise RuntimeError(error_status.prettyPrint())
            for name, value in var_binds:
                oid = str(name)
                if not oid.lstrip(".").startswith(base_oid.lstrip(".")):
                    return results
                results[_index_of(oid, base_oid)] = value
                count += 1
                if count >= max_rows:
                    return results
        return results

    # ---------------- health ----------------

    async def collect_health(self) -> DeviceHealth:
        health = DeviceHealth()
        try:
            system = await self.get(
                O.SYS_DESCR, O.SYS_OBJECT_ID, O.SYS_UPTIME,
                O.SYS_NAME, O.SYS_LOCATION, O.SYS_CONTACT,
            )
        except Exception as exc:  # noqa: BLE001
            health.error = f"{type(exc).__name__}: {exc}"
            return health

        health.reachable = True
        health.description = system.get(O.SYS_DESCR)
        health.name = system.get(O.SYS_NAME)
        health.location = system.get(O.SYS_LOCATION)
        health.contact = system.get(O.SYS_CONTACT)
        health.uptime_seconds = system.get(O.SYS_UPTIME)
        health.vendor = O.identify_vendor(system.get(O.SYS_OBJECT_ID))
        if health.uptime_seconds is not None:
            health.sources["uptime"] = "sysUpTime"

        # Each of these is optional and independent; one failing must not cost
        # us the others.
        await asyncio.gather(
            self._collect_cpu(health),
            self._collect_memory(health),
            self._collect_temperature(health),
            self._collect_entity(health),
            return_exceptions=True,
        )
        return health

    async def _collect_cpu(self, health: DeviceHealth) -> None:
        try:
            loads = await self.walk(O.HR_PROCESSOR_LOAD)
            values = [float(v) for v in loads.values() if isinstance(v, (int, float))]
            if values:
                health.cpu_percent = round(sum(values) / len(values), 1)
                health.sources["cpu"] = f"HOST-RESOURCES-MIB ({len(values)} core(s))"
                return
        except Exception:  # noqa: BLE001
            pass

        try:
            idle = await self.get(O.UCD_CPU_IDLE)
            raw = idle.get(O.UCD_CPU_IDLE)
            if isinstance(raw, (int, float)):
                health.cpu_percent = round(100.0 - float(raw), 1)
                health.sources["cpu"] = "UCD-SNMP-MIB (100 - ssCpuIdle)"
        except Exception:  # noqa: BLE001
            pass

    async def _collect_memory(self, health: DeviceHealth) -> None:
        try:
            types = await self.walk(HR_STORAGE_TYPE)
            ram_indexes = [
                idx for idx, value in types.items()
                if str(value).lstrip(".") == HR_STORAGE_TYPE_RAM
            ]
            if ram_indexes:
                descrs, units, sizes, used = await asyncio.gather(
                    self.walk(O.HR_STORAGE_DESCR),
                    self.walk(O.HR_STORAGE_UNITS),
                    self.walk(O.HR_STORAGE_SIZE),
                    self.walk(O.HR_STORAGE_USED),
                )
                idx = ram_indexes[0]
                unit = int(units.get(idx, 1) or 1)
                total = int(sizes.get(idx, 0) or 0) * unit
                consumed = int(used.get(idx, 0) or 0) * unit
                if total > 0:
                    health.memory_total_bytes = total
                    health.memory_used_bytes = consumed
                    health.memory_percent = round(consumed / total * 100, 1)
                    label = descrs.get(idx, "physical memory")
                    health.sources["memory"] = f"HOST-RESOURCES-MIB ({label})"
                    return
        except Exception:  # noqa: BLE001
            pass

        try:
            values = await self.get(O.UCD_MEM_TOTAL_REAL, O.UCD_MEM_AVAIL_REAL)
            total_kb = values.get(O.UCD_MEM_TOTAL_REAL)
            avail_kb = values.get(O.UCD_MEM_AVAIL_REAL)
            if isinstance(total_kb, (int, float)) and isinstance(avail_kb, (int, float)):
                total = int(total_kb) * 1024
                consumed = total - int(avail_kb) * 1024
                if total > 0:
                    health.memory_total_bytes = total
                    health.memory_used_bytes = consumed
                    health.memory_percent = round(consumed / total * 100, 1)
                    health.sources["memory"] = "UCD-SNMP-MIB"
        except Exception:  # noqa: BLE001
            pass

    async def _collect_temperature(self, health: DeviceHealth) -> None:
        """ENTITY-SENSOR-MIB, which many prosumer switches simply don't implement."""
        try:
            types, values, scales, precisions, names = await asyncio.gather(
                self.walk(O.ENT_SENSOR_TYPE),
                self.walk(O.ENT_SENSOR_VALUE),
                self.walk(O.ENT_SENSOR_SCALE),
                self.walk(O.ENT_SENSOR_PRECISION),
                self.walk(O.ENT_PHYSICAL_NAME),
                return_exceptions=False,
            )
        except Exception:  # noqa: BLE001
            return

        for index, sensor_type in types.items():
            if int(sensor_type or 0) != O.SENSOR_TYPE_CELSIUS:
                continue
            raw = values.get(index)
            if not isinstance(raw, (int, float)):
                continue
            celsius = scale_sensor_value(
                float(raw), scales.get(index), precisions.get(index)
            )
            if not (-50 <= celsius <= 200):
                continue
            health.temperatures.append(
                TemperatureReading(
                    name=str(names.get(index, f"sensor {index}")),
                    celsius=round(celsius, 1),
                )
            )

        if health.temperatures:
            health.sources["temperature"] = "ENTITY-SENSOR-MIB"

    async def _collect_entity(self, health: DeviceHealth) -> None:
        try:
            classes, models, serials, descrs = await asyncio.gather(
                self.walk(O.ENT_PHYSICAL_CLASS),
                self.walk(O.ENT_PHYSICAL_MODEL),
                self.walk(O.ENT_PHYSICAL_SERIAL),
                self.walk(O.ENT_PHYSICAL_DESCR),
            )
        except Exception:  # noqa: BLE001
            return

        chassis = [i for i, c in classes.items() if int(c or 0) == O.ENT_CLASS_CHASSIS]
        candidates = chassis or list(models.keys())
        for index in candidates:
            model = models.get(index) or descrs.get(index)
            serial = serials.get(index)
            if model and not health.model:
                health.model = str(model).strip() or None
            if serial and not health.serial:
                health.serial = str(serial).strip() or None
            if health.model and health.serial:
                break
        if health.model or health.serial:
            health.sources["hardware"] = "ENTITY-MIB"

    # ---------------- interfaces ----------------

    async def collect_interfaces(self) -> list[InterfaceStat]:
        try:
            descrs = await self.walk(O.IF_DESCR)
        except Exception as exc:  # noqa: BLE001
            log.debug("Interface walk failed on %s: %s", self.host, exc)
            return []
        if not descrs:
            return []

        async def safe_walk(oid: str) -> dict[str, Any]:
            try:
                return await self.walk(oid)
            except Exception:  # noqa: BLE001
                return {}

        (
            types, speeds, admin, oper, in32, out32, in_err, out_err,
            names, aliases, high_speed, in64, out64,
        ) = await asyncio.gather(
            safe_walk(O.IF_TYPE), safe_walk(O.IF_SPEED),
            safe_walk(O.IF_ADMIN_STATUS), safe_walk(O.IF_OPER_STATUS),
            safe_walk(O.IF_IN_OCTETS), safe_walk(O.IF_OUT_OCTETS),
            safe_walk(O.IF_IN_ERRORS), safe_walk(O.IF_OUT_ERRORS),
            safe_walk(O.IF_NAME), safe_walk(O.IF_ALIAS),
            safe_walk(O.IF_HIGH_SPEED), safe_walk(O.IF_HC_IN_OCTETS),
            safe_walk(O.IF_HC_OUT_OCTETS),
        )

        try:
            macs_raw = await self.walk_raw(O.IF_PHYS_ADDRESS)
        except Exception:  # noqa: BLE001
            macs_raw = {}

        interfaces: list[InterfaceStat] = []
        for index, descr in descrs.items():
            try:
                idx = int(index)
            except ValueError:
                continue

            if_type = types.get(index)
            if_type_int = int(if_type) if isinstance(if_type, (int, float)) else None

            # ifHighSpeed is in Mbps and doesn't saturate; ifSpeed is bps in a
            # 32-bit field, so it caps out at about 4.29 Gbps.
            speed_mbps: int | None = None
            if isinstance(high_speed.get(index), (int, float)) and high_speed[index]:
                speed_mbps = int(high_speed[index])
            elif isinstance(speeds.get(index), (int, float)) and speeds[index]:
                speed_mbps = int(int(speeds[index]) / 1_000_000)

            has_64 = index in in64 or index in out64

            interfaces.append(
                InterfaceStat(
                    index=idx,
                    descr=str(descr) if descr is not None else None,
                    name=str(names[index]) if index in names else None,
                    alias=str(aliases[index]).strip() or None if index in aliases else None,
                    mac=_format_mac(macs_raw.get(index)),
                    type=if_type_int,
                    type_name=O.IF_TYPE_NAMES.get(if_type_int or -1),
                    admin_status=O.IF_ADMIN_STATUS_NAMES.get(int(admin.get(index, 0) or 0)),
                    oper_status=O.IF_OPER_STATUS_NAMES.get(int(oper.get(index, 0) or 0)),
                    speed_mbps=speed_mbps,
                    in_octets=int(in64.get(index, in32.get(index, 0)) or 0),
                    out_octets=int(out64.get(index, out32.get(index, 0)) or 0),
                    in_errors=int(in_err.get(index, 0) or 0),
                    out_errors=int(out_err.get(index, 0) or 0),
                    counters_are_64bit=has_64,
                )
            )

        interfaces.sort(key=lambda i: i.index)
        return interfaces

    # ---------------- probe ----------------

    async def probe(self) -> ProbeReport:
        """Ask the device what it actually supports.

        Vendor SNMP documentation is unreliable enough that guessing is not
        worth it — particularly on prosumer gear, where temperature and MAC
        tables are frequently absent despite being standard MIBs.
        """
        started = time.monotonic()
        report = ProbeReport(host=self.host)

        try:
            system = await self.get(O.SYS_DESCR, O.SYS_OBJECT_ID, O.SYS_NAME, O.SYS_UPTIME)
        except Exception as exc:  # noqa: BLE001
            report.error = f"{type(exc).__name__}: {exc}"
            report.duration_seconds = round(time.monotonic() - started, 2)
            return report

        report.reachable = True
        report.sys_descr = system.get(O.SYS_DESCR)
        report.sys_name = system.get(O.SYS_NAME)
        report.sys_object_id = system.get(O.SYS_OBJECT_ID)
        report.vendor = O.identify_vendor(report.sys_object_id)

        uptime = system.get(O.SYS_UPTIME)
        if uptime is not None:
            report.uptime_human = DeviceHealth(uptime_seconds=uptime).uptime_human

        for probe in O.CAPABILITY_PROBES:
            result = CapabilityResult(
                key=probe.key, label=probe.label, supported=False, notes=probe.notes
            )
            try:
                found: dict[str, Any] = {}
                for oid in probe.oids:
                    found = await (self.walk(oid, max_rows=64) if probe.walk else self.get(oid))
                    if found:
                        break
                if found:
                    result.supported = True
                    result.sample_count = len(found)
                    first = next(iter(found.values()))
                    sample = str(first)
                    result.sample = sample[:80] + ("…" if len(sample) > 80 else "")
            except Exception as exc:  # noqa: BLE001
                result.error = f"{type(exc).__name__}"
            report.capabilities.append(result)

        report.duration_seconds = round(time.monotonic() - started, 2)
        return report


async def probe_host(
    host: str,
    community: str = "public",
    version: str = "v2c",
    port: int = 161,
    timeout: float = 3.0,
    **v3: Any,
) -> ProbeReport:
    """Convenience wrapper used by the CLI."""
    credential = SnmpCredential(
        version="v3" if version == "v3" else "v2c",
        community=community,
        port=port,
        timeout=timeout,
        **v3,
    )
    collector = SnmpCollector(host, credential)
    try:
        return await collector.probe()
    finally:
        await collector.close()
