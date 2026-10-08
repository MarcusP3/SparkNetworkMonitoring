"""SNMP threshold alerts: starred ports, CPU, memory, temperature -- and the mute list.

Each rule has to be right in both directions. Too eager -- a single spike, a
port bouncing through a reboot, a value hovering on the line -- and the
channel fills with noise until it is muted by hand. Too quiet -- a streak that
never completes because of how it is counted, a recovery that never comes --
and the one alert that mattered is not there. The tests feed record_poll a
sequence of polls a minute apart and read what was queued.
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from spark import db as D
from spark import snmp_alerts
from spark.collectors.base import DeviceHealth, InterfaceStat, TemperatureReading
from spark.config import Config
from spark.main import create_app
from spark.models import (
    AlertMute,
    AlertState,
    CheckType,
    Device,
    Notification,
    SnmpInterface,
    Target,
    utcnow,
)
from spark.snmp_config import ProfileInput, add_device, save_profile
from spark.snmp_poll import record_poll
from spark.vault import vault_for

T0 = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
GIG = 1000
PASSWORD = "correct horse battery"


def _config() -> Config:
    tmp = Path(tempfile.mkdtemp(prefix="spark-rules-"))
    config = Config.model_validate({
        "app": {"data_dir": str(tmp / "data"), "log_level": "WARNING"},
        "network": {"subnets": []},
    })
    config.app.data_dir.mkdir(parents=True, exist_ok=True)
    return config


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


@pytest.fixture
def db():
    """One device, core-switch at 10.0.0.2, on the SNMP list as row 1."""
    config = _config()

    async def setup():
        D.init_engine(config)
        await D.init_db(config)
        async with D.session_scope() as s:
            s.add(Device(mac="aa:bb:cc:00:00:01", primary_ip="10.0.0.2",
                         friendly_name="core-switch", last_seen=utcnow()))
            await s.flush()
            profile = await save_profile(s, vault_for(config), ProfileInput(
                name="lab", version="v2c", community="x"))
            await add_device(s, 1, profile.id)
    run(setup())
    yield config
    run(D.close_engine())


def port(index=1, *, oper="up", admin="up", octets=0, speed=GIG) -> InterfaceStat:
    return InterfaceStat(index=index, name=f"port{index}", admin_status=admin, oper_status=oper,
                         speed_mbps=speed, in_octets=octets, out_octets=0, in_errors=0,
                         out_errors=0, counters_are_64bit=True)


def poll(minute: float, *, cpu=10.0, memory=40.0, temp=None, ports=()) -> None:
    health = DeviceHealth(
        reachable=True, uptime_seconds=100_000 + minute * 60, cpu_percent=cpu,
        memory_percent=memory,
        temperatures=[TemperatureReading("board", temp)] if temp is not None else [],
    )

    async def go():
        async with D.session_scope() as s:
            await record_poll(s, 1, health, list(ports), T0 + timedelta(minutes=minute),
                              interval=60)
    run(go())


def sent() -> list[Notification]:
    async def go():
        async with D.session_scope() as s:
            return list((await s.execute(select(Notification).order_by(Notification.id))).scalars())
    return run(go())


def kinds() -> list[str]:
    return [n.kind for n in sent()]


def star(index=1, on=True) -> None:
    async def go():
        async with D.session_scope() as s:
            iface = await s.scalar(select(SnmpInterface).where(SnmpInterface.if_index == index))
            iface.starred = on
    run(go())


def rules(**changes) -> None:  # type: ignore[no-untyped-def]
    async def go():
        async with D.session_scope() as s:
            current = await snmp_alerts.load(s)
            current.update(changes)
            await D.save_setting(s, snmp_alerts.SETTING, current)
    run(go())


def mute_device() -> None:
    async def go():
        async with D.session_scope() as s:
            s.add(AlertMute(device_id=1))
    run(go())


class TestDeviceThresholds:
    def test_cpu_over_the_line_for_the_whole_time_alerts_once_then_recovers(self, db):
        for minute in range(10):
            poll(minute, cpu=95)
        assert kinds() == [], "nine minutes is not ten"
        poll(10, cpu=96)
        for minute in range(11, 15):
            poll(minute, cpu=97)
        assert kinds() == ["cpu_high"], "once, not every poll after"
        first = sent()[0]
        assert first.subject == "core-switch: CPU is high (96%)" and first.tone == "bad"
        assert "`10.0.0.2`" in first.body and "over 90% for 10 minutes" in first.body

        poll(15, cpu=88)            # under the line, not by the margin
        assert kinds() == ["cpu_high"]
        poll(16, cpu=80)
        assert kinds() == ["cpu_high", "cpu_ok"]
        assert sent()[1].subject == "core-switch: CPU back to normal (80%)"
        assert "was over 90% for 16m 0s" in sent()[1].body

    def test_one_dip_starts_the_count_again(self, db):
        for minute in range(5):
            poll(minute, cpu=95)
        poll(5, cpu=50)
        for minute in range(6, 15):
            poll(minute, cpu=95)
        assert kinds() == []
        poll(16, cpu=95)
        assert kinds() == ["cpu_high"]

    def test_hovering_on_the_line_does_not_flap(self, db):
        for minute in range(11):
            poll(minute, cpu=95)
        for minute in range(11, 30):
            poll(minute, cpu=89 if minute % 2 else 91)
        assert kinds() == ["cpu_high"]

    def test_time_it_could_not_see_does_not_count(self, db):
        for minute in range(3):
            poll(minute, cpu=95)
        poll(12, cpu=95)             # ten minutes silent, then still high
        assert kinds() == [], "the streak restarted at minute 12"
        poll(22, cpu=95)
        assert kinds() == []

        async def state():
            async with D.session_scope() as s:
                return await s.get(AlertState, "cpu:1")
        assert run(state()).since == T0 + timedelta(minutes=22)

    def test_temperature_and_memory(self, db):
        for minute in range(6):
            poll(minute, temp=85.0, memory=95)
        assert kinds() == ["temperature_high"]
        assert sent()[0].subject == "core-switch: Temperature is running hot (85°C)"
        for minute in range(6, 11):
            poll(minute, temp=85.0, memory=95)
        assert kinds() == ["temperature_high", "memory_high"]

    def test_a_metric_the_device_does_not_report_is_left_alone(self, db):
        for minute in range(11):
            poll(minute, cpu=95)
        poll(11, cpu=None)
        assert kinds() == ["cpu_high"]
        poll(12, cpu=20)
        assert kinds() == ["cpu_high", "cpu_ok"]

    def test_thresholds_come_from_settings(self, db):
        rules(cpu_percent=50, cpu_minutes=2)
        for minute in range(3):
            poll(minute, cpu=60)
        assert kinds() == ["cpu_high"]
        assert "over 50% for 2 minutes" in sent()[0].body

    def test_a_rule_switched_off_sends_nothing_either_way(self, db):
        rules(cpu=False)
        for minute in range(12):
            poll(minute, cpu=95)
        rules(cpu=True)
        poll(12, cpu=95)
        poll(13, cpu=10)
        assert kinds() == [], "no alert, and so no recovery for it"


class TestStarredPorts:
    def test_only_a_starred_port_alerts_when_it_goes_down(self, db):
        poll(0, ports=[port(1), port(2)])
        star(1)
        poll(1, ports=[port(1, oper="down"), port(2, oper="down")])
        assert kinds() == [], "one poll down is not enough"
        poll(2, ports=[port(1, oper="down"), port(2, oper="down")])
        assert kinds() == ["port_down"]
        assert sent()[0].subject == "core-switch: port port1 is down"
        assert "link down" in sent()[0].body
        poll(3, ports=[port(1), port(2)])
        assert kinds() == ["port_down", "port_up"]
        assert sent()[1].subject == "core-switch: port port1 is back up"
        assert "down for 2m 0s" in sent()[1].body

    def test_no_back_up_for_a_down_nobody_was_told_about(self, db):
        rules(port_down=False)
        poll(0, ports=[port(1)])
        star(1)
        poll(1, ports=[port(1, oper="down")])
        poll(2, ports=[port(1, oper="down")])
        rules(port_down=True)
        poll(3, ports=[port(1)])
        assert kinds() == []

    def test_a_quick_bounce_is_not_news(self, db):
        poll(0, ports=[port(1)])
        star(1)
        poll(1, ports=[port(1, oper="down")])
        poll(2, ports=[port(1)])
        poll(3, ports=[port(1, oper="down")])
        poll(4, ports=[port(1)])
        assert kinds() == []

    def test_switched_off_on_the_device_says_so(self, db):
        poll(0, ports=[port(1)])
        star(1)
        for minute in (1, 2):
            poll(minute, ports=[port(1, oper="down", admin="down")])
        assert "switched off on the device" in sent()[0].body

    def test_a_starred_port_that_stays_busy(self, db):
        star_after = True
        rate = 900e6 / 8 * 60       # octets per minute at 900 Mbps
        for minute in range(12):
            poll(minute, ports=[port(1, octets=int(rate * minute))])
            if star_after and minute == 0:
                star(1)
                star_after = False
        # The first rate needs two readings, so ten busy minutes end at minute 11.
        assert kinds() == ["port_busy"]
        assert sent()[0].subject == "core-switch: port port1 is busy (90% of 1 Gbps)"
        assert "In 900 Mbps" in sent()[0].body
        poll(12, ports=[port(1, octets=int(rate * 11) + 1000)])
        assert kinds() == ["port_busy", "port_calm"]

    def test_an_unstarred_port_never_alerts_busy(self, db):
        rate = 950e6 / 8 * 60
        for minute in range(15):
            poll(minute, ports=[port(1, octets=int(rate * minute))])
        assert kinds() == []

    def test_utilisation(self):
        iface = SnmpInterface(speed_mbps=100, in_bps=20e6, out_bps=80e6)
        assert snmp_alerts.utilisation(iface) == pytest.approx(80.0)
        assert snmp_alerts.utilisation(SnmpInterface(speed_mbps=0, in_bps=1, out_bps=1)) is None
        assert snmp_alerts.utilisation(SnmpInterface(speed_mbps=100, in_bps=None, out_bps=1)) is None


class TestMuted:
    def test_a_muted_device_sends_nothing(self, db):
        mute_device()
        poll(0, ports=[port(1)])
        star(1)
        for minute in range(1, 12):
            poll(minute, cpu=95, ports=[port(1, oper="down")])
        assert kinds() == []

    def test_unmuting_mid_alert_sends_no_recovery_for_a_silent_alert(self, db):
        mute_device()
        for minute in range(11):
            poll(minute, cpu=95)

        async def unmute():
            async with D.session_scope() as s:
                for row in (await s.execute(select(AlertMute))).scalars():
                    await s.delete(row)
        run(unmute())
        poll(11, cpu=10)
        assert kinds() == []


# --------------------------------------------------------------------------
# The pages
# --------------------------------------------------------------------------


@pytest.fixture
def site(db):
    poll(0, ports=[port(1), port(2, oper="down")])

    async def target():
        async with D.session_scope() as s:
            s.add(Target(name="nas ping", check_type=CheckType.PING, address="10.0.0.5"))
    run(target())
    with TestClient(create_app(db), follow_redirects=False) as client:
        client.post("/setup", data={"setup_code": client.app.state.setup_code, "username": "admin", "password": PASSWORD,
                                    "password_confirm": PASSWORD, "timezone": "UTC"})
        yield client


def _iface(index: int) -> SnmpInterface:
    async def go():
        async with D.session_scope() as s:
            return await s.scalar(select(SnmpInterface).where(SnmpInterface.if_index == index))
    return run(go())


def _mutes() -> list[AlertMute]:
    async def go():
        async with D.session_scope() as s:
            return list((await s.execute(select(AlertMute))).scalars())
    return run(go())


class TestPages:
    def test_star_a_port_from_the_device_page(self, site):
        page = site.get("/devices/1?range=24h").text
        assert page.count('class="star"') == 2 and 'aria-pressed="false"' in page
        iface = _iface(1)
        response = site.post(f"/devices/1/interfaces/{iface.id}/star",
                             data={"starred": "1", "back": "/devices/1?range=24h"})
        assert response.headers["location"] == "/devices/1?range=24h"
        assert _iface(1).starred
        page = site.get("/devices/1").text
        assert 'class="star on"' in page
        # Unstar from the global list in Settings.
        assert "port1" in site.get("/alerts/rules").text
        site.post(f"/settings/alerts/ports/{iface.id}/unstar")
        assert not _iface(1).starred

    def test_a_starred_port_that_is_down_shows_red(self, site):
        site.post(f"/devices/1/interfaces/{_iface(2).id}/star", data={"starred": "1"})
        page = site.get("/devices/1").text
        assert '<span class="pill bad">down</span>' in page

    def test_star_only_through_its_own_device_and_back_only_to_it(self, site):
        iface = _iface(1)
        response = site.post(f"/devices/2/interfaces/{iface.id}/star", data={"starred": "1"})
        assert not _iface(1).starred
        response = site.post(f"/devices/1/interfaces/{iface.id}/star",
                             data={"starred": "1", "back": "https://evil.example/"})
        assert response.headers["location"] == "/devices/1"

    def test_mute_and_unmute_a_device_from_its_page(self, site):
        site.post("/devices/1/mute", data={"muted": "1"})
        assert [m.device_id for m in _mutes()] == [1]
        page = site.get("/devices/1").text
        assert "alerts muted" in page and "Unmute alerts" in page
        site.post("/devices/1/mute", data={"muted": "1"})
        assert len(_mutes()) == 1, "twice is still once"
        site.post("/devices/1/mute", data={"muted": "0"})
        assert _mutes() == []

    def test_the_global_mute_list(self, site):
        page = site.get("/alerts/rules").text
        assert "Nothing is muted." in page and 'name="device_id"' in page and 'name="target_id"' in page
        site.post("/settings/alerts/mute", data={"device_id": "1"})
        site.post("/settings/alerts/mute", data={"target_id": "1"})
        site.post("/settings/alerts/mute", data={"target_id": "99999"})
        mutes = _mutes()
        assert {(m.device_id, m.target_id) for m in mutes} == {(1, None), (None, 1)}
        page = site.get("/alerts/rules").text
        assert "core-switch" in page and "nas ping" in page and "Unmute" in page
        for m in mutes:
            site.post(f"/settings/alerts/mute/{m.id}/delete")
        assert _mutes() == []

    def test_rules_are_saved_and_checked(self, site):
        form = {"port_down": "1", "port_busy": "1", "port_busy_percent": "70",
                "port_busy_minutes": "5", "cpu": "1", "cpu_percent": "85", "cpu_minutes": "3",
                "memory_percent": "90", "memory_minutes": "10", "temperature": "1",
                "temperature_celsius": "75", "temperature_minutes": "5",
                "pool_health": "1", "pool_space": "1", "pool_space_percent": "80",
                "disk_space_percent": "95", "drive_temperature": "1", "drive_celsius": "48",
                "drive_minutes": "15"}
        response = site.post("/settings/alerts/rules", data=form)
        assert response.headers["location"] == "/alerts/rules?saved=rules"
        assert "Alert rules saved." in site.get("/alerts/rules?saved=rules").text

        async def saved():
            async with D.session_scope() as s:
                return await snmp_alerts.load(s)
        r = run(saved())
        assert (r["cpu_percent"], r["cpu_minutes"], r["memory"], r["port_busy_percent"]) == (85, 3, False, 70)
        assert (r["pool_health"], r["pool_space_percent"], r["disk_space"], r["disk_space_percent"],
                r["drive_celsius"], r["drive_minutes"]) == (True, 80, False, 95, 48, 15)
        response = site.post("/settings/alerts/rules", data={**form, "drive_celsius": "10"})
        assert response.status_code == 400 and "Drive temperature must be between 30 and 120." in response.text

        response = site.post("/settings/alerts/rules", data={**form, "cpu_percent": "150"})
        assert response.status_code == 400 and "CPU must be between 1 and 100." in response.text
        response = site.post("/settings/alerts/rules", data={**form, "temperature_celsius": "hot"})
        assert response.status_code == 400 and "whole number" in response.text
        assert run(saved())["cpu_percent"] == 85, "nothing saved on an error"


def test_migration_9_from_a_version_8_database():
    """Column added, tables created, and the result matches a fresh install."""
    from sqlalchemy import inspect, text

    async def shape(wind_back: bool):
        config = _config()
        D.init_engine(config)
        await D.init_db(config)
        if wind_back:
            async with D.session_scope() as s:
                await s.execute(text("DROP TABLE alert_mute"))
                await s.execute(text("DROP TABLE alert_state"))
                await s.execute(text("ALTER TABLE snmp_interface DROP COLUMN starred"))
                await D._set_version(s, 8)
            await D.close_engine()
            D.init_engine(config)
            await D.init_db(config)
        async with D.session_scope() as s:
            connection = await s.connection()
            out = await connection.run_sync(lambda sync: {
                t: sorted(c["name"] for c in inspect(sync).get_columns(t))
                for t in ("snmp_interface", "alert_mute", "alert_state")
            })
            version = await D._get_version(s)
        await D.close_engine()
        return out, version

    fresh = run(shape(False))
    migrated = run(shape(True))
    assert migrated == fresh and fresh[1] == D.CURRENT_VERSION >= 9
    assert "starred" in fresh[0]["snmp_interface"]
