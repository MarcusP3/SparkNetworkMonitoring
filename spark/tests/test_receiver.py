"""SNMP traps and syslog from devices (receiver.py).

Parsing: syslog in RFC 3164 and 5424, traps v1 and v2c by the low-level
API; what cannot be read is counted, not stored. Listening: real UDP, only
devices SPARK knows, 120 a minute each, and a clear reason when a port
cannot be had. Rules: a match opens one incident and sends one alert, more
are counted, quiet minutes close it. The image and the compose file agree
on the capability, or the container would not start.
"""

from __future__ import annotations

import asyncio
import errno
import re
import socket
from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from pyasn1.codec.ber import encoder
from pysnmp.proto import api
from sqlalchemy import select, text

from spark import db as D
from spark import receiver
from spark.config import Config
from spark.main import create_app
from spark.models import (AlertIncident, AlertMute, Device, DeviceAddress, DeviceEvent, EventRule,
                          MaintenanceWindow, Notification, utcnow)

from test_alerts import _config
from test_credentials import flat

PASSWORD = "correct horse battery"
SPARK = Path(__file__).resolve().parents[1]


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def trap_v2c(oid: tuple, *varbinds: tuple) -> bytes:
    p = api.PROTOCOL_MODULES[api.SNMP_VERSION_2C]
    pdu = p.TrapPDU()
    p.apiTrapPDU.set_defaults(pdu)
    p.apiTrapPDU.set_varbinds(pdu, [
        (p.ObjectIdentifier((1, 3, 6, 1, 2, 1, 1, 3, 0)), p.TimeTicks(1234)),
        (p.ObjectIdentifier((1, 3, 6, 1, 6, 3, 1, 1, 4, 1, 0)), p.ObjectIdentifier(oid)),
        *varbinds])
    msg = p.Message()
    p.apiMessage.set_defaults(msg)
    p.apiMessage.set_community(msg, "public")
    p.apiMessage.set_pdu(msg, pdu)
    return encoder.encode(msg)


def trap_v1(enterprise: tuple, generic: int, specific: int = 0) -> bytes:
    p = api.PROTOCOL_MODULES[api.SNMP_VERSION_1]
    pdu = p.TrapPDU()
    p.apiTrapPDU.set_defaults(pdu)
    p.apiTrapPDU.set_enterprise(pdu, enterprise)
    p.apiTrapPDU.set_generic_trap(pdu, generic)
    p.apiTrapPDU.set_specific_trap(pdu, specific)
    msg = p.Message()
    p.apiMessage.set_defaults(msg)
    p.apiMessage.set_community(msg, "public")
    p.apiMessage.set_pdu(msg, pdu)
    return encoder.encode(msg)


def get_request() -> bytes:
    p = api.PROTOCOL_MODULES[api.SNMP_VERSION_2C]
    pdu = p.GetRequestPDU()
    p.apiPDU.set_defaults(pdu)
    msg = p.Message()
    p.apiMessage.set_defaults(msg)
    p.apiMessage.set_pdu(msg, pdu)
    return encoder.encode(msg)


ON_BATTERY = trap_v2c((1, 3, 6, 1, 2, 1, 33, 2, 1))
p2 = api.PROTOCOL_MODULES[api.SNMP_VERSION_2C]
LINK_DOWN = trap_v2c((1, 3, 6, 1, 6, 3, 1, 1, 5, 3),
                     (p2.ObjectIdentifier((1, 3, 6, 1, 2, 1, 2, 2, 1, 1, 5)), p2.Integer(5)),
                     (p2.ObjectIdentifier((1, 3, 6, 1, 2, 1, 31, 1, 1, 1, 1, 5)), p2.OctetString("eth5")))


