"""Excluded addresses: never pinged by the sweep, never port-scanned or used
as a port-scan control, never tried by Find SNMP. Nothing here touches the
network: every probe is replaced by one that records what it was asked."""

from __future__ import annotations

import asyncio
import ipaddress

import pytest
from fastapi.testclient import TestClient

from spark import db as D
from spark import exclusions, snmp_discover
from spark import subnets as subnet_service
from spark.discovery import runner, sweep
from spark.main import create_app
from spark.models import Device, utcnow
from spark.snmp_config import ProfileInput, save_profile
from spark.vault import vault_for

from test_credentials import PASSWORD, _config, flat, run


class TestParse:
    @pytest.mark.parametrize("spec, canon", [
        ("10.0.0.25", "10.0.0.25"),
        ("  10.0.0.25 ", "10.0.0.25"),
        ("10.0.0.100-10.0.0.150", "10.0.0.100-10.0.0.150"),
        ("10.0.0.100 - 10.0.0.150", "10.0.0.100-10.0.0.150"),
        ("10.0.0.7-10.0.0.7", "10.0.0.7"),
        ("fd00::1-fd00::ff", "fd00::1-fd00::ff"),
    ])
    def test_accepted(self, spec, canon):
        assert exclusions.canonical(spec) == canon

    @pytest.mark.parametrize("spec, why", [
        ("", "Enter an address"),
        ("10.0.0.0/24", "not an address or a range"),
        ("nas.home.arpa", "not an address or a range"),
        ("10.0.0.300", "not an address or a range"),
        ("10.0.0.150-10.0.0.100", "runs backwards"),
        ("10.0.0.1-fd00::1", "both be IPv4 or both IPv6"),
    ])
    def test_refused(self, spec, why):
        with pytest.raises(exclusions.ExclusionError, match=why):
            exclusions.parse(spec)

    def test_contains(self):
        ex = exclusions.Excluded((exclusions.parse("10.0.0.25"),
                                  exclusions.parse("10.0.0.100-10.0.0.150"),
                                  exclusions.parse("fd00::1-fd00::ff")))
        assert "10.0.0.25" in ex and "10.0.0.100" in ex and "10.0.0.150" in ex
        assert "10.0.0.99" not in ex and "10.0.0.151" not in ex and "10.0.0.24" not in ex
        assert "fd00::10" in ex and "fd00::100" not in ex
        assert "not an address" not in ex and None not in ex
        assert ex.keep(["10.0.0.24", "10.0.0.25", "10.0.0.26"]) == ["10.0.0.24", "10.0.0.26"]
        assert not exclusions.Excluded() and exclusions.Excluded().keep(["1.2.3.4"]) == ["1.2.3.4"]


# --------------------------------------------------------------------------
# The scans
# --------------------------------------------------------------------------


@pytest.fixture
def pings(monkeypatch):
    asked: list[str] = []

    async def ping_sweep(addresses, timeout=1.0):  # type: ignore[no-untyped-def]
        asked.extend(addresses)
        return [], True
    monkeypatch.setattr(sweep, "ping_sweep", ping_sweep)
    return asked


class TestSweep:
    def test_excluded_addresses_are_not_pinged(self, pings):
        ex = exclusions.Excluded((exclusions.parse("10.9.0.2-10.9.0.4"),))
        result = asyncio.run(sweep.sweep_subnet("10.9.0.0/29", excluded=ex, resolve_names=False))
        assert pings == ["10.9.0.1", "10.9.0.5", "10.9.0.6"]
        assert (result.probed, result.excluded, result.skipped) == (3, 3, None)

    def test_a_subnet_all_excluded_sends_nothing(self, pings):
        ex = exclusions.Excluded((exclusions.parse("10.9.0.1-10.9.0.6"),))
        result = asyncio.run(sweep.sweep_subnet("10.9.0.0/29", excluded=ex))
        assert pings == [] and (result.probed, result.excluded) == (0, 6)

    def test_the_scheduled_sweep_reads_them(self, pings):
        config = _config()

        async def go():  # type: ignore[no-untyped-def]
            D.init_engine(config)
            await D.init_db(config)
            async with D.session_scope() as s:
                await subnet_service.create(s, cidr="10.9.0.0/29")
                await exclusions.add(s, "10.9.0.3")
            await runner.run_sweep(config)
            async with D.session_scope() as s:
                state = await runner.last_sweep(s)
            await D.close_engine()
            return state
        state = asyncio.run(go())
        assert "10.9.0.3" not in pings and "10.9.0.2" in pings
        assert state["excluded"] == 1 and state["probed"] == 5


