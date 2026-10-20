"""A starred port that is up but unwell: slower than usual, or erroring.

Slower: below the fastest speed it has run at while starred, for five
minutes -- a failing cable renegotiates 1 Gbps down to 100 Mbps and stays
there. "Accept" takes the new speed as usual. Errors: in and out together,
a rate per minute over the line for its minutes, clearing under half the
line. Both on starred ports only, both off-able and suppressible, and
neither touches a port that is down (that is the other rule).
"""

from __future__ import annotations

from sqlalchemy import select

from spark import snmp_alerts
from spark import db as D
from spark.collectors.base import InterfaceStat
from spark.models import AlertIncident, AlertSuppression, SnmpInterface

from test_credentials import flat
from test_snmp_alerts import (_iface, db, kinds, poll, rules, run, sent, site,  # noqa: F401
                              star)

GIG, FAST = 1000, 100


def link(*, speed=GIG, oper="up", errors=0, octets=0) -> InterfaceStat:
    """port1 on core-switch; `errors` is the running in-error counter."""
    return InterfaceStat(index=1, name="port1", admin_status="up", oper_status=oper,
                         speed_mbps=speed, in_octets=octets, out_octets=0, in_errors=errors,
                         out_errors=0, counters_are_64bit=True)


def at(minute: float, **kwargs) -> None:  # type: ignore[no-untyped-def]
    poll(minute, ports=[link(**kwargs)])


def iface() -> SnmpInterface:
    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            return await s.scalar(select(SnmpInterface).where(SnmpInterface.if_index == 1))
    return run(go())


def incidents() -> list[AlertIncident]:
    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            return list((await s.execute(select(AlertIncident).order_by(AlertIncident.id))).scalars())
    return run(go())


def starred_at_a_gig() -> None:
    at(0)
    star(1)
    at(1)                       # seen up at 1 Gbps while starred: its usual speed


class TestSlower:
    def test_slower_for_five_minutes_then_back(self, db):
        starred_at_a_gig()
        assert iface().expected_mbps == GIG
        for minute in range(2, 7):
            at(minute, speed=FAST)
        assert kinds() == [], "four minutes slow is a renegotiation"
        at(7, speed=FAST)
        assert kinds() == ["port_slow"]
        (slow,) = sent()
        assert slow.subject == "core-switch: port port1 is running at 100 Mbps, not 1 Gbps"
        assert "`10.0.0.2`" in slow.body and "failing cable" in slow.body
        assert incidents()[0].key == f"speed:{iface().id}"
        at(8, speed=FAST)
        assert kinds() == ["port_slow"], "once"
        at(9)
        assert kinds() == ["port_slow", "port_speed_ok"]
        assert sent()[1].subject == "core-switch: port port1 is back to 1 Gbps"

    def test_a_short_dip_is_nothing(self, db):
        starred_at_a_gig()
        at(2, speed=FAST)
        at(3, speed=FAST)
        at(4)
        for minute in range(5, 9):
            at(minute, speed=FAST)
        assert kinds() == []

    def test_down_is_the_other_rule(self, db):
        starred_at_a_gig()
        for minute in range(2, 10):
            at(minute, oper="down", speed=FAST)
        assert kinds() == ["port_down"]

    def test_faster_becomes_the_usual(self, db):
        at(0, speed=FAST)
        star(1)
        at(1, speed=FAST)
        assert iface().expected_mbps == FAST
        at(2)
        assert iface().expected_mbps == GIG, "it can do a gig, so a gig is usual"
        for minute in range(3, 9):
            at(minute, speed=FAST)
        assert kinds() == ["port_slow"]

    def test_unstarred_never(self, db):
        at(0)
        for minute in range(1, 10):
            at(minute, speed=FAST)
        assert kinds() == [] and iface().expected_mbps is None

    def test_accepting_the_new_speed_ends_it_without_a_recovery(self, db):
        starred_at_a_gig()
        for minute in range(2, 8):
            at(minute, speed=FAST)

        ident = iface().id

        async def accept():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                await snmp_alerts.accept_speed(s, await s.get(SnmpInterface, ident))
        run(accept())
        assert iface().expected_mbps == FAST
        (row,) = incidents()
        assert row.closed_at is not None and row.resolution == "accepted"
        for minute in range(8, 16):
            at(minute, speed=FAST)
        assert kinds() == ["port_slow"], "no recovery, and nothing more"

    def test_unstarring_forgets_the_usual_speed(self, db):
        starred_at_a_gig()

        ident = iface().id

        async def unstar():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                await snmp_alerts.unstar(s, await s.get(SnmpInterface, ident))
        run(unstar())
        assert iface().expected_mbps is None and not iface().starred

    def test_the_rule_switched_off(self, db):
        rules(port_speed=False)
        starred_at_a_gig()
        for minute in range(2, 10):
            at(minute, speed=FAST)
        assert kinds() == [] and incidents() == []

    def test_suppressed_for_the_device(self, db):
        async def off():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                s.add(AlertSuppression(device_id=1, rule="port_speed", threshold=None))
        run(off())
        starred_at_a_gig()
        for minute in range(2, 10):
            at(minute, speed=FAST)
        assert kinds() == []


