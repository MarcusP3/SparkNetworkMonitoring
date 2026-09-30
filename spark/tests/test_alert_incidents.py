"""Incidents for alerts, not only outages: SNMP thresholds, ports, storage,
API credentials, drives, TrueNAS's alerts, and SNMP going quiet.

An incident is open while its rule stands fired, whether or not a message
went out (a muted device still had the problem), and closes when the rule
clears. The dashboard lists them beside target outages.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from spark import alerts, snmp_alerts
from spark import db as D
from spark.main import create_app
from spark.models import AlertIncident, CheckType, Incident, Target, utcnow

from test_snmp_alerts import (PASSWORD, T0, db, kinds, mute_device, poll, port,  # noqa: F401
                              rules, run, star)


def incidents() -> list[AlertIncident]:
    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            return list((await s.execute(select(AlertIncident).order_by(AlertIncident.id))).scalars())
    return run(go())


def cpu_high(until: int) -> None:
    """CPU at 95% a minute apart: the rule's 10 minutes are up at minute 10."""
    for minute in range(until + 1):
        poll(minute, cpu=95)


class TestRecorded:
    def test_open_while_it_stands_and_closed_when_it_clears(self, db):
        cpu_high(12)
        (row,) = incidents()
        assert row.key == "cpu:1" and row.device_id == 1 and row.closed_at is None
        assert row.title == "core-switch: CPU is high (95%)"
        assert row.detail == "10.0.0.2 — over 90% for 10 minutes.", "no Discord backticks"
        assert row.opened_at == T0, "from when the streak began, as the alert says"
        poll(13, cpu=50)
        (row,) = incidents()
        assert row.closed_at == T0 + timedelta(minutes=13) and row.resolution == "recovered"

    def test_nothing_before_it_fires(self, db):
        cpu_high(5)
        assert incidents() == []

    def test_a_muted_device_still_has_the_incident(self, db):
        mute_device()
        cpu_high(11)
        assert "cpu_high" not in kinds()
        assert len(incidents()) == 1

    def test_a_rule_switched_off_records_nothing(self, db):
        rules(cpu=False)
        cpu_high(11)
        assert incidents() == []

    def test_an_alert_that_fired_before_incidents_existed_gets_one(self, db):
        cpu_high(11)

        async def lose():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                await s.execute(AlertIncident.__table__.delete())
        run(lose())
        poll(12, cpu=95)
        (row,) = incidents()
        assert row.opened_at == T0 and row.closed_at is None

    def test_a_port_unstarred_while_down_is_no_longer_watched(self, db):
        poll(0, ports=[port(1)])
        star(1)
        for minute in (1, 2, 3):
            poll(minute, ports=[port(1, oper="down")])
        (row,) = incidents()
        assert row.title == "core-switch: port port1 is down" and row.closed_at is None

        async def unstar():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                await snmp_alerts.forget(s, "port:1", "busy:1")
        run(unstar())
        (row,) = incidents()
        assert row.closed_at is not None and row.resolution == "no longer watched"

    def test_one_open_incident_per_rule(self, db):
        cpu_high(20)
        assert len(incidents()) == 1


class TestSnmpQuiet:
    def _poll(self, minutes: float, *, answered: bool, muted=False):  # type: ignore[no-untyped-def]
        async def go():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                await alerts.on_snmp_poll(
                    s, row_id=1, device_id=1, device_name="core-switch", address="10.0.0.2",
                    answered=answered, previous_ok=T0, was_failing=not answered or minutes > 0,
                    now=T0 + timedelta(minutes=minutes), interval=60, error="No response.")
        run(go())

    def test_quiet_opens_and_answering_closes(self, db):
        self._poll(1, answered=False)
        assert incidents() == [], "one missed poll is not an outage"
        self._poll(5, answered=False)
        (row,) = incidents()
        assert row.key == "snmpdown:1" and row.title == "core-switch stopped answering SNMP"
        self._poll(6, answered=False)
        assert len(incidents()) == 1
        self._poll(7, answered=True)
        (row,) = incidents()
        assert row.closed_at == T0 + timedelta(minutes=7)

    def test_muted_still_recorded(self, db):
        mute_device()
        self._poll(5, answered=False)
        assert len(incidents()) == 1 and "snmp_down" not in kinds()

    def test_a_down_target_covers_it(self, db):
        async def target():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                s.add(Target(name="switch ping", check_type=CheckType.PING, address="10.0.0.2",
                             device_id=1, status="down"))
        run(target())
        self._poll(5, answered=False)
        assert incidents() == []


