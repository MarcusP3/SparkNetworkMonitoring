"""Tests for the SNMP collector.

Split in two:

  * Pure-function tests, which cover the parsing and scaling logic that is easy
    to get quietly wrong and impossible to notice in production — a temperature
    off by a factor of ten still looks like a plausible number.
  * Live tests against a local net-snmp agent, skipped when one isn't running.
    Start one with `tests/local_agent.sh` before running these.

Run:  pip install -e ".[dev]" && pytest -q
"""

from __future__ import annotations

import asyncio
import os
import socket

import pytest

from spark.collectors import SnmpCollector, SnmpCredential
from spark.collectors import oids as O
from spark.collectors.base import DeviceHealth, InterfaceStat
from spark.collectors.snmp import _format_mac, _index_of, scale_sensor_value

AGENT_HOST = os.environ.get("SPARK_TEST_SNMP_HOST", "127.0.0.1")
AGENT_PORT = int(os.environ.get("SPARK_TEST_SNMP_PORT", "11161"))
AGENT_COMMUNITY = os.environ.get("SPARK_TEST_SNMP_COMMUNITY", "sparktest")


def _agent_running() -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(0.3)
    try:
        sock.sendto(b"\x30\x00", (AGENT_HOST, AGENT_PORT))
        return True
    except OSError:
        return False
    finally:
        sock.close()


needs_agent = pytest.mark.skipif(
    not _agent_running(), reason="no local SNMP agent; see tests/local_agent.sh"
)


# --------------------------------------------------------------------------
# Sensor scaling — RFC 3433
# --------------------------------------------------------------------------


class TestSensorScaling:
    def test_units_scale_no_precision(self):
        # scale=units(9), precision=0 → the raw value is already Celsius
        assert scale_sensor_value(42, 9, 0) == 42.0

    def test_precision_one_decimal(self):
        # The common switch case: 425 with one decimal digit is 42.5 °C.
        # Ignoring precision here reports a switch as being at 425 °C.
        assert scale_sensor_value(425, 9, 1) == 42.5

    def test_precision_two_decimals(self):
        assert scale_sensor_value(4250, 9, 2) == 42.5

    def test_milli_scale(self):
        # scale=milli(8) → 10^-3
        assert scale_sensor_value(42000, 8, 0) == 42.0

    def test_missing_scale_defaults_to_units(self):
        assert scale_sensor_value(42, None, None) == 42.0

    def test_scale_is_an_exponent_enum_not_a_multiplier(self):
        # Treating scale=9 as "multiply by 9" would give 378. This assertion
        # exists to catch exactly that misreading of the RFC.
        assert scale_sensor_value(42, 9, 0) == 42.0
        assert scale_sensor_value(42, 9, 0) != 378


# --------------------------------------------------------------------------
# MAC formatting
# --------------------------------------------------------------------------


class TestMacFormatting:
    def test_bytes_input(self):
        assert _format_mac(b"\xaa\xbb\xcc\xdd\xee\xff") == "aa:bb:cc:dd:ee:ff"

    def test_colon_string_is_lowercased(self):
        assert _format_mac("AA:BB:CC:DD:EE:FF") == "aa:bb:cc:dd:ee:ff"

    def test_hyphen_separated(self):
        assert _format_mac("AA-BB-CC-DD-EE-FF") == "aa:bb:cc:dd:ee:ff"

    def test_cisco_dotted_notation(self):
        assert _format_mac("aabb.ccdd.eeff") == "aa:bb:cc:dd:ee:ff"

    def test_bare_hex(self):
        assert _format_mac("aabbccddeeff") == "aa:bb:cc:dd:ee:ff"

    def test_single_digit_octets_are_padded(self):
        assert _format_mac("0:1:2:3:4:5") == "00:01:02:03:04:05"

    def test_rejects_wrong_length(self):
        assert _format_mac("aa:bb:cc") is None

    def test_rejects_none_and_empty(self):
        assert _format_mac(None) is None
        assert _format_mac("") is None

    def test_rejects_non_hex(self):
        assert _format_mac("zz:bb:cc:dd:ee:ff") is None


# --------------------------------------------------------------------------
# Vendor identification
# --------------------------------------------------------------------------


class TestVendorIdentification:
    def test_ubiquiti(self):
        assert O.identify_vendor("1.3.6.1.4.1.41112.1.6") == "Ubiquiti (UniFi)"

    def test_leading_dot_tolerated(self):
        assert O.identify_vendor(".1.3.6.1.4.1.14988.1") == "MikroTik"

    def test_exact_prefix_match(self):
        assert O.identify_vendor("1.3.6.1.4.1.9") == "Cisco"

    def test_longest_prefix_wins(self):
        # 1.3.6.1.4.1.11 is HP and 1.3.6.1.4.1.11863 is TP-Link. A naive
        # startswith() check would call every TP-Link device an HP.
        assert O.identify_vendor("1.3.6.1.4.1.11863.1.1") == "TP-Link"

    def test_unknown_returns_none(self):
        assert O.identify_vendor("1.3.6.1.4.1.999999.1") is None

    def test_none_input(self):
        assert O.identify_vendor(None) is None


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


