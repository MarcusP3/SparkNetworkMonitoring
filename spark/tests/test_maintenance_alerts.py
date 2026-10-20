"""Maintenance windows and the alerts that are not outages: SNMP thresholds,
a device that stops answering SNMP, and the internet (whose window is the
gateway's). Held while the window is on, sent when it ends if still
standing, and their recoveries follow. Outages are in test_maintenance.py.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from spark import alerts, maintenance
from spark import db as D
from spark.models import AlertIncident, Device, DeviceRole, MaintenanceWindow, utcnow

from test_snmp_alerts import T0, db, kinds, mute_device, poll, run, sent  # noqa: F401


def window(start, end, device_id=1) -> None:  # type: ignore[no-untyped-def]
    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            s.add(MaintenanceWindow(device_id=device_id, starts_at=start, ends_at=end))
    run(go())


def finish(when) -> None:  # type: ignore[no-untyped-def]
    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            await maintenance.finish(s, when)
    run(go())


def incidents() -> list[AlertIncident]:
    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            return list((await s.execute(select(AlertIncident).order_by(AlertIncident.id))).scalars())
    return run(go())


class TestThresholds:
    def test_held_then_sent_at_the_end_then_its_recovery(self, db):
        window(T0, T0 + timedelta(minutes=15))
        for minute in range(13):
            poll(minute, cpu=95)
        assert kinds() == [], "it fired during maintenance"
        (row,) = incidents()
        assert row.key == "cpu:1" and row.closed_at is None
        poll(14, cpu=95)
        finish(T0 + timedelta(minutes=16))
        (left,) = sent()
        assert left.kind == "maintenance_left" and left.subject == "core-switch: CPU is high (95%)"
        assert "Maintenance on core-switch has ended." in left.body
        finish(T0 + timedelta(minutes=17))
        assert kinds() == ["maintenance_left"], "once"
        poll(18, cpu=50)
        assert kinds() == ["maintenance_left", "cpu_ok"], "and it says when it is over"

    def test_cleared_during_it_is_nothing(self, db):
        window(T0, T0 + timedelta(minutes=15))
        for minute in range(12):
            poll(minute, cpu=95)
        poll(12, cpu=50)
        finish(T0 + timedelta(minutes=16))
        assert kinds() == []

    def test_after_it_alerts_as_usual(self, db):
        window(T0 - timedelta(hours=2), T0 - timedelta(hours=1))
        for minute in range(11):
            poll(minute, cpu=95)
        assert kinds() == ["cpu_high"]


class TestSnmpSilence:
    def _poll(self, minutes: float, *, answered: bool) -> None:
        async def go():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                await alerts.on_snmp_poll(
                    s, row_id=1, device_id=1, device_name="core-switch", address="10.0.0.2",
                    answered=answered, previous_ok=T0, was_failing=True,
                    now=T0 + timedelta(minutes=minutes), interval=60, error="No response.")
        run(go())

    def test_held_then_sent_under_its_own_key_so_the_return_is_sent_too(self, db):
        window(T0, T0 + timedelta(minutes=10))
        self._poll(5, answered=False)
        assert kinds() == [] and len(incidents()) == 1
        finish(T0 + timedelta(minutes=11))
        (down,) = sent()
        assert down.kind == "snmp_down" and down.subject == "core-switch stopped answering SNMP"
        assert down.dedupe_key == f"snmp:1:{T0.isoformat()}:down"
        self._poll(12, answered=True)
        assert kinds() == ["snmp_down", "snmp_up"]

    def test_a_muted_device_is_not_told_at_the_end_either(self, db):
        mute_device()
        window(T0, T0 + timedelta(minutes=10))
        self._poll(5, answered=False)
        finish(T0 + timedelta(minutes=11))
        assert kinds() == []


# --------------------------------------------------------------------------
# The internet: the gateway's window
# --------------------------------------------------------------------------


@pytest.fixture
def lan():
    from test_credentials import _config

    config = _config()

    async def setup():  # type: ignore[no-untyped-def]
        D.init_engine(config)
        await D.init_db(config)
        async with D.session_scope() as s:
            s.add(Device(mac="aa:00:00:00:00:01", primary_ip="192.168.1.1", friendly_name="router",
                         role=DeviceRole.GATEWAY, last_seen=utcnow()))
    run(setup())
    yield config
    run(D.close_engine())


class TestInternet:
    def test_rebooting_the_router_is_not_an_outage_until_it_is(self, lan):
        from test_credentials import _sent
        from test_internet import DOWN, UP, record

        now = utcnow()
        window(now - timedelta(minutes=1), now + timedelta(minutes=10))
        record(DOWN, 0)
        record(DOWN, 1)
        assert run(_sent()) == [], "the gateway is under maintenance"
        finish(now + timedelta(minutes=11))
        assert [k for k, *_ in run(_sent())] == ["maintenance_left"]
        record(UP, 12)
        assert [k for k, *_ in run(_sent())] == ["maintenance_left", "internet_ok"]
