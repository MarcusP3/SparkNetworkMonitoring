"""Maintenance windows: a device worked on, on purpose, for a while.

While one is on, alerts about the device (and, by default, what is below it
on the map) are held like the mute list holds them; incidents are still
recorded. When it ends, what is still broken is sent, and its recovery
follows. Weekly windows keep to the local clock. The SNMP and rule alerts
are in test_maintenance_alerts.py.

The network here: router (the gateway), proxmox behind it, and a vm and a
printer -- the vm behind proxmox, the printer behind the router.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from spark import alerts, maintenance
from spark import db as D
from spark.checks.base import CheckOutcome
from spark.main import create_app
from spark.models import (AlertMute, CheckType, Device, DeviceRole, Incident, MaintenanceWindow,
                          Notification, Target, utcnow)

from test_alerts import _config
from test_credentials import flat

PASSWORD = "correct horse battery"
ROUTER, PROXMOX, VM, PRINTER = 1, 2, 3, 4
PROXMOX_WEB, VM_PING = 1, 2


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


@pytest.fixture
def db():
    config = _config()

    async def setup():  # type: ignore[no-untyped-def]
        D.init_engine(config)
        await D.init_db(config)
        async with D.session_scope() as s:
            s.add(Device(mac="aa:00:00:00:00:01", primary_ip="10.0.0.1", friendly_name="router",
                         role=DeviceRole.GATEWAY, last_seen=utcnow()))
            await s.flush()
            s.add(Device(mac="aa:00:00:00:00:02", primary_ip="10.0.0.2", friendly_name="proxmox",
                         parent_device_id=ROUTER, last_seen=utcnow()))
            await s.flush()
            s.add_all([
                Device(mac="aa:00:00:00:00:03", primary_ip="10.0.0.3", friendly_name="vm",
                       parent_device_id=PROXMOX, last_seen=utcnow()),
                Device(mac="aa:00:00:00:00:04", primary_ip="10.0.0.4", friendly_name="printer",
                       parent_device_id=ROUTER, last_seen=utcnow()),
            ])
            await s.flush()
            s.add_all([
                Target(name="proxmox web", check_type=CheckType.TCP, address="10.0.0.2:8006",
                       device_id=PROXMOX, failure_threshold=1, recovery_threshold=1),
                Target(name="vm ping", check_type=CheckType.PING, address="10.0.0.3",
                       device_id=VM, failure_threshold=1, recovery_threshold=1),
            ])
    run(setup())
    yield config
    run(D.close_engine())


def checks(monkeypatch, target_id: int, *results: bool) -> None:
    """The real check runner, with the probe faked."""
    from spark.engine import runner

    outcomes = iter(results)

    async def fake(check_type, spec):  # type: ignore[no-untyped-def]
        return CheckOutcome.up(1.0) if next(outcomes) else CheckOutcome.down("connection refused")

    monkeypatch.setattr(runner, "run_check", fake)
    for _ in results:
        run(runner.run_target(target_id))


def window(device_id: int, *, start: float = -1, minutes: float = 60, weekly=False,
           below=True, note="") -> int:
    """A window from `start` minutes from now, `minutes` long."""
    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            now = utcnow()
            w = await maintenance.add(s, device_id=device_id,
                                      starts_at=now + timedelta(minutes=start),
                                      ends_at=now + timedelta(minutes=start + minutes),
                                      weekly=weekly, include_below=below, note=note, now=now)
            return w.id
    return run(go())


def end(window_id: int) -> None:
    """The window is over: it ends now, and the minute's job runs."""
    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            w = await s.get(MaintenanceWindow, window_id)
            w.ends_at = utcnow()
        return await maintenance.run()
    run(go())


def sent() -> list[Notification]:
    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            rows = list((await s.execute(select(Notification).order_by(Notification.id))).scalars())
            for r in rows:
                s.expunge(r)
            return rows
    return run(go())