class TestSyslog:
    @pytest.mark.parametrize("line, severity, title, message", [
        (b"<13>Oct 20 13:00:00 fw sshd[123]: Failed password for root", 5, "sshd",
         "Failed password for root"),
        (b"<11>Oct  2 03:04:05 nas kernel: ata3: link is slow", 3, "kernel", "ata3: link is slow"),
        (b'<165>1 2026-10-20T13:00:00Z nas truenas 42 ID47 [ex@1 a="1"][b@2 c="\\]"] Pool DEGRADED',
         5, "truenas", "Pool DEGRADED"),
        (b"<30>1 2026-10-20T13:00:00Z host app - - - plain", 6, "app", "plain"),
        (b"<30>1 2026-10-20T13:00:00Z host - - - -", 6, "syslog", ""),
        (b"no priority at all", 5, "syslog", "no priority at all"),
        (b"<999>too high", 5, "syslog", "<999>too high"),
        ("<12>﻿Oct 20 13:00:00 h app: café".encode(), 4, "app", "café"),
    ])
    def test_reads(self, line, severity, title, message):
        parsed = receiver.parse_syslog(line)
        assert (parsed.source, parsed.severity, parsed.title, parsed.text) == \
            ("syslog", severity, title, message)

    def test_cut_to_a_kilobyte_and_bad_bytes_kept_readable(self):
        parsed = receiver.parse_syslog(b"<14>app: " + b"x" * 5000 + b"\xff")
        assert len(parsed.text) == receiver.MAX_TEXT

    def test_empty(self):
        with pytest.raises(receiver.Unreadable):
            receiver.parse_syslog(b"\x00\r\n")


class TestTraps:
    def test_v2c_with_names(self):
        parsed = receiver.parse_trap(LINK_DOWN)
        assert (parsed.source, parsed.title, parsed.severity, parsed.oid) == \
            ("trap", "linkDown", 4, "1.3.6.1.6.3.1.1.5.3")
        assert parsed.text == "ifIndex.5 = 5; ifName.5 = eth5", "uptime and the trap OID left out"

    def test_a_ups_on_battery_is_critical(self):
        parsed = receiver.parse_trap(ON_BATTERY)
        assert (parsed.severity, parsed.title) == (2, "upsTrapOnBattery")

    def test_v1_generic_and_enterprise_specific(self):
        assert receiver.parse_trap(trap_v1((1, 3, 6, 1, 4, 1, 9), 2)).title == "linkDown"
        vendor = receiver.parse_trap(trap_v1((1, 3, 6, 1, 4, 1, 318), 6, 5))
        assert vendor.oid == "1.3.6.1.4.1.318.0.5" and vendor.title == vendor.oid

    @pytest.mark.parametrize("data, why", [
        (b"garbage", "not SNMP"),
        (b"\x30\x03\x02\x01\x03", "SNMPv3"),
    ])
    def test_unreadable(self, data, why):
        with pytest.raises(receiver.Unreadable, match=why):
            receiver.parse_trap(data)

    def test_not_a_trap(self):
        with pytest.raises(receiver.Unreadable, match="not a trap"):
            receiver.parse_trap(get_request())


# --------------------------------------------------------------------------
# Listening, for real, on loopback
# --------------------------------------------------------------------------


@pytest.fixture
def db():
    config = _config()

    async def setup():  # type: ignore[no-untyped-def]
        D.init_engine(config)
        await D.init_db(config)
        async with D.session_scope() as s:
            s.add_all([Device(mac="aa:00:00:00:00:01", primary_ip="127.0.0.1", friendly_name="ups",
                              last_seen=utcnow()),
                       Device(mac="aa:00:00:00:00:02", primary_ip="10.0.0.2", friendly_name="fw",
                              last_seen=utcnow())])
    run(setup())
    yield config
    run(D.close_engine())


def stored() -> list[DeviceEvent]:
    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            return list((await s.execute(select(DeviceEvent).order_by(DeviceEvent.id))).scalars())
    return run(go())


async def _listen_and_send(packets: list[tuple[str, bytes]], *, wait: float = 0.5):  # type: ignore[no-untyped-def]
    r = receiver.Receiver("127.0.0.1", traps=receiver.Listener(0, True),
                          syslog=receiver.Listener(0, True))
    await r.start()
    try:
        out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for kind, data in packets:
            out.sendto(data, ("127.0.0.1", r.listeners[kind].port))
        out.close()
        for _ in range(int(wait * 20)):
            await asyncio.sleep(0.05)
            if r.counts.stored + r.counts.unknown + sum(r.counts.unreadable.values()) >= len(packets):
                break
    finally:
        await r.stop()
    return r


