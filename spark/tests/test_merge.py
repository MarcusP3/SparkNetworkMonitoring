"""Merging duplicate devices, and sweeps that respect the merge afterwards.

The firewall with a gateway address on every VLAN: across a router SPARK
sees no MAC, so each address became a device. After a merge, the addresses
belong to the firewall, everything that hung off the duplicates hangs off it,
and the next sweep must not bring the duplicates back -- or the merge was for
nothing.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from spark import db as D
from spark import merge
from spark.config import Config
from spark.discovery.store import record
from spark.discovery.sweep import Observation
from spark.main import create_app
from spark.models import (
    AlertMute,
    CheckType,
    Device,
    DeviceAddress,
    DeviceRole,
    Incident,
    Service,
    SnmpDevice,
    Target,
    utcnow,
)
from spark.snmp_config import ProfileInput, save_profile
from spark.vault import vault_for

PASSWORD = "correct horse battery"


def _config() -> Config:
    tmp = Path(tempfile.mkdtemp(prefix="spark-merge-"))
    config = Config.model_validate({
        "app": {"data_dir": str(tmp / "data"), "log_level": "WARNING"},
        "network": {"subnets": []},
    })
    config.app.data_dir.mkdir(parents=True, exist_ok=True)
    return config


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


# 1 firewall (MAC, on SPARK's own VLAN); 2 and 3 its gateway addresses on
# other VLANs (by IP); 4 a server hanging off 2 on the map; 5 a real other box.
@pytest.fixture
def db():
    config = _config()

    async def setup():
        D.init_engine(config)
        await D.init_db(config)
        async with D.session_scope() as s:
            s.add_all([
                Device(mac="aa:bb:cc:00:00:01", primary_ip="10.1.10.1", friendly_name="firewall",
                       role=DeviceRole.GATEWAY, last_seen=utcnow()),
                Device(primary_ip="10.1.20.1", last_seen=utcnow()),
                Device(primary_ip="10.1.30.1", friendly_name="iot gw", last_seen=utcnow()),
            ])
            await s.flush()
            s.add_all([
                Device(mac="aa:bb:cc:00:00:04", primary_ip="10.1.20.5", friendly_name="server",
                       parent_device_id=2, last_seen=utcnow()),
                Device(mac="aa:bb:cc:00:00:05", primary_ip="10.1.10.9", friendly_name="other",
                       last_seen=utcnow()),
            ])
            s.add(Target(name="vlan20 gw ping", check_type=CheckType.PING, address="10.1.20.1",
                         device_id=2))
            s.add(Service(device_id=1, port=443, name="https", state="open"))
            s.add(Service(device_id=2, port=443, name="https", state="open"))
            s.add(Service(device_id=2, port=53, name="dns", state="open"))
            await s.flush()
            s.add(Target(name="vlan20 https", check_type=CheckType.TCP, address="10.1.20.1:443",
                         device_id=2, service_id=2))
            s.add(Incident(target_id=1, opened_at=utcnow(), cause="timeout"))
            s.add(AlertMute(device_id=2))
    run(setup())
    yield config
    run(D.close_engine())


async def _get(model, ident):  # type: ignore[no-untyped-def]
    async with D.session_scope() as s:
        return await s.get(model, ident)


async def _all(model):  # type: ignore[no-untyped-def]
    async with D.session_scope() as s:
        return list((await s.execute(select(model))).scalars())


def do_merge(keep: int, other: int) -> merge.Plan:
    async def go():
        async with D.session_scope() as s:
            return await merge.apply(s, keep, other)
    return run(go())


class TestMerging:
    def test_everything_moves_to_the_device_kept(self, db):
        done = do_merge(1, 2)
        assert done.addresses == ["10.1.20.1"]
        assert run(_get(Device, 2)) is None, "the duplicate is gone"
        addresses = run(_all(DeviceAddress))
        assert [(a.device_id, a.ip) for a in addresses] == [(1, "10.1.20.1")]
        targets = {t.name: t for t in run(_all(Target))}
        assert targets["vlan20 gw ping"].device_id == 1
        assert [i.target_id for i in run(_all(Incident))] == [1], "history came with it"
        services = {(s.device_id, s.port) for s in run(_all(Service))}
        assert services == {(1, 443), (1, 53)}, "a shared port is kept once"
        assert targets["vlan20 https"].service_id == 1, "and its watcher follows the kept copy"
        assert run(_get(Device, 4)).parent_device_id == 1, "the map follows"
        assert run(_all(AlertMute)) == [], "the duplicate's mute went with it"
        assert run(_get(Device, 1)).primary_ip == "10.1.10.1"

    def test_it_fills_in_only_what_the_kept_device_lacks(self, db):
        async def blank():
            async with D.session_scope() as s:
                d = await s.get(Device, 1)
                d.friendly_name, d.role = None, DeviceRole.UNKNOWN
                (await s.get(Device, 3)).role = DeviceRole.GATEWAY
        run(blank())
        done = do_merge(1, 3)
        kept = run(_get(Device, 1))
        assert done.takes_name and kept.friendly_name == "iot gw"
        assert kept.role == DeviceRole.GATEWAY and kept.mac == "aa:bb:cc:00:00:01"

    def test_a_mac_only_the_duplicate_has_comes_across(self, db):
        do_merge(2, 1)
        kept = run(_get(Device, 2))
        assert kept.mac == "aa:bb:cc:00:00:01" and kept.primary_ip == "10.1.20.1"
        assert [a.ip for a in run(_all(DeviceAddress))] == ["10.1.10.1"]

    def test_extra_addresses_travel_with_a_second_merge(self, db):
        do_merge(3, 2)
        do_merge(1, 3)
        assert sorted(a.ip for a in run(_all(DeviceAddress))) == ["10.1.20.1", "10.1.30.1"]
        assert {a.device_id for a in run(_all(DeviceAddress))} == {1}

    def test_snmp_polling_moves_but_two_polled_devices_are_refused(self, db):
        async def poll(*ids):  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                profile = await save_profile(s, vault_for(db), ProfileInput(
                    name=f"p{ids[0]}", version="v2c", community="x"))
                for i in ids:
                    s.add(SnmpDevice(device_id=i, profile_id=profile.id))
        run(poll(2))
        do_merge(1, 2)
        assert [r.device_id for r in run(_all(SnmpDevice))] == [1]
        run(poll(3))
        with pytest.raises(merge.MergeError, match="Both are polled over SNMP"):
            do_merge(1, 3)

    @pytest.mark.parametrize("keep,other,message", [
        (1, 1, "into itself"), (1, 99, "no longer exists"), (1, 5, "two real devices"),
    ])
    def test_refusals(self, db, keep, other, message):
        with pytest.raises(merge.MergeError, match=message):
            do_merge(keep, other)
        assert len(run(_all(Device))) == 5, "nothing changed"


class TestSweepsAfterAMerge:
    def sweep(self, ip, mac=None):  # type: ignore[no-untyped-def]
        async def go():
            async with D.session_scope() as s:
                device, created = await record(s, Observation(ip=ip, mac=mac, subnet="x"))
                return device.id, created
        return run(go())

    def test_the_address_is_counted_as_the_kept_device(self, db):
        do_merge(1, 2)
        assert self.sweep("10.1.20.1") == (1, False), "not a new device again"
        kept = run(_get(Device, 1))
        assert kept.primary_ip == "10.1.10.1", "its own address does not change"
        assert run(_all(DeviceAddress))[0].last_seen is not None

    def test_a_real_device_turning_up_at_the_address_takes_it_back(self, db):
        do_merge(1, 2)
        device_id, created = self.sweep("10.1.20.1", mac="aa:bb:cc:00:00:99")
        assert created and device_id != 1
        assert run(_all(DeviceAddress)) == []

    def test_the_kept_device_seen_normally_keeps_its_addresses(self, db):
        do_merge(1, 2)
        assert self.sweep("10.1.10.1", mac="aa:bb:cc:00:00:01") == (1, False)
        assert [a.ip for a in run(_all(DeviceAddress))] == ["10.1.20.1"]


# --------------------------------------------------------------------------
# The pages
# --------------------------------------------------------------------------


@pytest.fixture
def site(db):
    with TestClient(create_app(db), follow_redirects=False) as client:
        client.post("/setup", data={"username": "admin", "password": PASSWORD,
                                    "password_confirm": PASSWORD, "timezone": "UTC"})
        yield client


class TestPages:
    def test_the_device_page_offers_it_and_the_preview_changes_nothing(self, site):
        page = site.get("/devices/1").text
        assert '<form method="get" action="/devices/1/merge"' in page
        assert '<option value="2">10.1.20.1 (by IP)</option>' in page
        preview = site.get("/devices/1/merge?other=2").text
        assert "10.1.20.1 becomes an extra address of" in preview.replace("\n", " ").replace("  ", " ")
        assert "vlan20 gw ping" in preview and "dns" in preview
        assert run(_get(Device, 2)) is not None, "the preview did not merge"

    def test_merging_and_then_removing_the_address(self, site):
        response = site.post("/devices/1/merge", data={"other_id": "2"})
        assert response.headers["location"] == "/devices/1?merged=1#addresses"
        page = site.get("/devices/1?merged=1").text
        assert "Merged. 1 address is now part of this device." in page
        assert "+1 more address" in page and "<code>10.1.20.1</code>" in page
        devices = site.get("/devices").text
        assert 'title="Also answers at 10.1.20.1">+1</span>' in devices
        address = run(_all(DeviceAddress))[0]
        site.post(f"/devices/1/addresses/{address.id}/delete")
        assert run(_all(DeviceAddress)) == []

    def test_a_refused_merge_says_why_and_changes_nothing(self, site):
        response = site.get("/devices/1/merge?other=5")
        assert response.status_code == 400 and "two real devices" in response.text
        site.post("/devices/1/merge", data={"other_id": "5"})
        assert run(_get(Device, 5)) is not None

    def test_an_address_is_removed_only_from_its_own_device(self, site):
        site.post("/devices/1/merge", data={"other_id": "2"})
        address = run(_all(DeviceAddress))[0]
        site.post(f"/devices/5/addresses/{address.id}/delete")
        assert len(run(_all(DeviceAddress))) == 1


def test_migration_10_from_a_version_9_database():
    from sqlalchemy import inspect, text

    async def shape(wind_back: bool):
        config = _config()
        D.init_engine(config)
        await D.init_db(config)
        if wind_back:
            async with D.session_scope() as s:
                await s.execute(text("DROP TABLE device_address"))
                await D._set_version(s, 9)
            await D.close_engine()
            D.init_engine(config)
            await D.init_db(config)
        async with D.session_scope() as s:
            connection = await s.connection()
            columns = await connection.run_sync(
                lambda sync: sorted(c["name"] for c in inspect(sync).get_columns("device_address")))
            version = await D._get_version(s)
        await D.close_engine()
        return columns, version

    assert run(shape(True)) == run(shape(False))
    assert run(shape(False))[1] == D.CURRENT_VERSION >= 10