def erroring(start: int, end: int, per_minute: int, *, total: int = 0) -> int:
    """Polls a minute apart from `start` to `end`, the counter rising by
    `per_minute` each time. Returns the counter at the end."""
    for minute in range(start, end + 1):
        total += per_minute
        at(minute, errors=total)
    return total


class TestErrors:
    def test_over_the_line_for_ten_minutes_then_quiet(self, db):
        starred_at_a_gig()
        total = erroring(2, 11, 20)
        assert kinds() == [], "nine minutes of errors"
        total = erroring(12, 12, 20, total=total)
        assert kinds() == ["port_errors"]
        (bad,) = sent()
        assert bad.subject == "core-switch: port port1 has errors (20 a minute)"
        assert "over 10 a minute for 10 minutes" in bad.body
        total = erroring(13, 14, 7, total=total)
        assert kinds() == ["port_errors"], "7 is under the line but not under half of it"
        erroring(15, 15, 2, total=total)
        assert kinds() == ["port_errors", "port_errors_ok"]
        assert sent()[1].subject == "core-switch: port port1 errors have stopped"

    def test_under_the_line_is_nothing(self, db):
        starred_at_a_gig()
        erroring(2, 30, 8)
        assert kinds() == []

    def test_its_own_line_for_one_device(self, db):
        async def line():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                s.add(AlertSuppression(device_id=1, rule="port_errors", threshold=50))
        run(line())
        starred_at_a_gig()
        erroring(2, 20, 20)
        assert kinds() == []

    def test_a_reboot_resets_nothing_into_a_spike(self, db):
        """A counter that goes backwards is a new baseline, not -1000 errors
        or a burst: no rate that poll, so neither a breach nor a clear."""
        starred_at_a_gig()
        total = erroring(2, 12, 20)
        assert kinds() == ["port_errors"]
        at(13, errors=0)                         # counter reset
        assert kinds() == ["port_errors"]
        erroring(14, 14, 1, total=0)
        assert kinds() == ["port_errors", "port_errors_ok"]
        assert total

    def test_the_rule_switched_off(self, db):
        rules(port_errors=False)
        starred_at_a_gig()
        erroring(2, 20, 50)
        assert kinds() == [] and incidents() == []

    def test_unstarred_never(self, db):
        at(0)
        erroring(1, 20, 50)
        assert kinds() == []