class TestListening:
    def test_a_trap_and_a_log_line_over_udp(self, db):
        r = run(_listen_and_send([("trap", ON_BATTERY),
                                  ("syslog", b"<11>Oct 20 13:00:00 ups apcupsd: Power failure.")]))
        assert {l.state for l in r.listeners.values()} == {"listening"}
        events = stored()
        assert [(e.source, e.title, e.severity, e.device_id, e.address) for e in events] == [
            ("trap", "upsTrapOnBattery", 2, 1, "127.0.0.1"),
            ("syslog", "apcupsd", 3, 1, "127.0.0.1")]
        assert r.counts.received == 2 and r.counts.stored == 2

    def test_a_stranger_is_counted_not_kept(self, db):
        async def forget():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                (await s.get(Device, 1)).primary_ip = "10.0.0.1"
        run(forget())
        r = run(_listen_and_send([("syslog", b"<11>who: am I")]))
        assert stored() == [] and r.counts.unknown == 1 and "127.0.0.1" in r.counts.strangers

    def test_a_merged_address_counts_as_its_device(self, db):
        async def merge():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                (await s.get(Device, 1)).primary_ip = "10.0.0.1"
                s.add(DeviceAddress(device_id=2, ip="127.0.0.1"))
        run(merge())
        run(_listen_and_send([("syslog", b"<11>fw: blocked")]))
        assert [e.device_id for e in stored()] == [2]

    def test_unreadable_is_counted(self, db):
        r = run(_listen_and_send([("trap", b"garbage"), ("trap", get_request())]))
        assert stored() == [] and r.counts.unreadable == {"not SNMP": 1, "not a trap": 1}

    def test_each_sender_gets_its_share(self):
        r = receiver.Receiver("127.0.0.1", traps=receiver.Listener(0, False),
                              syslog=receiver.Listener(0, False))
        took = [r.feed("syslog", b"<11>x", "10.0.0.5", now=1.0) for _ in range(125)]
        assert took.count(True) == receiver.PER_SENDER_PER_MINUTE and r.counts.limited == 5
        assert r.feed("syslog", b"<11>x", "10.0.0.6", now=1.0), "another sender is not held"
        assert r.feed("syslog", b"<11>x", "10.0.0.5", now=62.0), "a minute later, again"
        assert r.feed("syslog", b"<11>x", "::ffff:10.0.0.7", now=1.0)

    def test_a_big_datagram_is_cut_before_it_is_queued(self):
        r = receiver.Receiver("127.0.0.1", traps=receiver.Listener(0, False),
                              syslog=receiver.Listener(0, False))
        r.feed("syslog", b"<11>x: " + b"y" * 70_000, "10.0.0.5", now=1.0)
        assert len(r._queue.get_nowait()[1]) == receiver.MAX_DATAGRAM

    def test_a_port_it_may_not_have_says_how_to_get_it(self, monkeypatch):
        async def go():  # type: ignore[no-untyped-def]
            loop = asyncio.get_running_loop()

            async def refuse(*a, **k):  # type: ignore[no-untyped-def]
                raise PermissionError(errno.EACCES, "Permission denied")
            monkeypatch.setattr(loop, "create_datagram_endpoint", refuse)
            r = receiver.Receiver("0.0.0.0", traps=receiver.Listener(162, True),
                                  syslog=receiver.Listener(514, False))
            await r.start()
            return r
        r = run(go())
        trap = r.listeners["trap"]
        assert trap.state == "failed" and "SETCAP_NET_BIND_SERVICE=1" in trap.error
        assert "NET_BIND_SERVICE to cap_add" in trap.error
        assert r.listeners["syslog"].state == "off"

    def test_a_port_in_use_says_so(self):
        taken = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        taken.bind(("127.0.0.1", 0))
        port = taken.getsockname()[1]
        try:
            async def go():  # type: ignore[no-untyped-def]
                r = receiver.Receiver("127.0.0.1", traps=receiver.Listener(port, True),
                                      syslog=receiver.Listener(0, False))
                await r.start()
                await r.stop()
                return r
            r = run(go())
        finally:
            taken.close()
        assert r.listeners["trap"].state == "failed"
        assert r.listeners["trap"].error == f"Port {port} is already in use on this machine."


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------