def subjects() -> list[str]:
    # Sign-in events from setup are not what these are about.
    return [n.subject for n in sent() if n.kind not in ("setup", "new_address")]


def incidents() -> list[Incident]:
    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            return list((await s.execute(select(Incident).order_by(Incident.id))).scalars())
    return run(go())


def covering(device_id: int):  # type: ignore[no-untyped-def]
    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            w = await maintenance.covering(s, device_id)
            return w.id if w else None
    return run(go())


# --------------------------------------------------------------------------
# When a window is on
# --------------------------------------------------------------------------


NY = ZoneInfo("America/New_York")


def at(*args) -> datetime:  # type: ignore[no-untyped-def]
    return datetime(*args, tzinfo=NY).astimezone(timezone.utc)


def weekly_at_3am() -> MaintenanceWindow:
    """Sundays 03:00-04:00 New York, from Oct 25 2026. Clocks go back on Nov 1."""
    return MaintenanceWindow(starts_at=at(2026, 10, 25, 3), ends_at=at(2026, 10, 25, 4), weekly=True)


class TestSpans:
    def test_one_off(self):
        w = MaintenanceWindow(starts_at=at(2026, 10, 20, 9), ends_at=at(2026, 10, 20, 11), weekly=False)
        assert maintenance.span_at(w, at(2026, 10, 20, 10), NY).holds(at(2026, 10, 20, 10))
        assert not maintenance.span_at(w, at(2026, 10, 20, 11), NY).holds(at(2026, 10, 20, 11)), "the end is out"
        assert maintenance.last_ended(w, at(2026, 10, 20, 10), NY) is None
        assert maintenance.last_ended(w, at(2026, 10, 20, 11), NY).end == at(2026, 10, 20, 11)
        assert maintenance.next_span(w, at(2026, 10, 20, 8), NY).start == at(2026, 10, 20, 9)
        assert maintenance.next_span(w, at(2026, 10, 20, 12), NY) is None

    def test_weekly_keeps_to_the_local_clock_across_the_change(self):
        w = weekly_at_3am()
        first = maintenance.span_at(w, at(2026, 10, 25, 3, 30), NY)
        after = maintenance.span_at(w, at(2026, 11, 1, 3, 30), NY)
        assert first.start == datetime(2026, 10, 25, 7, tzinfo=timezone.utc), "EDT"
        assert after.start == datetime(2026, 11, 1, 8, tzinfo=timezone.utc), "EST: still 03:00 there"
        assert after.end - after.start == timedelta(hours=1)
        assert after.holds(at(2026, 11, 1, 3, 30)) and not after.holds(at(2026, 11, 1, 4, 30))

    def test_weekly_ends_and_what_comes_next(self):
        w = weekly_at_3am()
        assert maintenance.last_ended(w, at(2026, 10, 25, 2), NY) is None, "not begun"
        assert maintenance.last_ended(w, at(2026, 11, 1, 3, 30), NY).end == at(2026, 10, 25, 4)
        assert maintenance.last_ended(w, at(2026, 11, 1, 5), NY).end == at(2026, 11, 1, 4)
        assert maintenance.next_span(w, at(2026, 11, 1, 5), NY).start == at(2026, 11, 8, 3)
        assert maintenance.next_span(w, at(2026, 11, 1, 3, 30), NY).start == at(2026, 11, 1, 3)


class TestCovering:
    def test_its_own_and_what_is_below(self, db):
        ident = window(PROXMOX)
        assert covering(PROXMOX) == ident and covering(VM) == ident
        assert covering(ROUTER) is None and covering(PRINTER) is None

    def test_only_itself(self, db):
        window(PROXMOX, below=False)
        assert covering(PROXMOX) is not None and covering(VM) is None

    def test_not_yet_and_no_longer(self, db):
        later = window(PROXMOX, start=30)
        assert covering(PROXMOX) is None
        end(later)
        assert covering(PROXMOX) is None


