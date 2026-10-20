"""Acknowledging an incident: someone has seen it and is dealing with it.

It stays in Firing now and still counts, but stops turning the counts red,
which are for what no one has looked at. Only an open incident can be
acknowledged; the note goes with it; Undo takes it back. Nothing is sent.
The rows come from test_alerts_page's fixture: an outage (incident 1) and a
CPU alert (alert incident 1) firing, and three that have ended.
"""

from __future__ import annotations

import re

from sqlalchemy import func, select

from spark import db as D
from spark import events
from spark.models import AlertIncident, Incident, Notification

from test_alerts_page import NOW, ago, db, section, site  # noqa: F401
from test_credentials import flat, run
from test_dashboard import stat_card

TARGET = "/alerts/incidents/target/1"
ALERT = "/alerts/incidents/alert/1"


def row(model, ident: int):  # type: ignore[no-untyped-def]
    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            return await s.get(model, ident)
    return run(go())


def messages() -> int:
    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            return await s.scalar(select(func.count(Notification.id)))
    return run(go())


def firing_tile(page: str) -> set[str]:
    match = re.search(r'<a class="stat ([^"]*)" href="/alerts#firing"', page)
    assert match, "no Firing now tile"
    return set(match.group(1).split())


class TestAcknowledge:
    def test_an_outage_with_a_note(self, site):
        before = messages()
        answer = site.post(f"{TARGET}/acknowledge", data={"note": "  ISP ticket 4471  ", "back": "/alerts"})
        assert answer.status_code == 303 and answer.headers["location"] == "/alerts#firing"
        incident = row(Incident, 1)
        assert incident.acknowledged_at is not None and incident.ack_note == "ISP ticket 4471"
        assert messages() == before, "acknowledging sends nothing"

        firing = section(flat(site.get("/alerts").text), "firing")
        assert '<span class="pill bad dot">ongoing</span> <span class="pill neutral">acknowledged</span>' in firing
        assert '<span class="ack-note">ISP ticket 4471</span> · Acknowledged' in firing
        assert f'action="{TARGET}/unacknowledge"' in firing, "with Undo"
        assert f'action="{TARGET}/acknowledge"' not in firing
        assert f'action="{ALERT}/acknowledge"' in firing, "the other is still to do"

    def test_an_alert_and_no_note(self, site):
        site.post(f"{ALERT}/acknowledge", data={"note": "   ", "back": "/alerts"})
        alert = row(AlertIncident, 1)
        assert alert.acknowledged_at is not None and alert.ack_note is None
        firing = section(flat(site.get("/alerts").text), "firing")
        assert "ack-note" not in firing and "Acknowledged" in firing

    def test_again_replaces_the_note(self, site):
        site.post(f"{ALERT}/acknowledge", data={"note": "looking"})
        site.post(f"{ALERT}/acknowledge", data={"note": "fan replaced, watching it"})
        assert row(AlertIncident, 1).ack_note == "fan replaced, watching it"

    def test_undo(self, site):
        site.post(f"{TARGET}/acknowledge", data={"note": "wrong one"})
        answer = site.post(f"{TARGET}/unacknowledge", data={"back": "/"})
        assert answer.headers["location"] == "/"
        incident = row(Incident, 1)
        assert incident.acknowledged_at is None and incident.ack_note is None
        assert f'action="{TARGET}/acknowledge"' in site.get("/alerts").text

    def test_tells_open_pages(self, site, monkeypatch):
        seen = []
        monkeypatch.setattr(events, "publish", seen.append)
        site.post(f"{ALERT}/acknowledge", data={})
        site.post(f"{ALERT}/unacknowledge", data={})
        assert seen == [{"kind": "acknowledged"}, {"kind": "acknowledged"}]


class TestOnlyWhatIsOpen:
    def test_an_ended_incident_is_left_as_it_ended(self, site, monkeypatch):
        seen = []
        monkeypatch.setattr(events, "publish", seen.append)
        site.post("/alerts/incidents/target/2/acknowledge", data={"note": "late"})
        site.post("/alerts/incidents/alert/2/acknowledge", data={"note": "late"})
        assert row(Incident, 2).acknowledged_at is None
        assert row(AlertIncident, 2).acknowledged_at is None
        assert seen == [], "nothing changed, so nothing to tell"

    def test_ended_after_acknowledging_keeps_the_note_and_loses_undo(self, site):
        site.post(f"{ALERT}/acknowledge", data={"note": "fan replaced"})

        async def recover():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                alert = await s.get(AlertIncident, 1)
                alert.closed_at, alert.resolution = NOW, "recovered"
        run(recover())
        assert site.post(f"{ALERT}/unacknowledge", data={}).status_code == 303
        assert row(AlertIncident, 1).ack_note == "fan replaced", "Undo cannot rewrite history"
        history = section(flat(site.get("/alerts").text), "history")
        assert '<span class="ack-note">fan replaced</span> · Acknowledged' in history
        assert "unacknowledge" not in history and 'name="note"' not in history

    def test_no_such_incident(self, site):
        assert site.post("/alerts/incidents/alert/999/acknowledge", data={}).status_code == 303
        assert site.post("/alerts/incidents/nonsense/1/acknowledge", data={}).status_code == 404
        assert site.post("/alerts/incidents/alert/0/acknowledge", data={}).status_code == 404

    def test_history_has_no_buttons(self, site):
        history = section(flat(site.get("/alerts").text), "history")
        assert "/acknowledge" not in history and "<details class=\"ack\">" not in history