def rule(**over) -> None:  # type: ignore[no-untyped-def]
    fields = {"name": "A UPS went on battery", "source": "trap", "contains": "upsTrapOnBattery",
              "max_severity": 7, "quiet_minutes": 30, **over}

    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            s.add(EventRule(**fields))
    run(go())


def take(*packets: tuple[str, bytes], at=None):  # type: ignore[no-untyped-def]
    r = receiver.Receiver("127.0.0.1", traps=receiver.Listener(0, False),
                          syslog=receiver.Listener(0, False))
    when = at or utcnow()
    return run(r.take([(kind, data, "127.0.0.1", when) for kind, data in packets]))


def sent() -> list[Notification]:
    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            return list((await s.execute(select(Notification).order_by(Notification.id))).scalars())
    return run(go())


def incidents() -> list[AlertIncident]:
    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            return list((await s.execute(select(AlertIncident).order_by(AlertIncident.id))).scalars())
    return run(go())


def expire(at) -> int:  # type: ignore[no-untyped-def]
    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            return await receiver.expire(s, at)
    return run(go())


class TestRules:
    def test_the_matching(self):
        r = EventRule(name="x", source="syslog", device_id=None, contains="Failed",
                      max_severity=4, quiet_minutes=30, enabled=True)
        hit = receiver.Parsed("syslog", 3, "sshd", "Failed password")
        assert receiver.matches(r, 1, hit)
        assert not receiver.matches(r, 1, receiver.Parsed("syslog", 6, "sshd", "Failed password")), "too mild"
        assert not receiver.matches(r, 1, receiver.Parsed("trap", 3, "sshd", "Failed password"))
        assert not receiver.matches(r, 1, receiver.Parsed("syslog", 3, "sshd", "Accepted"))
        r.device_id = 2
        assert not receiver.matches(r, 1, hit) and receiver.matches(r, 2, hit)
        r.enabled = False
        assert not receiver.matches(r, 2, hit)

    def test_one_alert_then_counted_then_quiet_again(self, db):
        rule()
        t0 = utcnow()
        take(("trap", ON_BATTERY), at=t0)
        [n] = sent()
        assert n.subject == "ups: A UPS went on battery" and n.kind == "event" and n.tone == "bad"
        assert "critical trap. upsTrapOnBattery" in n.body
        take(("trap", ON_BATTERY), ("trap", ON_BATTERY), at=t0 + timedelta(minutes=5))
        assert len(sent()) == 1, "counted, not sent again"
        [incident] = incidents()
        assert incident.key == "event:1:1" and incident.closed_at is None
        assert incident.detail.startswith("`127.0.0.1` — critical trap. upsTrapOnBattery")
        assert incident.detail.endswith("(3 so far)")
        assert expire(t0 + timedelta(minutes=34)) == 0, "30 quiet minutes from the last"
        assert expire(t0 + timedelta(minutes=36)) == 1
        assert incidents()[0].resolution == "quiet"
        quiet = sent()[-1]
        assert quiet.subject == "ups: A UPS went on battery — quiet again" and quiet.tone == "ok"
        assert quiet.body == "3 matching events over 5m 0s; none for 30 minutes since."

    def test_one_event_then_quiet_says_so_plainly(self, db):
        rule(quiet_minutes=1)
        t0 = utcnow()
        take(("trap", ON_BATTERY), at=t0)
        expire(t0 + timedelta(minutes=2))
        assert sent()[-1].body == "One matching event; none for 1 minute since."

    def test_stored_without_a_rule_sends_nothing(self, db):
        take(("syslog", b"<11>Oct 20 13:00:00 ups kernel: oops"))
        assert len(stored()) == 1 and sent() == [] and incidents() == []

    def test_muted_or_under_maintenance_is_recorded_not_sent(self, db):
        rule()

        async def mute():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                s.add(AlertMute(device_id=1))
        run(mute())
        take(("trap", ON_BATTERY))
        assert sent() == [] and len(incidents()) == 1

    def test_maintenance_holds_it_and_its_end_sends_it(self, db):
        from spark import maintenance
        rule()
        t0 = utcnow()

        async def window():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                s.add(MaintenanceWindow(device_id=1, starts_at=t0 - timedelta(minutes=1),
                                        ends_at=t0 + timedelta(minutes=10)))
        run(window())
        take(("trap", ON_BATTERY), at=t0)
        assert sent() == []

        async def finish():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                await maintenance.finish(s, t0 + timedelta(minutes=11))
        run(finish())
        assert [n.subject for n in sent()] == ["ups: A UPS went on battery"]

    def test_a_removed_rule_closes_its_incident_silently(self, db):
        rule()
        take(("trap", ON_BATTERY))

        async def remove():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                await s.delete(await s.get(EventRule, 1))
        run(remove())
        assert expire(utcnow()) == 1
        assert incidents()[0].resolution == "no longer watched" and len(sent()) == 1

    def test_housekeeping_drops_old_events(self, db):
        take(("syslog", b"<11>a: old"), at=utcnow() - timedelta(days=31))
        take(("syslog", b"<11>a: new"))
        config = Config.model_validate({"app": {"data_dir": str(db.app.data_dir)}})
        run(receiver.housekeeping(config))
        assert [e.text for e in stored()] == ["new"]