class TestHeld:
    def test_an_outage_during_it_is_recorded_not_sent(self, db, monkeypatch):
        window(PROXMOX)
        checks(monkeypatch, PROXMOX_WEB, False)
        assert subjects() == []
        (incident,) = incidents()
        assert incident.closed_at is None
        checks(monkeypatch, PROXMOX_WEB, True)
        assert subjects() == [], "nor its recovery"

    def test_below_it_too(self, db, monkeypatch):
        window(PROXMOX)
        checks(monkeypatch, VM_PING, False)
        assert subjects() == []

    def test_below_it_only_when_asked(self, db, monkeypatch):
        window(PROXMOX, below=False)
        checks(monkeypatch, VM_PING, False)
        assert subjects() == ["vm ping is down"]

    def test_somewhere_else_is_not_held(self, db, monkeypatch):
        window(PRINTER)
        checks(monkeypatch, PROXMOX_WEB, False)
        assert subjects() == ["proxmox web is down"]

    def test_a_planned_one_holds_nothing_yet(self, db, monkeypatch):
        window(PROXMOX, start=30)
        checks(monkeypatch, PROXMOX_WEB, False)
        assert subjects() == ["proxmox web is down"]


# --------------------------------------------------------------------------
# When it ends
# --------------------------------------------------------------------------


class TestEnd:
    def test_still_down_is_sent_and_its_recovery_follows(self, db, monkeypatch):
        ident = window(PROXMOX)
        checks(monkeypatch, PROXMOX_WEB, False)
        end(ident)
        (down,) = sent()
        assert down.subject == "proxmox web is still down after maintenance" and down.kind == "down"
        assert "Maintenance on proxmox has ended." in down.body and "10.0.0.2:8006" in down.body
        assert down.dedupe_key == f"incident:{incidents()[0].id}:down"
        checks(monkeypatch, PROXMOX_WEB, True)
        assert subjects() == ["proxmox web is still down after maintenance", "proxmox web is back up"]

    def test_below_it_as_well(self, db, monkeypatch):
        ident = window(PROXMOX)
        checks(monkeypatch, VM_PING, False)
        end(ident)
        assert subjects() == ["vm ping is still down after maintenance"]

    def test_back_before_the_end_is_nothing(self, db, monkeypatch):
        ident = window(PROXMOX)
        checks(monkeypatch, PROXMOX_WEB, False, True)
        end(ident)
        assert subjects() == []

    def test_once(self, db, monkeypatch):
        ident = window(PROXMOX)
        checks(monkeypatch, PROXMOX_WEB, False)
        end(ident)
        run(maintenance.run())
        assert len(subjects()) == 1

    def test_already_told_is_not_told_again(self, db, monkeypatch):
        checks(monkeypatch, PROXMOX_WEB, False)          # alerted before the window
        ident = window(PROXMOX)
        end(ident)
        assert subjects() == ["proxmox web is down"]

    def test_a_muted_device_stays_quiet(self, db, monkeypatch):
        async def mute():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                s.add(AlertMute(device_id=PROXMOX))
        run(mute())
        ident = window(PROXMOX)
        checks(monkeypatch, PROXMOX_WEB, False)
        end(ident)
        assert subjects() == []

    def test_alerting_off_sends_nothing(self, db, monkeypatch):
        ident = window(PROXMOX)
        checks(monkeypatch, PROXMOX_WEB, False)

        async def off():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                settings = await alerts.load(s)
                settings["enabled"] = False
                await D.save_setting(s, alerts.SETTING, settings)
        run(off())
        end(ident)
        assert subjects() == []

    def test_still_under_another_waits_for_that_one(self, db, monkeypatch):
        short = window(PROXMOX)
        long = window(ROUTER, minutes=120)
        checks(monkeypatch, PROXMOX_WEB, False)
        end(short)
        assert subjects() == [], "the router's window still covers it"
        end(long)
        assert subjects() == ["proxmox web is still down after maintenance"]

    def test_ending_early_sends_at_once(self, db, monkeypatch):
        ident = window(PROXMOX)
        checks(monkeypatch, PROXMOX_WEB, False)

        async def stop():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                return await maintenance.end_now(s, await s.get(MaintenanceWindow, ident))
        assert run(stop()) is True
        assert subjects() == ["proxmox web is still down after maintenance"]
        assert covering(PROXMOX) is None

    def test_removing_one_that_is_on_still_sends(self, db, monkeypatch):
        ident = window(PROXMOX)
        checks(monkeypatch, PROXMOX_WEB, False)

        async def remove():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                await maintenance.remove(s, await s.get(MaintenanceWindow, ident))
        run(remove())
        assert subjects() == ["proxmox web is still down after maintenance"]
        assert covering(PROXMOX) is None

    def test_a_weekly_one_dated_in_the_past_starts_with_its_next(self, db, monkeypatch):
        checks(monkeypatch, PROXMOX_WEB, False)          # down, and told
        async def clear():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                await s.execute(Notification.__table__.delete())
        run(clear())
        window(PROXMOX, start=-60 * 24 * 3, minutes=30, weekly=True)   # three days ago
        run(maintenance.run())
        assert subjects() == [], "an end from before it existed is not one to report"