class TestPages:
    def test_the_rules_form(self, site):
        page = flat(site.get("/alerts/rules").text)
        assert 'name="port_speed" value="1" checked' in page
        assert 'name="port_errors" value="1" checked' in page
        assert 'name="port_errors_per_minute" min="1" max="10000" required value="10"' in page

    def test_saving_it_keeps_every_box_it_has(self, site):
        """Every checkbox on the form is saved as ticked, the Proxmox and
        internet ones included (they used to be dropped, which switched
        them off on every save)."""
        form = {"port_down": "1", "port_busy": "1", "port_busy_percent": "80",
                "port_busy_minutes": "10", "port_speed": "1", "port_errors": "1",
                "port_errors_per_minute": "25", "port_errors_minutes": "5",
                "cpu": "1", "cpu_percent": "90", "cpu_minutes": "10", "memory": "1",
                "memory_percent": "90", "memory_minutes": "10", "temperature": "1",
                "temperature_celsius": "80", "temperature_minutes": "5", "pool_health": "1",
                "pool_space": "1", "pool_space_percent": "85", "disk_space": "1",
                "disk_space_percent": "90", "space_trend": "1", "space_trend_days": "14",
                "drive_temperature": "1", "drive_celsius": "50",
                "drive_minutes": "10", "api_down": "1", "drive_errors": "1",
                "truenas_alerts": "1", "guest_down": "1", "internet_down": "1"}
        assert site.post("/settings/alerts/rules", data=form).status_code == 303

        async def saved():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                return await snmp_alerts.load(s)
        r = run(saved())
        assert all(r[k] is True for k, v in form.items() if v == "1"), \
            [k for k, v in form.items() if v == "1" and r[k] is not True]
        assert (r["port_errors_per_minute"], r["port_errors_minutes"]) == (25, 5)
        site.post("/settings/alerts/rules", data={k: v for k, v in form.items()
                                                  if k not in ("port_speed", "guest_down")})
        r = run(saved())
        assert r["port_speed"] is False and r["guest_down"] is False and r["internet_down"] is True
        bad = site.post("/settings/alerts/rules", data={**form, "port_errors_per_minute": "0"})
        assert bad.status_code == 400 and "Port errors must be between 1 and 10000." in bad.text

    def test_slower_says_so_and_can_be_accepted(self, site):
        port = _iface(1)
        site.post(f"/devices/1/interfaces/{port.id}/star", data={"starred": "1"})
        at(1)
        at(2, speed=FAST)
        page = flat(site.get(f"/devices/1?port={port.id}").text)
        assert '<span class="warn-text">Usually 1000 Mbps</span>' in page
        assert ">Accept 100 Mbps</button>" in page
        assert "usually 1000 Mbps" in flat(site.get("/alerts/rules").text), "on Starred ports too"
        answer = site.post(f"/devices/1/interfaces/{port.id}/speed", data={"back": "/devices/1"})
        assert answer.headers["location"] == "/devices/1"
        assert iface().expected_mbps == FAST
        assert "Usually 1000 Mbps" not in site.get(f"/devices/1?port={port.id}").text

    def test_accept_only_through_its_own_device_and_only_starred(self, site):
        port = _iface(1)
        at(1, speed=FAST)
        site.post(f"/devices/2/interfaces/{port.id}/speed", data={})
        site.post(f"/devices/1/interfaces/{port.id}/speed", data={})
        assert iface().expected_mbps is None, "not starred: nothing to accept"

    def test_unstarring_from_either_page_forgets_the_speed(self, site):
        port = _iface(1)
        site.post(f"/devices/1/interfaces/{port.id}/star", data={"starred": "1"})
        at(1)
        assert iface().expected_mbps == GIG
        site.post(f"/settings/alerts/ports/{port.id}/unstar")
        assert iface().expected_mbps is None
        site.post(f"/devices/1/interfaces/{port.id}/star", data={"starred": "1"})
        at(2)
        site.post(f"/devices/1/interfaces/{port.id}/star", data={"starred": "0"})
        assert iface().expected_mbps is None and not iface().starred

    def test_errors_take_a_line_on_suppressions(self, site):
        answer = site.post("/settings/suppressions", data={
            "device_id": "1", "rule": "port_errors", "mode": "line", "threshold": "500"})
        assert answer.status_code == 303
        page = flat(site.get("/alerts/suppressions").text)
        assert "A starred port has errors: alert over 500 a minute" in page
        answer = site.post("/settings/suppressions", data={
            "device_id": "1", "rule": "port_speed", "mode": "off"})
        assert "A starred port runs slower than usual: off" in flat(site.get("/alerts/suppressions").text)


def test_migration_24_from_a_version_23_database():
    from sqlalchemy import inspect, text

    from test_snmp_alerts import _config

    async def shape(wind_back: bool):  # type: ignore[no-untyped-def]
        config = _config()
        D.init_engine(config)
        await D.init_db(config)
        if wind_back:
            async with D.session_scope() as s:
                await s.execute(text("ALTER TABLE snmp_interface DROP COLUMN expected_mbps"))
                await D._set_version(s, 23)
            await D.close_engine()
            D.init_engine(config)
            await D.init_db(config)
        async with D.session_scope() as s:
            connection = await s.connection()
            columns = await connection.run_sync(
                lambda sync: sorted(c["name"] for c in inspect(sync).get_columns("snmp_interface")))
            version = await D._get_version(s)
        await D.close_engine()
        return columns, version

    migrated, fresh = run(shape(True)), run(shape(False))
    assert migrated == fresh and "expected_mbps" in fresh[0]
    assert fresh[1] == D.CURRENT_VERSION >= 24