# --------------------------------------------------------------------------
# The page, the image and the config
# --------------------------------------------------------------------------


@pytest.fixture
def site(db):
    with TestClient(create_app(db), follow_redirects=False) as client:
        client.post("/setup", data={"setup_code": client.app.state.setup_code, "username": "admin",
                                    "password": PASSWORD, "password_confirm": PASSWORD,
                                    "timezone": "UTC"})
        yield client


RULE = {"name": "Failed sign-ins", "source": "syslog", "device_id": "", "contains": "failed password",
        "max_severity": "7", "quiet_minutes": "30"}


class TestPage:
    def test_off_by_default_and_says_how(self, site):
        page = flat(site.get("/device-events").text)
        assert '<a href="/device-events" class="active">Events</a>' in page
        assert page.count('<span class="pill neutral">off</span>') == 2
        assert "events.traps: true in spark.yaml to listen" in page
        assert "No event alerts yet" in page and "+ A UPS went on battery" in page

    def test_events_listed_and_filtered(self, site):
        def recent(query: str = "") -> str:   # the Recent card, not the presets below it
            page = flat(site.get("/device-events" + query).text)
            return page[page.index('id="events"'):page.index('id="rules"')]
        take(("trap", ON_BATTERY), ("syslog", b"<14>Oct 20 13:00:00 ups app: hello"))
        page = recent()
        assert "upsTrapOnBattery" in page and '<span class="pill bad">critical</span>' in page
        assert "2 events." in page
        page = recent("?severity=3")
        assert "upsTrapOnBattery" in page and "hello" not in page and "1 event." in page
        page = recent("?source=syslog")
        assert "hello" in page and "upsTrapOnBattery" not in page
        assert "Nothing yet with these filters" in recent("?source=trap&severity=0")

    def test_paged_fifty_at_a_time(self, site):
        take(*[("syslog", f"<14>app: line {n}".encode()) for n in range(51)])
        page = flat(site.get("/device-events?source=syslog").text)
        assert "Page 1 of 2" in page and "line 50" in page and "line 0<" not in page
        assert 'href="/device-events?source=syslog&amp;page=2#events" rel="next"' in page
        page = flat(site.get("/device-events?source=syslog&page=2").text)
        assert "line 0<" in page and 'href="/device-events?source=syslog#events" rel="prev"' in page
        assert "Page 2 of 2" in flat(site.get("/device-events?page=999").text), "past the end: the last"

    def test_the_device_page_card(self, site):
        take(("trap", ON_BATTERY))
        page = flat(site.get("/devices/1").text)
        assert '<section class="card" id="device-events">' in page and "upsTrapOnBattery" in page
        assert 'href="/device-events?device=1#events"' in page
        assert 'id="device-events"' not in site.get("/devices/2").text, "nothing to show, no card"

    def test_rules_added_switched_and_removed(self, site):
        assert site.post("/device-events/rules", data=RULE).status_code == 303
        page = flat(site.get("/device-events").text)
        assert "Syslog from any device, containing “failed password”" in page
        assert "+ Failed sign-ins" not in page, "the preset it matches is not offered again"
        site.post("/device-events/rules/1/toggle")
        assert '<span class="pill neutral">off</span></td>' in flat(site.get("/device-events").text)
        site.post("/device-events/rules/1/delete")
        assert "No event alerts yet" in site.get("/device-events").text

    @pytest.mark.parametrize("over, message", [
        ({"name": " "}, "Give the alert a name"),
        ({"source": "email"}, "traps, syslog or either"),
        ({"device_id": "99"}, "Choose a device"),
        ({"max_severity": "9"}, "severity from the list"),
        ({"quiet_minutes": "0"}, "from 1 to 1440"),
        ({"source": "any", "contains": ""}, "alert on everything"),
    ])
    def test_refused_says_why_and_keeps_it(self, site, over, message):
        answer = site.post("/device-events/rules", data={**RULE, **over})
        assert answer.status_code == 400 and message in answer.text

    def test_signed_out(self, db):
        with TestClient(create_app(db), follow_redirects=False) as client:
            assert client.get("/device-events").status_code in (302, 303, 307)
            assert client.post("/device-events/rules", data=RULE).status_code in (302, 303, 307)