class TestRules:
    @pytest.mark.parametrize("weekly, minutes, message", [
        (False, 60 * 24 * 15, "at most 14 days"),
        (True, 60 * 25, "at most a day"),
        (False, 0, "after the start"),
    ])
    def test_refused(self, db, weekly, minutes, message):
        with pytest.raises(maintenance.MaintenanceError, match=message):
            window(PROXMOX, minutes=minutes, weekly=weekly)

    def test_over_already(self, db):
        with pytest.raises(maintenance.MaintenanceError, match="already over"):
            window(PROXMOX, start=-120, minutes=60)

    def test_no_such_device(self, db):
        with pytest.raises(maintenance.MaintenanceError, match="Choose a device"):
            window(99)


# --------------------------------------------------------------------------
# The pages
# --------------------------------------------------------------------------


@pytest.fixture
def site(db):
    with TestClient(create_app(db), follow_redirects=False) as client:
        client.post("/setup", data={"setup_code": client.app.state.setup_code, "username": "admin",
                                    "password": PASSWORD, "password_confirm": PASSWORD,
                                    "timezone": "America/Chicago"})
        yield client


def windows() -> list[MaintenanceWindow]:
    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            return list((await s.execute(select(MaintenanceWindow).order_by(MaintenanceWindow.id))).scalars())
    return run(go())