class TestHelpers:
    def test_index_extraction(self):
        assert _index_of("1.3.6.1.2.1.2.2.1.2.4", "1.3.6.1.2.1.2.2.1.2") == "4"

    def test_multipart_index(self):
        assert _index_of("1.3.6.1.2.1.4.22.1.2.5.192.168.1.1", "1.3.6.1.2.1.4.22.1.2") == \
            "5.192.168.1.1"

    def test_uptime_formatting(self):
        assert DeviceHealth(uptime_seconds=90).uptime_human == "1m"
        assert DeviceHealth(uptime_seconds=3700).uptime_human == "1h 1m"
        assert DeviceHealth(uptime_seconds=200000).uptime_human == "2d 7h 33m"
        assert DeviceHealth().uptime_human is None

    def test_interface_label_prefers_configured_alias(self):
        # ifAlias is what a human typed into the switch; ifDescr is what the
        # driver made up. Prefer the human.
        interface = InterfaceStat(index=1, descr="GigabitEthernet0/1",
                                  name="gi0/1", alias="Uplink to core")
        assert interface.label == "Uplink to core"

    def test_interface_label_falls_back(self):
        assert InterfaceStat(index=7).label == "if7"

    def test_max_temperature(self):
        from spark.collectors.base import TemperatureReading

        health = DeviceHealth(temperatures=[
            TemperatureReading("CPU", 51.0),
            TemperatureReading("PHY", 63.5),
        ])
        assert health.max_temperature == 63.5
        assert DeviceHealth().max_temperature is None


# --------------------------------------------------------------------------
# Live agent
# --------------------------------------------------------------------------


@needs_agent
class TestAgainstLocalAgent:
    @staticmethod
    def _collector(community: str = AGENT_COMMUNITY, timeout: float = 2.0) -> SnmpCollector:
        return SnmpCollector(
            AGENT_HOST,
            SnmpCredential(community=community, port=AGENT_PORT, timeout=timeout, retries=0),
        )

    def test_probe_reports_reachable(self):
        async def go():
            collector = self._collector()
            try:
                return await collector.probe()
            finally:
                await collector.close()

        report = asyncio.run(go())
        assert report.reachable
        assert report.sys_name
        assert report.has("system")
        assert report.has("interfaces")

    def test_health_collects_system_and_resources(self):
        async def go():
            collector = self._collector()
            try:
                return await collector.collect_health()
            finally:
                await collector.close()

        health = asyncio.run(go())
        assert health.reachable
        assert health.error is None
        assert health.uptime_seconds is not None and health.uptime_seconds > 0
        assert health.cpu_percent is not None
        assert 0 <= health.cpu_percent <= 100
        assert health.memory_percent is not None
        assert 0 <= health.memory_percent <= 100
        assert "cpu" in health.sources

    def test_interfaces_are_parsed(self):
        async def go():
            collector = self._collector()
            try:
                return await collector.collect_interfaces()
            finally:
                await collector.close()

        interfaces = asyncio.run(go())
        assert interfaces
        loopback = next((i for i in interfaces if (i.descr or "") == "lo"), None)
        assert loopback is not None
        assert loopback.oper_status == "up"
        assert interfaces == sorted(interfaces, key=lambda i: i.index)

    def test_prefers_64bit_counters_when_available(self):
        async def go():
            collector = self._collector()
            try:
                return await collector.collect_interfaces()
            finally:
                await collector.close()

        interfaces = asyncio.run(go())
        assert any(i.counters_are_64bit for i in interfaces)

    def test_wrong_community_is_reported_not_raised(self):
        async def go():
            collector = self._collector(community="definitely-wrong", timeout=1.0)
            try:
                return await collector.collect_health()
            finally:
                await collector.close()

        health = asyncio.run(go())
        # A monitoring tool must never throw because a device rejected it.
        assert health.reachable is False
        assert health.error

    def test_unreachable_host_is_reported_not_raised(self):
        async def go():
            collector = SnmpCollector(
                "127.0.0.1",
                SnmpCredential(community="x", port=1, timeout=0.5, retries=0),
            )
            try:
                return await collector.probe()
            finally:
                await collector.close()

        report = asyncio.run(go())
        assert report.reachable is False
        assert report.error