class TestInputs:
    def test_a_note_over_its_limit_is_refused_and_nothing_changes(self, site):
        answer = site.post(f"{TARGET}/acknowledge", data={"note": "x" * 281})
        assert answer.status_code == 400
        assert row(Incident, 1).acknowledged_at is None
        assert site.post(f"{TARGET}/acknowledge", data={"note": "x" * 280}).status_code == 303

    def test_the_page_stops_you_at_the_same_limit(self, site):
        assert 'name="note" maxlength="280"' in flat(site.get("/alerts").text)

    def test_markup_in_a_note_is_shown_not_run(self, site):
        site.post(f"{TARGET}/acknowledge", data={"note": "<script>alert(1)</script>"})
        page = site.get("/alerts").text
        assert "<script>alert(1)</script>" not in page
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page

    def test_going_back_never_leaves_the_site(self, site):
        for back in ("https://example.com/", "//example.com", "/settings", ""):
            answer = site.post(f"{ALERT}/acknowledge", data={"back": back})
            assert answer.headers["location"] == "/alerts#firing", back

    def test_signed_out(self, db):
        from fastapi.testclient import TestClient

        from spark.main import create_app
        with TestClient(create_app(db), follow_redirects=False) as client:
            assert client.post(f"{TARGET}/acknowledge", data={}).status_code in (302, 303, 307)
            assert row(Incident, 1).acknowledged_at is None


class TestCounts:
    def test_one_of_two_still_red(self, site):
        site.post(f"{TARGET}/acknowledge", data={})
        page = flat(site.get("/alerts").text)
        assert "is-bad" in firing_tile(page)
        assert '<span class="subnav-hint warn">2 firing</span>' in page
        assert 'title="1 acknowledged"' in page

    def test_all_acknowledged_is_not_red(self, site):
        site.post(f"{TARGET}/acknowledge", data={})
        site.post(f"{ALERT}/acknowledge", data={})
        page = flat(site.get("/alerts").text)
        assert firing_tile(page) == {"s-inc"}, "pink as ever, the number plain"
        assert '<span class="stat-name">Firing now</span></span><span class="stat-value">2</span>' in page
        assert '<span class="subnav-hint">2 acknowledged</span>' in page
        assert 'class="card " id="firing"' in page, "no warning edge"

    def test_the_dashboard(self, site):
        classes, value = stat_card(site.get("/").text, "Open incidents")
        assert value == "2" and "is-bad" in classes
        site.post(f"{TARGET}/acknowledge", data={"back": "/"})
        page = site.get("/").text
        classes, value = stat_card(page, "Open incidents")
        assert value == "2" and "is-bad" in classes, "one is still news"
        assert 'title="Still unresolved, 1 acknowledged"' in page
        site.post(f"{ALERT}/acknowledge", data={"back": "/"})
        classes, value = stat_card(site.get("/").text, "Open incidents")
        assert value == "2" and "is-bad" not in classes, "still counted, not red"

    def test_the_dashboard_buttons_come_back_to_it(self, site):
        page = flat(site.get("/").text)
        assert f'action="{TARGET}/acknowledge" class="ack-form" data-stay> <input type="hidden" name="back" value="/">' in page


def test_migration_22_from_a_version_21_database():
    from sqlalchemy import inspect, text

    from test_snmp_alerts import _config

    async def shape(wind_back: bool):  # type: ignore[no-untyped-def]
        config = _config()
        D.init_engine(config)
        await D.init_db(config)
        if wind_back:
            async with D.session_scope() as s:
                await s.execute(text("ALTER TABLE incident DROP COLUMN ack_note"))
                await s.execute(text("ALTER TABLE alert_incident DROP COLUMN ack_note"))
                await s.execute(text("ALTER TABLE alert_incident DROP COLUMN acknowledged_at"))
                await D._set_version(s, 21)
            await D.close_engine()
            D.init_engine(config)
            await D.init_db(config)
        async with D.session_scope() as s:
            connection = await s.connection()
            columns = await connection.run_sync(lambda sync: {
                table: sorted(c["name"] for c in inspect(sync).get_columns(table))
                for table in ("incident", "alert_incident")})
            version = await D._get_version(s)
        await D.close_engine()
        return columns, version

    migrated, fresh = run(shape(True)), run(shape(False))
    assert migrated == fresh
    assert {"acknowledged_at", "ack_note"} <= set(fresh[0]["alert_incident"])
    assert fresh[1] == D.CURRENT_VERSION >= 22