class TestDevicePage:
    def test_start_one_in_a_click(self, site):
        page = flat(site.get(f"/devices/{PROXMOX}").text)
        assert '<summary class="btn-quiet">Maintenance…</summary>' in page
        assert "And the 1 device below it" in page
        answer = site.post("/alerts/maintenance", data={
            "device_id": PROXMOX, "minutes": "120", "below": "1", "note": "PVE 9 upgrade",
            "back": f"/devices/{PROXMOX}"})
        assert answer.status_code == 303 and answer.headers["location"] == f"/devices/{PROXMOX}"
        (w,) = windows()
        assert w.include_below and w.note == "PVE 9 upgrade" and not w.weekly
        assert timedelta(minutes=119) < w.ends_at - w.starts_at <= timedelta(minutes=120)
        page = flat(site.get(f"/devices/{PROXMOX}").text)
        assert "under maintenance until" in page and "· PVE 9 upgrade</span>" in page
        assert f'action="/alerts/maintenance/{w.id}/end"' in page and "Maintenance…" not in page
        below = flat(site.get(f"/devices/{VM}").text)
        assert f'· from <a href="/devices/{PROXMOX}">proxmox</a> · PVE 9 upgrade</span>' in below
        assert "Maintenance →" in below, "ended where it was started"

    def test_end_it_from_there(self, site):
        site.post("/alerts/maintenance", data={"device_id": PROXMOX, "minutes": "60", "below": "1",
                                               "back": f"/devices/{PROXMOX}"})
        (w,) = windows()
        answer = site.post(f"/alerts/maintenance/{w.id}/end", data={"back": f"/devices/{PROXMOX}"})
        assert answer.headers["location"] == f"/devices/{PROXMOX}"
        assert "under maintenance" not in site.get(f"/devices/{PROXMOX}").text
        assert covering(PROXMOX) is None

    def test_a_device_with_nothing_below_does_not_ask(self, site):
        page = flat(site.get(f"/devices/{PRINTER}").text)
        assert "below it" not in page and '<input type="hidden" name="below" value="1">' in page

    def test_only_the_offered_lengths(self, site):
        site.post("/alerts/maintenance", data={"device_id": PROXMOX, "minutes": "7",
                                               "back": f"/devices/{PROXMOX}"})
        site.post("/alerts/maintenance", data={"device_id": PROXMOX, "minutes": "999999999",
                                               "back": f"/devices/{PROXMOX}"})
        assert windows() == []

    def test_going_back_never_leaves_the_site(self, site):
        for back in ("https://example.com", "/settings", f"/devices/{VM}"):
            answer = site.post("/alerts/maintenance", data={"device_id": PROXMOX, "minutes": "30",
                                                            "back": back})
            assert answer.headers["location"] == "/alerts/maintenance", back


class TestMaintenanceTab:
    def test_schedule_one_on_the_local_clock(self, site):
        answer = site.post("/alerts/maintenance", data={
            "device_id": PRINTER, "starts": "2031-12-01T03:00", "ends": "2031-12-01T04:30",
            "below": "1", "note": "new drum", "back": "/alerts/maintenance"})
        assert answer.status_code == 303 and answer.headers["location"] == "/alerts/maintenance"
        (w,) = windows()
        assert w.starts_at == datetime(2031, 12, 1, 9, tzinfo=timezone.utc), "03:00 in Chicago"
        assert w.ends_at == datetime(2031, 12, 1, 10, 30, tzinfo=timezone.utc)
        page = flat(site.get("/alerts/maintenance").text)
        assert "Mon Dec 1, 03:00 – Mon Dec 1, 04:30" in page and "new drum" in page
        assert ">Cancel</button>" in page and "End now" not in page
        assert '<span class="subnav-hint">1 planned</span>' in page
        assert "Times are in America/Chicago." in page

    def test_weekly(self, site):
        site.post("/alerts/maintenance", data={
            "device_id": ROUTER, "starts": "2031-12-07T02:00", "ends": "2031-12-07T02:30",
            "weekly": "1", "below": "1"})
        (w,) = windows()
        assert w.weekly
        page = flat(site.get("/alerts/maintenance").text)
        assert "Every Sunday, 02:00–02:30" in page and "and 3 below it" in page
        assert ">Stop repeating</button>" in page

    @pytest.mark.parametrize("starts, ends, extra, message", [
        ("2031-12-01T04:00", "2031-12-01T03:00", {}, "The end has to be after the start."),
        ("2031-12-01T04:00", "2031-12-03T04:00", {"weekly": "1"}, "A weekly window can be at most a day long."),
        ("2031-12-01T04:00", "2032-01-01T04:00", {}, "A window can be at most 14 days long."),
        ("2020-12-01T04:00", "2020-12-01T05:00", {}, "That window is already over."),
        ("tomorrow", "2031-12-01T05:00", {}, "Give a start and an end."),
    ])
    def test_refused_says_why_and_keeps_what_was_typed(self, site, starts, ends, extra, message):
        answer = site.post("/alerts/maintenance", data={
            "device_id": PRINTER, "starts": starts, "ends": ends, "note": "kept", **extra})
        assert answer.status_code == 400
        page = flat(answer.text)
        assert f'<div class="alert error">{message}</div>' in page
        assert 'value="kept"' in page and f'value="{starts}"' in page
        assert windows() == []

    def test_no_device(self, site):
        answer = site.post("/alerts/maintenance", data={"device_id": "", "starts": "2031-12-01T03:00",
                                                        "ends": "2031-12-01T04:00"})
        assert answer.status_code == 400 and "Choose a device." in answer.text

    def test_preselects_the_device_it_came_from(self, site):
        page = flat(site.get(f"/alerts/maintenance?device={PRINTER}").text)
        assert f'<option value="{PRINTER}" selected>printer — 10.0.0.4</option>' in page

    def test_on_now_and_cancelled(self, site):
        on = window(PROXMOX)
        page = flat(site.get("/alerts/maintenance").text)
        assert '<span class="pill neutral dot">on now</span>' in page
        assert f'action="/alerts/maintenance/{on}/end"' in page
        assert '<span class="subnav-hint">1 on</span>' in page
        site.post(f"/alerts/maintenance/{on}/delete", data={})
        assert windows() == []

    def test_nothing_yet(self, site):
        assert "No maintenance on or planned." in site.get("/alerts/maintenance").text

    def test_signed_out(self, db):
        with TestClient(create_app(db), follow_redirects=False) as client:
            assert client.get("/alerts/maintenance").status_code in (302, 303, 307)
            assert client.post("/alerts/maintenance", data={"device_id": PROXMOX,
                                                            "minutes": "60"}).status_code in (302, 303, 307)
            assert windows() == []