class TestDashboard:
    def _site(self, config):  # type: ignore[no-untyped-def]
        client = TestClient(create_app(config), follow_redirects=False)
        client.__enter__()
        client.post("/setup", data={"setup_code": client.app.state.setup_code, "username": "admin", "password": PASSWORD,
                                    "password_confirm": PASSWORD, "timezone": "UTC"})
        return client

    def test_outages_and_alerts_together(self, db):
        cpu_high(11)

        async def outage():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                t = Target(name="plex web", check_type=CheckType.TCP, address="10.0.0.9:32400")
                s.add(t)
                await s.flush()
                s.add(Incident(target_id=t.id, opened_at=T0 - timedelta(hours=1), cause="Connection refused"))
                s.add(AlertIncident(key="tnalert:1:x", device_id=None, title="nas: SMART failure",
                                    detail="192.168.1.20 — TrueNAS Critical (SMART).",
                                    opened_at=T0 - timedelta(days=1),
                                    closed_at=T0 - timedelta(hours=20), resolution="no longer watched"))
        run(outage())
        client = self._site(db)
        try:
            page = " ".join(client.get("/").text.split())
        finally:
            client.__exit__(None, None, None)
        assert "Outages, and SNMP, storage and API alerts." in page
        assert '<span class="stat-value">2</span> <span class="stat-note">Still unresolved</span>' in page
        page = page[page.index("<h2>Recent incidents</h2>"):]
        plex = page.index("<strong>plex web</strong>")
        cpu = page.index('<strong><a href="/devices/1">core-switch: CPU is high (95%)</a></strong>')
        smart = page.index("<strong>nas: SMART failure</strong>")
        assert cpu < plex < smart, "newest first, whatever the kind"
        assert '<span class="pill neutral">Target</span>' in page
        assert ('<span class="detail">10.0.0.2 — over 90% for 10 minutes.</span> <span class="detail"> '
                '<span class="pill neutral">SNMP</span> <span class="pill bad dot">ongoing</span>') in page
        assert '<span class="pill neutral">TrueNAS</span> Lasted 4h' in page
        assert "no longer watched</span>" in page


def test_sources():
    from spark.web.routes_dashboard import source_of
    assert [source_of(k) for k in ("cpu:1", "port:3", "space:1:pool:tank", "api:2", "apidrive:2:sda",
                                   "tnalert:2:u", "snmpdown:1", "mystery:1")] == \
        ["SNMP", "Port", "Storage", "API", "TrueNAS", "TrueNAS", "SNMP", "Alert"]


def test_migration_17_from_a_version_16_database():
    from sqlalchemy import inspect, text

    from test_snmp_alerts import _config

    async def shape(wind_back: bool):  # type: ignore[no-untyped-def]
        config = _config()
        D.init_engine(config)
        await D.init_db(config)
        if wind_back:
            async with D.session_scope() as s:
                await s.execute(text("DROP TABLE alert_incident"))
                await D._set_version(s, 16)
            await D.close_engine()
            D.init_engine(config)
            await D.init_db(config)
        async with D.session_scope() as s:
            connection = await s.connection()
            columns = await connection.run_sync(
                lambda sync: sorted(c["name"] for c in inspect(sync).get_columns("alert_incident")))
            version = await D._get_version(s)
        await D.close_engine()
        return columns, version

    assert run(shape(True)) == run(shape(False))