class TestPortScan:
    def test_excluded_devices_and_controls_are_left_out(self, monkeypatch):
        config = _config()
        scanned: list = []
        controlled: list = []

        async def scan_hosts(targets, ports, names=None):  # type: ignore[no-untyped-def]
            scanned.extend(targets)
            return []

        async def find_interception(controls, ports):  # type: ignore[no-untyped-def]
            controlled.extend(controls)
            return set()
        monkeypatch.setattr(runner, "scan_hosts", scan_hosts)
        monkeypatch.setattr(runner, "find_interception", find_interception)

        async def go():  # type: ignore[no-untyped-def]
            D.init_engine(config)
            await D.init_db(config)
            async with D.session_scope() as s:
                await subnet_service.create(s, cidr="10.9.1.0/28")
                s.add_all([Device(mac="aa:00:00:00:00:01", primary_ip="10.9.1.2", last_seen=utcnow()),
                           Device(mac="aa:00:00:00:00:02", primary_ip="10.9.1.3", last_seen=utcnow())])
                await exclusions.add(s, "10.9.1.3")
                await exclusions.add(s, "10.9.1.5-10.9.1.12")
            await runner.run_port_scan(config)
            await D.close_engine()
        asyncio.run(go())
        assert [ip for _, ip in scanned] == ["10.9.1.2"]
        ex = exclusions.Excluded((exclusions.parse("10.9.1.3"), exclusions.parse("10.9.1.5-10.9.1.12")))
        assert controlled and not [c for c in controlled if c in ex], controlled
        assert all(ipaddress.ip_address(c) in ipaddress.ip_network("10.9.1.0/28") for c in controlled)


class TestFindSnmp:
    def test_an_excluded_device_is_not_tried(self, monkeypatch):
        config = _config()
        asked: list[str] = []

        async def ask(address, credential):  # type: ignore[no-untyped-def]
            asked.append(address)
            return "silent", None
        monkeypatch.setattr(snmp_discover, "_ask", ask)

        async def go():  # type: ignore[no-untyped-def]
            D.init_engine(config)
            await D.init_db(config)
            async with D.session_scope() as s:
                s.add_all([Device(mac="aa:00:00:00:00:01", primary_ip="127.0.0.1", last_seen=utcnow()),
                           Device(mac="aa:00:00:00:00:02", primary_ip="127.0.0.2", last_seen=utcnow())])
                await save_profile(s, vault_for(config), ProfileInput(name="p", version="v2c",
                                                                      community="x"))
                await exclusions.add(s, "127.0.0.2")
            state = await snmp_discover.run_discovery(config)
            await D.close_engine()
            return state
        state = asyncio.run(go())
        assert asked == ["127.0.0.1"] and state["devices"] == 1


# --------------------------------------------------------------------------
# Settings -> Subnets
# --------------------------------------------------------------------------


@pytest.fixture
def site():
    config = _config()

    async def setup():  # type: ignore[no-untyped-def]
        D.init_engine(config)
        await D.init_db(config)
    run(setup())
    with TestClient(create_app(config), follow_redirects=False) as client:
        client.post("/setup", data={"setup_code": client.app.state.setup_code, "username": "admin",
                                    "password": PASSWORD, "password_confirm": PASSWORD,
                                    "timezone": "UTC"})
        yield client
    run(D.close_engine())


def saved():  # type: ignore[no-untyped-def]
    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            return await exclusions.entries(s)
    return run(go())


class TestSettings:
    def test_empty(self, site):
        page = flat(site.get("/settings").text)
        assert '<section class="card" id="exclusions">' in page
        assert "Nothing excluded: every address in a swept subnet is scanned." in page

    def test_add_and_remove(self, site):
        response = site.post("/settings/exclusions", data={"spec": "10.0.0.100 - 10.0.0.150",
                                                            "note": "lab PLC, logs every probe"})
        assert response.status_code == 303 and response.headers["location"] == "/settings#exclusions"
        site.post("/settings/exclusions", data={"spec": "10.0.0.25"})
        assert saved() == [{"spec": "10.0.0.100-10.0.0.150", "note": "lab PLC, logs every probe"},
                           {"spec": "10.0.0.25", "note": ""}]
        page = flat(site.get("/settings").text)
        assert "<td><code>10.0.0.100-10.0.0.150</code></td> <td class=\"muted\">lab PLC, logs every probe</td>" in page
        assert '<input type="hidden" name="spec" value="10.0.0.25">' in page
        site.post("/settings/exclusions/delete", data={"spec": "10.0.0.25"})
        assert [e["spec"] for e in saved()] == ["10.0.0.100-10.0.0.150"]

    def test_refused_says_why_and_keeps_what_was_typed(self, site):
        response = site.post("/settings/exclusions", data={"spec": "10.0.0.0/24", "note": "x"})
        page = flat(response.text)
        assert response.status_code == 400
        assert "is not an address or a range. Use 10.0.0.25 or 10.0.0.100-10.0.0.150." in page
        assert 'value="10.0.0.0/24"' in page and saved() == []
        site.post("/settings/exclusions", data={"spec": "10.0.0.25"})
        page = flat(site.post("/settings/exclusions", data={"spec": " 10.0.0.25"}).text)
        assert "10.0.0.25 is already excluded." in page and len(saved()) == 1

    def test_signed_out(self):
        config = _config()
        D.init_engine(config)
        run(D.init_db(config))
        with TestClient(create_app(config), follow_redirects=False) as client:
            response = client.post("/settings/exclusions", data={"spec": "10.0.0.25"})
            assert response.status_code in (302, 303, 401, 403)
        run(D.close_engine())


def test_the_devices_page_says_how_many_were_left_out(site):
    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            await D.save_setting(s, runner.STATE_KEY, {
                "finished_at": utcnow().isoformat(), "probed": 250, "answered": 10,
                "with_mac": 8, "new": 0, "excluded": 4, "icmp_available": True, "subnets": []})
    run(go())
    page = flat(site.get("/devices").text)
    assert '4 addresses left out, as <a href="/settings#exclusions">excluded</a>.' in page