class TestIncidents:
    def test_held_ones_do_not_turn_the_counts_red(self, site, monkeypatch):
        window(PROXMOX)
        checks(monkeypatch, PROXMOX_WEB, False)
        page = flat(site.get("/alerts").text)
        firing = page[page.index('id="firing"'):]
        assert '<span class="pill neutral" title="Its device is under a maintenance window: held until it ends">under maintenance</span>' in firing
        assert '<a class="stat s-inc" href="/alerts#firing" title="1 under maintenance">' in page
        assert '<span class="subnav-hint">1 on hold</span>' in page
        from test_dashboard import stat_card
        classes, value = stat_card(site.get("/").text, "Open incidents")
        assert value == "1" and "is-bad" not in classes

    def test_after_it_ends_they_do(self, site, monkeypatch):
        ident = window(PROXMOX)
        checks(monkeypatch, PROXMOX_WEB, False)
        end(ident)
        page = flat(site.get("/alerts").text)
        assert '<a class="stat s-inc is-bad" href="/alerts#firing">' in page
        assert ">during maintenance</span>" in page, "and it says when it began"

    def test_the_history_remembers(self, site, monkeypatch):
        ident = window(PROXMOX)
        checks(monkeypatch, PROXMOX_WEB, False, True)
        end(ident)
        history = flat(site.get("/alerts").text)
        history = history[history.index('id="history"'):]
        assert ">during maintenance</span>" in history


def test_migration_23_from_a_version_22_database():
    from sqlalchemy import inspect

    async def shape(wind_back: bool):  # type: ignore[no-untyped-def]
        config = _config()
        D.init_engine(config)
        await D.init_db(config)
        if wind_back:
            async with D.session_scope() as s:
                await s.execute(text("DROP TABLE maintenance_window"))
                await D._set_version(s, 22)
            await D.close_engine()
            D.init_engine(config)
            await D.init_db(config)
        async with D.session_scope() as s:
            connection = await s.connection()
            columns = await connection.run_sync(
                lambda sync: sorted(c["name"] for c in inspect(sync).get_columns("maintenance_window")))
            version = await D._get_version(s)
        await D.close_engine()
        return columns, version

    assert run(shape(True)) == run(shape(False))
    assert run(shape(False))[1] == D.CURRENT_VERSION >= 23
