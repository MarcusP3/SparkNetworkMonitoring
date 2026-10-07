"""The Alerts page: what is firing now, the history of what ended (outages
and alert-rule incidents together, filtered and paged), and the messages
SPARK sent. Rows are written straight into the database; nothing here runs
a check or sends anything."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from spark import db as D
from spark.main import create_app
from spark.models import (AlertIncident, AlertSuppression, CheckType, Device, Incident,
                          Notification, NotificationStatus, Target, utcnow)
from spark.web import incident_rows

from test_credentials import PASSWORD, _config, flat, run

NOW = utcnow()


def ago(minutes: float):  # type: ignore[no-untyped-def]
    return NOW - timedelta(minutes=minutes)


@pytest.fixture
def db():
    config = _config()

    async def setup():  # type: ignore[no-untyped-def]
        D.init_engine(config)
        await D.init_db(config)
        async with D.session_scope() as s:
            s.add_all([
                Device(mac="aa:00:00:00:00:01", primary_ip="10.0.0.1", friendly_name="router",
                       last_seen=NOW),
                Device(mac="aa:00:00:00:00:02", primary_ip="10.0.0.2", friendly_name="nas",
                       last_seen=NOW),
            ])
            await s.flush()
            s.add(Target(name="nas web", address="10.0.0.2:443", check_type=CheckType.TCP,
                         device_id=2))
            await s.flush()
            s.add_all([
                # Firing now: an outage and a CPU alert.
                Incident(target_id=1, opened_at=ago(5), cause="connection refused"),
                AlertIncident(key="cpu:1", device_id=1, title="router: CPU is high (95%)",
                              detail="over 90% for 10 minutes.", opened_at=ago(15)),
                # Ended.
                Incident(target_id=1, opened_at=ago(600), closed_at=ago(590), cause="timeout",
                         resolution="recovered"),
                AlertIncident(key="pool:1:tank", device_id=2, title="nas: pool tank DEGRADED",
                              opened_at=ago(300), closed_at=ago(240), resolution="recovered"),
                AlertIncident(key="internet", title="The internet is down",
                              opened_at=ago(60 * 30), closed_at=ago(60 * 30 - 4),
                              resolution="recovered"),
            ])
    run(setup())
    yield config
    run(D.close_engine())


@pytest.fixture
def site(db):
    with TestClient(create_app(db), follow_redirects=False) as client:
        client.post("/setup", data={"setup_code": client.app.state.setup_code, "username": "admin",
                                    "password": PASSWORD, "password_confirm": PASSWORD,
                                    "timezone": "UTC"})
        yield client


def add(*rows):  # type: ignore[no-untyped-def]
    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            s.add_all(rows)
    run(go())


def section(page: str, ident: str) -> str:
    start = page.index(f'id="{ident}"')
    return page[start:page.index("</section>", start)]


class TestActivity:
    def test_firing_now(self, site):
        page = flat(site.get("/alerts").text)
        firing = section(page, "firing")
        assert firing.index("nas web") < firing.index("router: CPU is high"), "newest first"
        assert '<a href="/devices/2">nas web</a>' in firing, "an outage links its device"
        assert firing.count('<span class="pill bad dot">ongoing</span>') == 2
        assert "pool tank" not in firing
        assert '<span class="stat-name">Firing now</span></span><span class="stat-value">2</span>' in page
        assert '<span class="subnav-hint warn">2 firing</span>' in page

    def test_history_is_what_ended(self, site):
        history = section(flat(site.get("/alerts").text), "history")
        for name in ("nas web", "nas: pool tank DEGRADED", "The internet is down"):
            assert name in history
        assert "CPU is high" not in history and "ongoing" not in history
        assert history.index("pool tank") < history.index("nas web") < history.index("internet")
        assert "3 ended." in history

    def test_filter_by_source(self, site):
        history = section(flat(site.get("/alerts?source=Storage").text), "history")
        assert "pool tank" in history and "nas web" not in history and "internet" not in history
        assert '<a class="chip" href="/alerts?source=Storage#history" aria-current="true">Storage</a>' in history
        history = section(flat(site.get("/alerts?source=Target").text), "history")
        assert "nas web" in history and "pool tank" not in history
        history = section(flat(site.get("/alerts?source=Internet").text), "history")
        assert "The internet is down" in history and "1 ended, with these filters." in history
        assert "3 ended." in flat(site.get("/alerts?source=nonsense").text), "unknown: everything"

    def test_filter_by_device(self, site):
        page = flat(site.get("/alerts").text)
        assert '<option value="1" >router</option>' in page and '<option value="2" >nas</option>' in page
        history = section(flat(site.get("/alerts?device=2").text), "history")
        assert "pool tank" in history and "nas web" in history and "internet" not in history
        history = section(flat(site.get("/alerts?device=1").text), "history")
        assert "Nothing has ended with these filters." in history
        assert "3 ended." in flat(site.get("/alerts?device=99").text), "not a device with incidents"

    def test_paging(self, site):
        add(*[AlertIncident(key=f"port:{i}", device_id=1, title=f"old port {i:02d}",
                            opened_at=ago(1000 + i), closed_at=ago(999 + i), resolution="recovered")
              for i in range(30)])
        page = flat(site.get("/alerts").text)
        assert "Page 1 of 2" in page and 'href="/alerts?page=2#history" rel="next">Older →</a>' in page
        assert "old port 22" in page and "old port 23" not in page, "2 newer, then 23 of these"
        page = flat(site.get("/alerts?page=2").text)
        assert "old port 23" in page and "old port 29" in page and "old port 22" not in page
        assert "The internet is down" in page, "the oldest, last"
        assert 'href="/alerts#history" rel="prev">← Newer</a>' in page
        assert "Page 2 of 2" in flat(site.get("/alerts?page=999").text), "past the end: the last"

    def test_a_suppressed_rule_is_left_out_of_firing_now(self, site):
        add(AlertSuppression(device_id=1, rule="cpu", threshold=None))
        page = flat(site.get("/alerts").text)
        assert "CPU is high" not in section(page, "firing")
        assert "1 more open from a rule switched off for its device" in page

    def test_quiet(self, db):
        async def clear():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                for model in (Incident, AlertIncident):
                    await s.execute(model.__table__.delete())
        run(clear())
        with TestClient(create_app(db), follow_redirects=False) as site:
            site.post("/setup", data={"setup_code": site.app.state.setup_code, "username": "admin",
                                      "password": PASSWORD, "password_confirm": PASSWORD,
                                      "timezone": "UTC"})
            page = flat(site.get("/alerts").text)
        assert "Nothing is firing." in page and "Nothing has ended yet." in page
        assert '<span class="subnav-hint">quiet</span>' in page
        assert 'name="device"' not in page, "no device filter with nothing to filter"

    def test_counts(self, db):
        async def go():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                return (await incident_rows.opened_since(s, ago(60 * 24)),
                        await incident_rows.opened_since(s, ago(60 * 24 * 7)))
        assert run(go()) == (4, 5)


class TestMessages:
    def test_the_log(self, site):
        add(Notification(kind="cpu_high", subject="router: CPU is high", body="`10.0.0.1` — over 90%",
                         tone="bad", status=NotificationStatus.SENT, created_at=ago(15),
                         sent_at=ago(15)),
            Notification(kind="target_down", subject="nas web is down", tone="bad",
                         status=NotificationStatus.FAILED, attempts=5, created_at=ago(5),
                         error="Discord answered 404"))
        page = flat(site.get("/alerts/messages").text)
        assert page.index("nas web is down") < page.index("router: CPU is high")
        assert '<span class="pill bad dot">failed</span> <strong>nas web is down</strong>' in page
        assert "5 tries" in page and "Discord answered 404" in page
        assert '<span class="pill ok dot">sent</span>' in page
        assert "<pre>`10.0.0.1` — over 90%</pre>" in page
        assert '<span class="subnav-hint warn">1 failed</span>' in page
        assert '<span class="stat-name">Failed, 7 days</span></span><span class="stat-value">1</span>' in page
        page = flat(site.get("/alerts/messages?status=failed").text)
        assert "nas web is down" in page and "router: CPU is high" not in page
        assert "No messages like that." in flat(site.get("/alerts/messages?status=held").text)

    def test_no_webhook_says_so(self, site):
        page = flat(site.get("/alerts/messages").text)
        assert "No Discord webhook is set, so nothing can be sent." in page
        assert ('<span class="pill neutral">waiting</span> <strong>SPARK set up: administrator '
                '&#39;admin&#39; created') in page, "security events are messages too"


class TestAround:
    def test_in_the_nav_and_from_the_dashboard(self, site):
        page = site.get("/").text
        assert '<a href="/alerts" class="">Alerts</a>' in page
        assert '<a href="/alerts" class="btn-quiet">All alerts →</a>' in page
        assert '<a href="/alerts" class="active">Alerts</a>' in site.get("/alerts/messages").text

    def test_signed_out(self, db):
        with TestClient(create_app(db), follow_redirects=False) as client:
            for path in ("/alerts", "/alerts/messages"):
                assert client.get(path).status_code in (302, 303, 307)


def test_sources_include_proxmox():
    assert incident_rows.source_of("pveguest:3:101") == "Proxmox"
    assert incident_rows.source_of("internet") == "Internet"
