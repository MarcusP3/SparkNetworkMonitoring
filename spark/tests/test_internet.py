"""The Internet card: what one round of checks decides, the alert after two
down checks in a row (and not when the gateway's own target already says
it), the card, and switching it off. No real network: the pings and checks
are replaced, so nothing here reaches the internet."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from spark import db as D
from spark import internet
from spark.checks.base import CheckOutcome
from spark.main import create_app
from spark.models import (AlertIncident, CheckType, Device, DeviceRole, HealthStatus,
                          InternetSample, Target, utcnow)

from test_credentials import PASSWORD, _config, _sent, flat, run

UP_PING = internet.Ping(alive=True, avg_ms=12.0, jitter_ms=1.5, loss_percent=0.0)
NO_PING = internet.Ping(alive=False)
ALL_UP = {a: UP_PING for _, a in internet.RESOLVERS}
ALL_DOWN = {a: NO_PING for _, a in internet.RESOLVERS}


def decide(pings=None, **over):  # type: ignore[no-untyped-def]
    args = {"dns_local": True, "dns_public": True, "web": True, "web_ms": 80.0}
    args.update(over)
    return internet.decide(ALL_UP if pings is None else pings, **args)


class TestDecide:
    def test_all_well(self):
        r = decide()
        assert (r.state, r.reasons) == ("up", [])
        assert (r.latency_ms, r.jitter_ms, r.loss_percent, r.web_ms) == (12.0, 1.5, 0.0, 80.0)
        assert r.pings == {"1.1.1.1": 12.0, "8.8.8.8": 12.0, "9.9.9.9": 12.0}

    def test_one_provider_down_is_not_an_outage(self):
        r = decide({**ALL_UP, "9.9.9.9": NO_PING})
        assert r.state == "up" and r.pings["9.9.9.9"] is None

    def test_down_only_when_nothing_answers_and_the_web_fails(self):
        r = decide(ALL_DOWN, web=False, dns_local=False, dns_public=False)
        assert r.state == "down"
        assert r.detail == "No answer from 1.1.1.1, 8.8.8.8, 9.9.9.9, and the web check failed"
        r = decide(ALL_DOWN, web=False, gateway=NO_PING)
        assert r.detail.endswith("the gateway did not answer either")

    def test_icmp_blocked_but_the_web_works_is_degraded_not_down(self):
        r = decide(ALL_DOWN)
        assert r.state == "degraded" and "ICMP may be blocked" in r.detail

    @pytest.mark.parametrize("over, why", [
        ({"web": False}, "the web check failed"),
        ({"dns_local": False, "dns_public": False}, "DNS is not answering"),
        ({"dns_local": False}, "your DNS is not answering (a public resolver is)"),
        ({"dns_public": False}, "a public DNS resolver is not answering (yours is)"),
    ])
    def test_degraded_says_why(self, over, why):
        r = decide(**over)
        assert r.state == "degraded" and why in r.reasons

    def test_packet_loss(self):
        lossy = internet.Ping(alive=True, avg_ms=40.0, jitter_ms=9.0, loss_percent=25.0)
        r = decide({a: lossy for a in ALL_UP})
        assert r.state == "degraded" and r.reasons == ["25% packet loss"]
        r = decide({a: internet.Ping(True, 10.0, 1.0, 1.0) for a in ALL_UP})
        assert r.state == "up", "under the line"


def test_probe_runs_every_check(monkeypatch):
    seen = []

    async def ping(address):  # type: ignore[no-untyped-def]
        seen.append(("ping", address))
        return UP_PING if address != "192.168.1.1" else NO_PING

    async def check(kind, spec):  # type: ignore[no-untyped-def]
        seen.append((kind, spec.address, spec.params.get("server") or spec.params.get("expect_status")))
        return CheckOutcome.up(latency_ms=50.0)

    monkeypatch.setattr(internet, "ping", ping)
    monkeypatch.setattr(internet, "run_check", check)
    r = run(internet.probe("192.168.1.1"))
    assert r.state == "up" and r.gateway_ok is False and r.web_ms == 50.0
    assert ("dns", "example.com", None) in seen and ("dns", "example.com", "1.1.1.1") in seen
    assert ("http", internet.WEB_URL, 204) in seen
    assert {a for k, a, *_ in seen if k == "ping"} == {"1.1.1.1", "8.8.8.8", "9.9.9.9", "192.168.1.1"}


# --------------------------------------------------------------------------
# Recording, and the alert
# --------------------------------------------------------------------------


@pytest.fixture
def db():
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


DOWN = decide(ALL_DOWN, web=False, dns_local=False, dns_public=False)
UP = decide()


def record(reading, minutes=0):  # type: ignore[no-untyped-def]
    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            await internet.record(s, reading, utcnow() + timedelta(minutes=minutes))
    run(go())


def incidents():  # type: ignore[no-untyped-def]
    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            return [(i.key, i.title, i.closed_at is None)
                    for i in (await s.execute(select(AlertIncident))).scalars()]
    return run(go())


class TestAlert:
    def test_two_down_checks_alert_and_the_return_says_so(self, db):
        record(DOWN, 0)
        assert run(_sent()) == [], "one check is a blip"
        record(DOWN, 1)
        sent = run(_sent())
        assert sent[0][:2] == ("internet_down", "The internet is down")
        assert "No answer from 1.1.1.1" in sent[0][2]
        assert incidents() == [("internet", "The internet is down", True)]
        record(UP, 3)
        assert run(_sent())[-1][:2] == ("internet_ok", "The internet is back")
        assert incidents() == [("internet", "The internet is down", False)]

    def test_not_when_the_gateways_own_target_is_already_down(self, db):
        async def watch():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                s.add(Target(name="router", address="192.168.1.1", check_type=CheckType.PING,
                             device_id=1, status=HealthStatus.DOWN))
        run(watch())
        down = decide(ALL_DOWN, web=False, gateway=NO_PING)
        record(down, 0)
        record(down, 1)
        assert run(_sent()) == [], "the router's alert says it already"
        assert incidents() == [("internet", "The internet is down", True)], "still on the record"
        record(UP, 3)
        assert run(_sent()) == [], "no recovery for an alert never sent"

    def test_the_rule_switched_off(self, db):
        async def off():  # type: ignore[no-untyped-def]
            from spark.db import get_setting, save_setting
            async with D.session_scope() as s:
                rules = await get_setting(s, "alert_rules")
                await save_setting(s, "alert_rules", {**rules, "internet_down": False})
        run(off())
        record(DOWN, 0)
        record(DOWN, 1)
        assert run(_sent()) == [] and incidents() == []

    def test_switching_the_checks_off(self, db, monkeypatch):
        record(DOWN, 0)
        record(DOWN, 1)

        async def off():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                await internet.set_enabled(s, False)
        run(off())
        assert incidents() == [("internet", "The internet is down", False)], "closed, not left open"

        async def boom(*a, **k):  # type: ignore[no-untyped-def]
            raise AssertionError("no checks while off")
        monkeypatch.setattr(internet, "probe", boom)
        assert run(internet.run()) is None

    def test_a_round_is_stored(self, db, monkeypatch):
        async def probe(gateway_ip):  # type: ignore[no-untyped-def]
            assert gateway_ip == "192.168.1.1", "the Gateway / router device is pinged too"
            return UP
        monkeypatch.setattr(internet, "probe", probe)
        assert run(internet.run()).state == "up"

        async def rows():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                return list((await s.execute(select(InternetSample))).scalars())
        [row] = run(rows())
        assert (row.state, row.latency_ms, row.web_ok, row.pings["8.8.8.8"]) == ("up", 12.0, True, 12.0)

    def test_old_samples_are_pruned(self, db):
        record(UP, -60 * 24 * 31)
        record(UP, 0)

        async def go():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                return await internet.prune(s, utcnow())
        assert run(go()) == 1


# --------------------------------------------------------------------------
# The card
# --------------------------------------------------------------------------


@pytest.fixture
def site(db):
    with TestClient(create_app(db), follow_redirects=False) as client:
        client.post("/setup", data={"setup_code": client.app.state.setup_code, "username": "admin",
                                    "password": PASSWORD, "password_confirm": PASSWORD,
                                    "timezone": "UTC"})
        yield client


class TestCard:
    def test_before_the_first_check(self, site):
        page = flat(site.get("/").text)
        assert '<section class="card inet-card" id="internet">' in page
        assert '<span class="pill neutral">Checking…</span>' in page
        assert "The first check runs within a minute" in page

    def test_the_card(self, site):
        for minute in range(-10, 0):
            record(UP, minute)
        record(DOWN, 0)
        record(DOWN, 1)
        record(decide({**ALL_UP, "9.9.9.9": NO_PING}, dns_local=False), 2)
        page = flat(site.get("/").text)
        assert '<span class="pill warn dot">Degraded</span>' in page
        assert "your DNS is not answering (a public resolver is)." in page
        assert "Quad9 <code>9.9.9.9</code> <span class=\"muted\">no reply</span>" in page
        assert "Cloudflare <code>1.1.1.1</code> <span class=\"muted\">12 ms</span>" in page
        assert ('<span class="muted small">Up, 24 hours</span><strong>84.62%</strong>' in page), \
            "11 of 13 checks were not down"
        assert "<h3 class=\"sub-title\">Outages</h3>" in page
        assert '<svg class="trend' in page

    def test_off_and_on(self, site):
        response = site.post("/internet/enabled", data={"on": "0"})
        assert response.headers["location"] == "/#internet"
        page = flat(site.get("/").text)
        assert '<span class="pill neutral">Off</span>' in page
        assert "Off: SPARK is not contacting any outside host for this." in page
        assert '<input type="hidden" name="on" value="1">' in page and ">Turn on</button>" in page
        site.post("/internet/enabled", data={"on": "1"})
        assert ">Turn off</button>" in site.get("/").text

    def test_the_rule_is_on_the_alerts_form(self, site):
        page = flat(site.get("/settings/alerts").text)
        assert 'name="internet_down" value="1" checked' in page
        assert "<span>The internet is down</span>" in page


def test_migration_21_from_a_version_20_database():
    from sqlalchemy import inspect, text

    async def shape(wind_back: bool):  # type: ignore[no-untyped-def]
        config = _config()
        D.init_engine(config)
        await D.init_db(config)
        if wind_back:
            async with D.session_scope() as s:
                await s.execute(text("DROP TABLE internet_sample"))
                await D._set_version(s, 20)
            await D.close_engine()
            D.init_engine(config)
            await D.init_db(config)
        async with D.session_scope() as s:
            connection = await s.connection()
            columns = await connection.run_sync(
                lambda sync: sorted(c["name"] for c in inspect(sync).get_columns("internet_sample")))
            version = await D._get_version(s)
        await D.close_engine()
        return columns, version

    assert run(shape(True)) == run(shape(False))
    assert run(shape(False))[1] == D.CURRENT_VERSION == 21