class TestImage:
    def test_the_capability_is_opt_in_and_the_compose_file_agrees(self):
        dockerfile = (SPARK / "Dockerfile").read_text()
        assert "ARG SETCAP_NET_BIND_SERVICE=0" in dockerfile
        assert 'caps="${caps:+$caps,}cap_net_bind_service"' in dockerfile
        compose = yaml.safe_load((SPARK / "docker-compose.yml").read_text())["services"]["spark"]
        args, caps = compose["build"]["args"], compose["cap_add"]
        assert args["SETCAP_NET_BIND_SERVICE"] == "0"
        # A file capability the kernel cannot grant stops the container
        # starting: whichever is built in has to be added too.
        if args["SETCAP_NET_RAW"] == "1":
            assert "NET_RAW" in caps
        if args["SETCAP_NET_BIND_SERVICE"] == "1":
            assert "NET_BIND_SERVICE" in caps
        assert compose["cap_drop"] == ["ALL"]

    def test_the_compose_file_says_how(self):
        compose = (SPARK / "docker-compose.yml").read_text()
        assert re.search(r"#\s*-\s*NET_BIND_SERVICE", compose), "there, commented, to switch on"


class TestConfig:
    def test_off_by_default(self):
        ev = Config().events
        assert (ev.traps, ev.trap_port, ev.syslog, ev.syslog_port, ev.keep_days) == (False, 162, False, 514, 30)

    def test_bounds(self):
        with pytest.raises(ValueError):
            Config.model_validate({"events": {"trap_port": 70000}})

    def test_the_example_config_has_it(self):
        example = yaml.safe_load((SPARK / "config" / "spark.example.yaml").read_text())
        assert example["events"] == {"traps": False, "trap_port": 162, "syslog": False,
                                     "syslog_port": 514, "keep_days": 30}


def test_migration_28_from_a_version_27_database():
    from sqlalchemy import inspect

    async def shape(wind_back: bool):  # type: ignore[no-untyped-def]
        config = _config()
        D.init_engine(config)
        await D.init_db(config)
        if wind_back:
            async with D.session_scope() as s:
                await s.execute(text("DROP TABLE device_event"))
                await s.execute(text("DROP TABLE event_rule"))
                await D._set_version(s, 27)
            await D.close_engine()
            D.init_engine(config)
            await D.init_db(config)
        async with D.session_scope() as s:
            connection = await s.connection()
            tables = await connection.run_sync(lambda sync: {
                t: sorted(c["name"] for c in inspect(sync).get_columns(t))
                for t in ("device_event", "event_rule")})
            version = await D._get_version(s)
        await D.close_engine()
        return tables, version

    assert run(shape(True)) == run(shape(False))
    assert run(shape(False))[1] == D.CURRENT_VERSION >= 28
