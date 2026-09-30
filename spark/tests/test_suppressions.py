"""Suppressions: one rule, for one device, off or with its own line.

The example that asked for them: ZFS keeps TrueNAS's memory nearly full on
purpose, so "memory over 90%" is always true there and never news. A
suppression must be fully quiet (no message, no incident), leave every other
rule and device alone, and start the rule afresh when saved or removed.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from spark import db as D
from spark import suppressions
from spark.main import create_app
from spark.models import AlertIncident, AlertState, AlertSuppression

from test_snmp_alerts import PASSWORD, db, kinds, poll, run  # noqa: F401


def incidents():  # type: ignore[no-untyped-def]
    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            return [(r.key, r.closed_at is None, r.resolution) for r in
                    (await s.execute(select(AlertIncident).order_by(AlertIncident.id))).scalars()]
    return run(go())


def save(**form):  # type: ignore[no-untyped-def]
    args = {"device_id": 1, "rule": "memory", "mode": "off", "threshold": "", "detail": ""} | form

    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            return (await suppressions.save(s, **args)).id
    return run(go())


def remove(ident):  # type: ignore[no-untyped-def]
    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            await suppressions.remove(s, await s.get(AlertSuppression, ident))
    run(go())


def memory(start, end, value=99.0, cpu=10.0):  # type: ignore[no-untyped-def]
    for minute in range(start, end + 1):
        poll(minute, memory=value, cpu=cpu)


class TestQuiet:
    def test_off_is_silent_and_leaves_the_rest(self, db):
        save()
        memory(0, 12, cpu=95)
        assert "memory_high" not in kinds() and "cpu_high" in kinds()
        assert [k for k, _, _ in incidents()] == ["cpu:1"], "no memory incident either"

    def test_its_own_line(self, db):
        save(mode="line", threshold="100")
        memory(0, 12)
        assert "memory_high" not in kinds()
        save(mode="line", threshold="95")          # changed, not added twice
        memory(13, 25)
        assert kinds().count("memory_high") == 1

        async def count():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                return len((await s.execute(select(AlertSuppression))).all())
        assert run(count()) == 1

    def test_saving_closes_what_stands_and_removing_starts_afresh(self, db):
        memory(0, 12)
        assert kinds().count("memory_high") == 1 and incidents() == [("memory:1", True, None)]
        ident = save()
        assert incidents() == [("memory:1", False, "suppressed")]

        async def state():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                return await s.get(AlertState, "memory:1")
        assert run(state()) is None
        memory(13, 30)
        assert kinds().count("memory_high") == 1, "quiet while suppressed"
        assert "memory_ok" not in kinds(), "no recovery for a suppression"
        remove(ident)
        memory(31, 45)
        assert kinds().count("memory_high") == 2, "back to the global line, as new"

    def test_a_line_raised_over_a_standing_alert_does_not_leave_it_hanging(self, db):
        memory(0, 12)
        save(mode="line", threshold="100")
        memory(13, 20)
        assert incidents() == [("memory:1", False, "suppressed")]


class TestRefused:
    @pytest.mark.parametrize("form, message", [
        ({"device_id": None}, "Choose a device"),
        ({"device_id": 99}, "Choose a device"),
        ({"rule": "coffee"}, "Choose which alert"),
        ({"rule": "port_down", "mode": "line", "threshold": "5"}, "can only be off"),
        ({"mode": "line", "threshold": "0"}, "between 1 and 100%"),
        ({"mode": "line", "threshold": "ninety"}, "whole number"),
        ({"rule": "temperature", "mode": "line", "threshold": "200"}, "between 30 and 120°C"),
        ({"mode": "sideways"}, "Choose off, or its own line"),
    ])
    def test_refusals(self, db, form, message):
        with pytest.raises(suppressions.SuppressionError, match=message):
            save(**form)

    def test_a_type_only_for_truenas_alerts(self, db):
        ident = save(detail="PoolUSBDisks")

        async def row():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                return await s.get(AlertSuppression, ident)
        assert run(row()).detail is None


class TestKeys:
    def test_rule_of(self):
        assert [suppressions.rule_of(k) for k in (
            "cpu:1", "memory:1", "temperature:1", "port:4", "busy:4", "snmpdown:1",
            "pool:1:tank", "space:1:pool:tank", "space:1:fs:/", "drive:1:sda", "api:2",
            "apidrive:2:sda", "tnalert:2:u", "nope:1")] == [
            "cpu", "memory", "temperature", "port_down", "port_busy", "snmp_down",
            "pool_health", "pool_space", "disk_space", "drive_temperature", "api_down",
            "drive_errors", "truenas_alerts", None]

    def test_describe(self):
        assert suppressions.describe(AlertSuppression(rule="memory", threshold=100.0)) == \
            "Memory: alert over 100%"
        assert suppressions.describe(AlertSuppression(rule="truenas_alerts", detail="PoolUSBDisks")) == \
            "TrueNAS alert PoolUSBDisks: off"
        assert suppressions.describe(AlertSuppression(rule="truenas_alerts")) == \
            "Every TrueNAS alert: off"


# --------------------------------------------------------------------------
# Settings -> Suppressions
# --------------------------------------------------------------------------


@pytest.fixture
def site(db):
    with TestClient(create_app(db), follow_redirects=False) as client:
        client.post("/setup", data={"setup_code": client.app.state.setup_code, "username": "admin", "password": PASSWORD,
                                    "password_confirm": PASSWORD, "timezone": "UTC"})
        yield client


def flat(html: str) -> str:
    return " ".join(html.split())


class TestPage:
    def test_the_tab_and_prefill(self, site):
        page = flat(site.get("/settings/suppressions?device=1&rule=memory").text)
        assert '<a href="/settings/suppressions" class="active" aria-current="page"> <span>Suppressions</span>' in page
        assert "Nothing suppressed." in page
        assert '<option value="1" selected>core-switch — 10.0.0.2</option>' in page
        assert '<option value="memory" selected>Memory (everywhere: 90%)</option>' in page
        assert '<form method="post" action="/settings/suppressions" class="supp-form">' in page
        assert '<label class="stack-field supp-detail">' in page, "there with JS off; hidden by class"
        assert "document.documentElement.classList.add('js-supp')" in page
        page = flat(site.get("/settings/suppressions?device=1&rule=truenas_alerts").text)
        assert '<form method="post" action="/settings/suppressions" class="supp-form is-truenas">' in page

    def test_save_list_and_remove(self, site):
        response = site.post("/settings/suppressions", data={
            "device_id": "1", "rule": "memory", "mode": "line", "threshold": "100"})
        assert response.status_code == 303 and response.headers["location"].startswith(
            "/settings/suppressions?saved=1#supp-")
        page = flat(site.get(response.headers["location"].split("#")[0]).text)
        assert "Saved. That alert starts afresh for this device." in page
        assert '<td>Memory: alert over 100%</td>' in page
        assert '<span>Suppressions</span> <span class="subnav-hint">1</span>' in page
        site.post("/settings/suppressions/1/delete")
        assert "Nothing suppressed." in site.get("/settings/suppressions").text

    def test_a_refusal_keeps_what_was_chosen(self, site):
        response = site.post("/settings/suppressions", data={
            "device_id": "1", "rule": "port_down", "mode": "line", "threshold": "5"})
        assert response.status_code == 400
        page = flat(response.text)
        assert "can only be off" in page and '<option value="port_down" selected>' in page

    def test_dashboard_incident_offers_suppress(self, site):
        memory(0, 12)
        page = flat(site.get("/").text)
        assert ('<a href="/settings/suppressions?device=1&amp;rule=memory#add" class="suppress-link"'
                in page)


def test_migration_18_from_a_version_17_database():
    from sqlalchemy import inspect, text

    from test_snmp_alerts import _config

    async def shape(wind_back: bool):  # type: ignore[no-untyped-def]
        config = _config()
        D.init_engine(config)
        await D.init_db(config)
        if wind_back:
            async with D.session_scope() as s:
                await s.execute(text("DROP TABLE alert_suppression"))
                await D._set_version(s, 17)
            await D.close_engine()
            D.init_engine(config)
            await D.init_db(config)
        async with D.session_scope() as s:
            connection = await s.connection()
            columns = await connection.run_sync(
                lambda sync: sorted(c["name"] for c in inspect(sync).get_columns("alert_suppression")))
            version = await D._get_version(s)
        await D.close_engine()
        return columns, version

    assert run(shape(True)) == run(shape(False))


# --------------------------------------------------------------------------
# The other rules: SNMP going quiet, storage, the TrueNAS API
# --------------------------------------------------------------------------


def test_snmp_quiet_can_be_off(db):
    from datetime import timedelta

    from spark import alerts
    from test_snmp_alerts import T0

    save(rule="snmp_down")

    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            await alerts.on_snmp_poll(
                s, row_id=1, device_id=1, device_name="core-switch", address="10.0.0.2",
                answered=False, previous_ok=T0, was_failing=True,
                now=T0 + timedelta(minutes=10), interval=60, error=None)
    run(go())
    assert incidents() == [] and "snmp_down" not in kinds()


def test_storage_lines(db):
    from datetime import timedelta

    from spark import storage
    from spark.collectors.snmp import PoolReading
    from test_snmp_alerts import T0

    save(rule="pool_space", mode="line", threshold="95")
    TB = 10**12

    async def read(minute, pct):  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            await storage.store(s, 1, pools=[PoolReading("tank", "ONLINE", int(10 * TB * pct / 100), 10 * TB)],
                                drives=[], filesystems=[])
            await storage.evaluate(s, 1, T0 + timedelta(minutes=minute))
    for minute in (0, 5, 10):
        run(read(minute, 90))
    assert "pool_space_bad" not in kinds(), "90% is under this device's 95"
    for minute in (15, 20):
        run(read(minute, 96))
    assert kinds().count("pool_space_bad") == 1


class TestTrueNAS:
    """Over the API, with the fake TrueNAS from the credential tests."""

    @pytest.fixture
    def nas(self):  # type: ignore[no-untyped-def]
        from test_credentials import FakeTrueNAS
        from test_truenas_health import real

        server = FakeTrueNAS()
        server.start()
        server.answers = real()
        return server

    def _setup(self, db, nas):  # type: ignore[no-untyped-def]
        from test_credentials import _trusted
        return run(_trusted(db, nas.host))

    def _check(self, db, ident):  # type: ignore[no-untyped-def]
        from spark import credentials
        return run(credentials.check_one(db, ident))

    def test_one_type_off_the_rest_still_sent(self, db, nas):
        from test_truenas_health import ALERTS, SMART

        nas.answers["alert.list"] = [SMART] + ALERTS
        save(rule="truenas_alerts", detail="PoolUSBDisks")
        ident = self._setup(db, nas)
        self._check(db, ident)
        tn = [x for x in incidents() if x[0].startswith("tnalert:")]
        assert tn == [(f"tnalert:{ident}:a-smart", True, None)], "SMART, not the USB warning"
        assert kinds().count("truenas_alert_bad") == 1

    def test_every_type_off(self, db, nas):
        from test_truenas_health import ALERTS, SMART

        nas.answers["alert.list"] = [SMART] + ALERTS
        save(rule="truenas_alerts")
        ident = self._setup(db, nas)
        self._check(db, ident)
        assert "truenas_alert_bad" not in kinds()

    def test_suppressing_a_standing_type_closes_it_and_only_it(self, db, nas):
        from test_truenas_health import ALERTS, SMART

        nas.answers["alert.list"] = [SMART] + ALERTS
        ident = self._setup(db, nas)
        self._check(db, ident)
        assert (f"tnalert:{ident}:a-usb", True, None) in incidents()
        save(rule="truenas_alerts", detail="PoolUSBDisks")
        assert (f"tnalert:{ident}:a-usb", False, "suppressed") in incidents()
        assert (f"tnalert:{ident}:a-smart", True, None) in incidents(), "SMART untouched"
        self._check(db, ident)
        assert kinds().count("truenas_alert_bad") == 2, "neither sent again"

    def test_drive_errors_off(self, db, nas):
        from test_truenas_health import _pools

        save(rule="drive_errors")
        nas.answers["pool.query"] = _pools(c=3)
        ident = self._setup(db, nas)
        self._check(db, ident)
        assert "drive_health_bad" not in kinds()

    def test_the_dashboard_suppress_link_names_the_type(self, db, nas, site):
        ident = self._setup(db, nas)
        self._check(db, ident)
        page = flat(site.get("/").text)
        assert ('href="/settings/suppressions?device=1&amp;rule=truenas_alerts&amp;detail=PoolUSBDisks#add"'
                in page)
        page = flat(site.get("/settings/suppressions?device=1&rule=truenas_alerts&detail=PoolUSBDisks").text)
        assert 'value="PoolUSBDisks"' in page and '<option value="PoolUSBDisks">' in page

    def test_api_down_off(self, db, nas):
        from test_credentials import _set_key

        save(rule="api_down")
        ident = self._setup(db, nas)
        run(_set_key(db, ident, "1-revoked"))
        for _ in range(3):
            self._check(db, ident)
        assert "api_down" not in kinds()
        assert not [k for k, _, _ in incidents() if k.startswith("api:")]


# --------------------------------------------------------------------------
# The dashboard leaves suppressed alerts out
# --------------------------------------------------------------------------


def recent(site):  # type: ignore[no-untyped-def]
    page = flat(site.get("/").text)
    return page[page.index("<h2>Recent incidents</h2>"):]


def open_tile(site) -> str:  # type: ignore[no-untyped-def]
    page = flat(site.get("/").text)
    at = page.index('<span class="stat-name">Open incidents</span>')
    return page[at:at + 120]


class TestDashboardHides:
    def test_suppressing_takes_it_off_the_list(self, site):
        memory(0, 12, cpu=95)
        assert "core-switch: Memory is high (99%)" in recent(site)
        assert '<span class="stat-value">2</span>' in open_tile(site)
        save()
        assert "core-switch: Memory is high (99%)" not in recent(site)
        assert "core-switch: CPU is high (95%)" in recent(site), "the rest stay"
        assert '<span class="stat-value">1</span>' in open_tile(site)

    def test_older_ones_of_a_rule_now_off_go_too_and_come_back_with_it(self, site):
        memory(0, 12)
        memory(13, 20, value=50)                    # recovered: closed, not suppressed
        memory(21, 35)                              # again, and standing
        assert recent(site).count("Memory is high") == 2
        ident = save()                              # closes the standing one as suppressed
        assert "Memory is high" not in recent(site)
        remove(ident)
        page = recent(site)
        assert "Memory is high" in page, "the recovered one is history again"
        assert page.count("Memory is high") == 1, "the one closed as suppressed stays out"

    def test_its_own_line_keeps_the_history(self, site):
        memory(0, 12)
        memory(13, 20, value=50)
        save(mode="line", threshold="98")
        assert "Memory is high" in recent(site), "a line is not off"

    def test_a_truenas_type_that_is_no_longer_listed(self, site):
        from datetime import timedelta

        from test_snmp_alerts import T0

        async def gone():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                s.add(AlertIncident(key="tnalert:9:old", device_id=1,
                                    title="core-switch: boot-pool is on USB",
                                    detail="10.0.0.2 — TrueNAS Warning (PoolUSBDisks).",
                                    opened_at=T0))          # still open, its credential gone
                s.add(AlertIncident(key="tnalert:9:smart", device_id=1,
                                    title="core-switch: SMART says no",
                                    detail="10.0.0.2 — TrueNAS Critical (SMART).",
                                    opened_at=T0, closed_at=T0 + timedelta(hours=1),
                                    resolution="recovered"))
        run(gone())
        save(rule="truenas_alerts", detail="PoolUSBDisks")
        page = recent(site)
        assert "boot-pool is on USB" not in page and "SMART says no" in page
        assert '<span class="stat-value">0</span>' in open_tile(site), "nor counted"


def test_klass_from_the_incident_words():
    from spark.suppressions import _KLASS
    assert _KLASS.search("10.0.0.2 — TrueNAS Warning (PoolUSBDisks).").group(1) == "PoolUSBDisks"
    assert _KLASS.search("10.0.0.2 — TrueNAS Critical.") is None
