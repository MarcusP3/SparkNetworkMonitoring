"""Tests for discovery.

Weighted heavily toward the identity rules in `store.py`. Getting those wrong
does not raise: it silently splits one device into two rows, or merges two into
one, and you find out months later when a history looks wrong. Network code is
tested through its parsing, which is where its bugs are.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import func, select

from spark import db as D
from spark.config import Config
from spark.discovery import oui
from spark.discovery.store import record, record_all
from spark.discovery.sweep import MAX_HOSTS_PER_SUBNET, Observation, hosts_in, read_arp_table
from spark.models import Device


@pytest.fixture
def session_factory():
    tmp = Path(tempfile.mkdtemp(prefix="spark-discovery-"))
    config = Config.model_validate(
        {"app": {"data_dir": str(tmp / "data")}, "network": {"subnets": []}}
    )
    config.app.data_dir.mkdir(parents=True, exist_ok=True)

    async def setup():
        D.init_engine(config)
        await D.init_db(config)

    asyncio.run(setup())
    yield D.session_scope
    asyncio.run(D.close_engine())


def obs(ip, mac=None, hostname=None, vendor=None, subnet="LAN") -> Observation:
    return Observation(ip=ip, mac=mac, hostname=hostname, vendor=vendor, subnet=subnet)


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------


class TestDeviceIdentity:
    def test_same_mac_at_a_new_address_is_the_same_device(self, session_factory):
        async def go():
            async with session_factory() as s:
                await record(s, obs("10.1.10.50", "aa:bb:cc:dd:ee:ff"))
                # DHCP moves it
                device, created = await record(s, obs("10.1.10.99", "aa:bb:cc:dd:ee:ff"))
                total = await s.scalar(select(func.count()).select_from(Device))
                return device.primary_ip, created, total

        ip, created, total = asyncio.run(go())
        # The whole reason identity keys on MAC: a lease change must not look
        # like a new device and orphan the old row's history.
        assert total == 1 and not created
        assert ip == "10.1.10.99"

    def test_same_address_with_a_different_mac_is_a_different_device(self, session_factory):
        async def go():
            async with session_factory() as s:
                await record(s, obs("10.1.10.50", "aa:bb:cc:dd:ee:ff"))
                await record(s, obs("10.1.10.50", "11:22:33:44:55:66"))
                return await s.scalar(select(func.count()).select_from(Device))

        # A recycled address is a different machine, not the same one renamed.
        assert asyncio.run(go()) == 2

    def test_a_mac_less_row_is_adopted_when_a_mac_appears(self, session_factory):
        async def go():
            async with session_factory() as s:
                await record(s, obs("10.1.10.50"))          # routed: no ARP
                device, created = await record(s, obs("10.1.10.50", "aa:bb:cc:dd:ee:ff"))
                total = await s.scalar(select(func.count()).select_from(Device))
                return device.mac, created, total

        mac, created, total = asyncio.run(go())
        assert total == 1 and not created
        assert mac == "aa:bb:cc:dd:ee:ff"

    def test_a_known_device_seen_without_a_mac_is_not_duplicated(self, session_factory):
        async def go():
            async with session_factory() as s:
                await record(s, obs("10.1.10.50", "aa:bb:cc:dd:ee:ff"))
                # Seen again from across a router, so no MAC this time.
                device, created = await record(s, obs("10.1.10.50"))
                total = await s.scalar(select(func.count()).select_from(Device))
                return device.mac, created, total

        mac, created, total = asyncio.run(go())
        assert total == 1 and not created
        assert mac == "aa:bb:cc:dd:ee:ff", "an existing MAC must not be erased"

    def test_a_user_set_name_survives_rediscovery(self, session_factory):
        async def go():
            async with session_factory() as s:
                device, _ = await record(s, obs("10.1.10.50", "aa:bb:cc:dd:ee:ff"))
                device.friendly_name = "sw-core"
                await s.flush()
                again, _ = await record(
                    s, obs("10.1.10.50", "aa:bb:cc:dd:ee:ff", hostname="dhcp-50.lan")
                )
                return again.friendly_name, again.hostname

        name, hostname = asyncio.run(go())
        assert name == "sw-core", "discovery must never overwrite what a human typed"
        assert hostname == "dhcp-50.lan"

    def test_discovery_does_not_claim_a_device_is_up(self, session_factory):
        async def go():
            async with session_factory() as s:
                device, _ = await record(s, obs("10.1.10.50", "aa:bb:cc:dd:ee:ff"))
                return device.status, device.last_seen

        status, last_seen = asyncio.run(go())
        # "Answered a sweep" is not "is up" -- the check engine owns status.
        assert status.value == "unknown"
        assert last_seen is not None

    def test_first_seen_is_not_moved_by_later_sightings(self, session_factory):
        async def go():
            async with session_factory() as s:
                first, _ = await record(s, obs("10.1.10.50", "aa:bb:cc:dd:ee:ff"))
                original = first.first_seen
                again, _ = await record(s, obs("10.1.10.50", "aa:bb:cc:dd:ee:ff"))
                return original, again.first_seen

        original, later = asyncio.run(go())
        assert original == later

    def test_record_all_reports_only_genuinely_new_devices(self, session_factory):
        async def go():
            async with session_factory() as s:
                await record_all(s, [obs("10.1.10.1", "aa:aa:aa:aa:aa:aa")])
                seen, new = await record_all(
                    s,
                    [
                        obs("10.1.10.1", "aa:aa:aa:aa:aa:aa"),   # known
                        obs("10.1.10.2", "bb:bb:bb:bb:bb:bb"),   # new
                    ],
                )
                return seen, len(new)

        seen, new = asyncio.run(go())
        assert seen == 2 and new == 1


# --------------------------------------------------------------------------
# ARP parsing
# --------------------------------------------------------------------------


class TestArpTable:
    def _write(self, body: str) -> Path:
        path = Path(tempfile.mkdtemp()) / "arp"
        path.write_text(body)
        return path

    def test_parses_complete_entries(self):
        table = read_arp_table(self._write(
            "IP address       HW type     Flags       HW address            Mask     Device\n"
            "10.1.10.1        0x1         0x2         AA:BB:CC:DD:EE:FF     *        ens18\n"
        ))
        assert table == {"10.1.10.1": "aa:bb:cc:dd:ee:ff"}

    def test_skips_incomplete_entries(self):
        # Flags 0x0 means "we asked and nobody answered". Recording that as a
        # device's MAC would invent a device at every unused address we probed.
        table = read_arp_table(self._write(
            "IP address       HW type     Flags       HW address            Mask     Device\n"
            "10.1.10.7        0x1         0x0         00:00:00:00:00:00     *        ens18\n"
        ))
        assert table == {}

    def test_missing_file_is_not_an_error(self):
        assert read_arp_table(Path("/nonexistent/arp")) == {}

    def test_garbage_lines_are_skipped(self):
        table = read_arp_table(self._write("header\nnot a real line\n\n"))
        assert table == {}


# --------------------------------------------------------------------------
# Subnet expansion
# --------------------------------------------------------------------------


class TestHostsIn:
    def test_a_24_excludes_network_and_broadcast(self):
        hosts = hosts_in("10.1.10.0/24")
        assert len(hosts) == 254
        assert "10.1.10.0" not in hosts and "10.1.10.255" not in hosts

    def test_an_oversized_subnet_is_refused_rather_than_swept(self):
        # Silently probing 65k addresses would be a worse answer than none.
        assert hosts_in("10.0.0.0/16") == []

    def test_the_cap_boundary(self):
        assert len(hosts_in("10.1.0.0/22")) <= MAX_HOSTS_PER_SUBNET


# --------------------------------------------------------------------------
# OUI
# --------------------------------------------------------------------------


class TestOui:
    def test_known_vendor(self):
        assert oui.lookup("b8:27:eb:11:22:33") == "Raspberry Pi"

    def test_proxmox_guests_are_recognisable(self):
        assert oui.lookup("52:54:00:aa:bb:cc") == "QEMU/KVM virtual"

    def test_unknown_prefix_returns_none_rather_than_guessing(self):
        assert oui.lookup("de:ad:be:ef:00:01") is None

    def test_normalises_input_formats(self):
        for form in ("B8-27-EB-11-22-33", "b827.eb11.2233", "B827EB112233"):
            assert oui.lookup(form) == "Raspberry Pi", form

    def test_randomised_addresses_are_flagged(self):
        # Phones randomise per network; these never come back the same.
        assert oui.is_locally_administered("b2:27:eb:11:22:33")
        assert not oui.is_locally_administered("b8:27:eb:11:22:33")

    def test_junk_is_not_a_mac(self):
        assert oui.normalise("not-a-mac") is None
        assert oui.lookup(None) is None
        assert not oui.is_locally_administered(None)
