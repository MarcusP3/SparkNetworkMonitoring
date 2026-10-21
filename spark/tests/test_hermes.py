"""Hermes Agent, by its dashboard's /api/status (checks/hermes.py).

The verdict: down when the gateway is not running, a platform failed for
good, the Nous sign-in ended or the session database is corrupt; degraded
for the rest Hermes counts against itself and critical pressure; up
otherwise. Payloads are in the shape the dashboard's status router builds
(hermes_cli/web_routers/status.py). Then a real local server, the
readings the runner keeps, the alert, the card and the form.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import socket
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from spark import db as D
from spark import hermes_scan
from spark.checks import hermes
from spark.checks.base import CheckSpec
from spark.checks.net import run_check
from spark.discovery.ports import port_name
from spark.engine import runner
from spark.main import create_app
from spark.models import (CheckResult, CheckType, Device, HealthStatus, Notification, Service, Target,
                          utcnow)

from test_alerts import _config
from test_credentials import flat

PASSWORD = "correct horse battery"

HEALTHY = {
    "version": "0.21.3", "release_date": "2026-09-30", "config_version": 41,
    "latest_config_version": 41, "can_update_hermes": True,
    "gateway_running": True, "gateway_state": "running",
    "gateway_platforms": {
        "telegram": {"state": "connected", "error_code": None, "error_message": None,
                     "updated_at": "2026-10-20T13:00:00Z"},
        "discord": {"state": "connected"},
    },
    "gateway_exit_reason": None, "gateway_updated_at": "2026-10-20T13:00:00Z",
    "gateway_heartbeat_stale_s": None, "gateway_shared_with": None, "active_agents": 0,
    "gateway_busy": False, "gateway_drainable": True, "restart_drain_timeout": 60,
    "active_sessions": 1, "auth_required": True, "auth_providers": ["password"],
    "auth_flows": ["cookie"], "nous_session_valid": "unknown", "install_id": "abc",
    "shared_profile_warning": False,
    "components": {"gateway": {"status": "ok", "state": "running"}, "dashboard": {"status": "ok"},
                   "storage": {"status": "ok"},
                   "platforms": {"status": "ok", "configured": 2, "connected": 2}},
    "overall": "ok",
    "memory": {"pressure": "ok", "gateway_rss_mb": 310, "system_total_mb": 7900,
               "system_available_mb": 5100},
    "disk": {"pressure": "ok", "total_mb": 60000, "free_mb": 41000, "used_percent": 31.7},
    "profiles": ["default"], "parked_profiles": [], "gateway_mode": "single",
}


def status(**over) -> dict:  # type: ignore[no-untyped-def]
    s = copy.deepcopy(HEALTHY)
    for key, value in over.items():
        s[key] = value
    return s


def platform(name: str, state: str, message: str | None = None) -> dict:
    s = copy.deepcopy(HEALTHY)
    s["gateway_platforms"][name] = {"state": state, "error_code": "x" if message else None,
                                    "error_message": message}
    s["components"]["platforms"]["status"] = "degraded"
    s["overall"] = "degraded"
    return s


def verdict(s: dict) -> tuple[str, str]:
    o = hermes.judge(s)
    return ("down" if not o.ok else "degraded" if o.degraded else "up"), o.detail or ""


class TestAddress:
    @pytest.mark.parametrize("address, url", [
        ("10.0.0.5", "http://10.0.0.5:9119/api/status"),
        ("agent.lan:9200", "http://agent.lan:9200/api/status"),
        ("http://10.0.0.5:9119/", "http://10.0.0.5:9119/api/status"),
        ("https://agent.example/hermes", "https://agent.example/hermes/api/status"),
        ("http://10.0.0.5:9119/api/status", "http://10.0.0.5:9119/api/status"),
        ("fd00::5", "http://[fd00::5]:9119/api/status"),
        ("[fd00::5]:9300", "http://[fd00::5]:9300/api/status"),
    ])
    def test_status_url(self, address, url):
        assert hermes.status_url(address) == url


class TestVerdict:
    def test_healthy(self):
        assert verdict(HEALTHY) == (
            "up", "Hermes 0.21.3: gateway running, 2 of 2 platforms connected, 1 active session")

    def test_gateway_stopped(self):
        s = status(gateway_running=False, gateway_state="stopped", gateway_platforms={})
        assert verdict(s) == ("down", "the gateway is stopped")

    def test_failed_to_start_with_the_platform_that_did_it(self):
        s = status(gateway_running=False, gateway_state="startup_failed",
                   gateway_exit_reason="telegram: token rejected",
                   gateway_platforms={"telegram": {"state": "fatal",
                                                   "error_message": "Unauthorized (401)"}})
        assert verdict(s) == ("down", "the gateway failed to start: telegram: token rejected; "
                                      "Telegram failed: Unauthorized (401)")

    def test_a_platform_failed_for_good_is_down(self):
        assert verdict(platform("discord", "fatal", "another gateway holds this bot")) == (
            "down", "Discord failed: another gateway holds this bot")

    @pytest.mark.parametrize("state", ["retrying", "disconnected", "connecting", "paused"])
    def test_a_platform_finding_its_feet_is_degraded(self, state):
        assert verdict(platform("slack", state)) == ("degraded", f"Slack is {state}")

    def test_a_disabled_platform_is_not_a_problem(self):
        s = status()
        s["gateway_platforms"]["webhook"] = {"state": "disabled"}
        assert verdict(s)[0] == "up" and "2 of 2 platforms" in verdict(s)[1]

    def test_names_a_profile_and_a_platform_people_know(self):
        s = status()
        s["gateway_platforms"]["alpha:whatsapp"] = {"state": "retrying", "error_message": "QR expired"}
        assert verdict(s) == ("degraded", "WhatsApp (alpha) is retrying: QR expired")

    def test_the_nous_sign_in_ending_is_down(self):
        assert verdict(status(nous_session_valid="terminal")) == (
            "down", "its Nous sign-in has ended: every turn fails until it signs in again")

    def test_storage(self):
        corrupt = status()
        corrupt["components"]["storage"] = {"status": "degraded", "reason": "corrupt"}
        assert verdict(corrupt) == ("down", "its session database is corrupt")
        slow = status()
        slow["components"]["storage"] = {"status": "degraded"}
        assert verdict(slow) == ("degraded", "its storage is unhealthy")

    def test_a_wedged_gateway(self):
        assert verdict(status(gateway_heartbeat_stale_s=340)) == (
            "degraded", "the gateway has not checked in for 5m 40s")

    def test_draining_or_starting(self):
        assert verdict(status(gateway_state="draining")) == ("degraded", "the gateway is draining")

    def test_pressure_alerts_only_at_critical(self):
        s = status()
        s["memory"]["pressure"], s["memory"]["system_available_mb"] = "critical", 180
        s["disk"]["pressure"] = "elevated"
        assert verdict(s) == ("degraded", "memory is critical (180 MB free)")

    def test_degraded_for_a_reason_it_does_not_name(self):
        s = status(overall="degraded")
        s["components"]["dashboard"] = {"status": "ok"}
        assert verdict(s) == ("degraded", "Hermes reports itself degraded")

    def test_the_worst_decides_and_the_rest_follow(self):
        s = platform("discord", "retrying")
        s["nous_session_valid"] = "terminal"
        assert verdict(s) == ("down", "its Nous sign-in has ended: every turn fails until it "
                                      "signs in again; Discord is retrying")

    def test_an_older_hermes_with_less_to_say(self):
        assert verdict({"version": "0.9.0", "gateway_running": True, "gateway_state": "running"}) == (
            "up", "Hermes 0.9.0: gateway running")

    def test_nonsense_in_the_fields_does_not_break_it(self):
        s = {"gateway_running": True, "gateway_platforms": ["telegram"], "components": "ok",
             "memory": None, "disk": 7, "active_sessions": "many", "gateway_heartbeat_stale_s": True}
        assert verdict(s) == ("up", "Hermes: gateway running")
        hermes.summary(s)


class TestOpenStatusDetails:
    """What the open /api/status says beyond the verdict, for the card."""

    NOW = datetime(2026, 10, 20, 13, 0, 27, tzinfo=timezone.utc)

    def detailed(self) -> dict:
        s = status(gateway_updated_at="2026-10-20T13:00:00+00:00", gateway_mode="multiplex",
                   profiles=["default", "work", "lab"], active_agents=1, gateway_busy=True)
        s["gateway_platforms"]["discord"] = {"state": "retrying", "error_message": "1006",
                                             "updated_at": "2026-10-20T12:58:00Z",
                                             "retrying_since": "2026-10-20T12:58:00Z",
                                             "needs_attention": True}
        s["memory"].update(swap_used_mb=1900, last_boot_unclean=True, last_boot_suspected_oom=True)
        s["components"]["dashboard"] = {"status": "ok", "recent_unhandled_errors": 2,
                                        "last_error_at": None, "selftest": "unknown"}
        return s

    def test_kept(self):
        kept = hermes.summary(self.detailed(), now=self.NOW)
        g = kept["gateway"]
        assert (g["heartbeat_age_s"], g["mode"], g["profiles"], g["busy"]) == (
            27, "multiplex", ["default", "work", "lab"], True)
        discord = kept["platforms"][0]
        assert discord["retrying_since"] == "2026-10-20T12:58:00+00:00" and discord["attention"]
        assert kept["memory"]["swap_used_mb"] == 1900 and kept["memory"]["boot_oom"]
        assert kept["dashboard"] == {"status": "ok", "errors": 2}

    def test_no_heartbeat_when_it_is_not_running(self):
        s = status(gateway_running=False, gateway_state="stopped",
                   gateway_updated_at="2026-10-20T12:00:00Z")
        assert hermes.summary(s, now=self.NOW)["gateway"]["heartbeat_age_s"] is None

    def test_odd_times_are_ignored(self):
        s = status(gateway_updated_at=1760000000.5)
        s["gateway_platforms"]["telegram"]["updated_at"] = "yesterday"
        kept = hermes.summary(s, now=self.NOW)
        assert kept["gateway"]["heartbeat_age_s"] is None and kept["platforms"][1]["since"] is None

    def test_the_words(self):
        card = hermes.view(hermes.summary(self.detailed(), now=self.NOW))
        assert card["gateway_words"] == ["heartbeat 27s old", "1 agent working", "3 profiles, multiplex"]
        assert card["platforms"][0]["retrying_at"] == datetime(2026, 10, 20, 12, 58, tzinfo=timezone.utc)

    def test_on_the_card(self, site, server, answer):
        here = device_at("127.0.0.1")
        answer.update(body=json.dumps(self.detailed()).encode())
        check(add(site, server))
        page = flat(site.get(f"/devices/{here}").text)
        assert "1 agent working · 3 profiles, multiplex" in page and "heartbeat " in page
        assert '<span class="pill warn">needs attention</span>' in page
        assert "1006 · retrying since Oct 20, 12:58" in page
        assert "connected since Oct 20, 13:00" in page
        assert "1900 MB of swap in use" in page and "its last start followed an out-of-memory kill" in page
        assert "<td>Dashboard</td>" in page and "2 recent errors" in page


class TestSummary:
    def test_keeps_only_what_the_card_shows(self):
        s = status(hermes_home="/home/me/.hermes", config_path="/home/me/.hermes/config.yaml",
                   gateway_pid=4242, gateways=[{"port": 8642}])
        kept = json.dumps(hermes.summary(s))
        for private in ("/home/me", "4242", "8642", "install_id", "abc"):
            assert private not in kept

    def test_cut_to_size(self):
        s = status()
        s["gateway_platforms"] = {f"p{n:02}": {"state": "retrying", "error_message": "e" * 5000}
                                  for n in range(60)}
        kept = hermes.summary(s)
        assert len(kept["platforms"]) == hermes.MAX_PLATFORMS
        assert len(kept["platforms"][0]["error"]) == hermes.MAX_TEXT
        assert len(hermes.judge(s).detail) <= 500

    def test_the_card(self):
        s = platform("discord", "retrying", "gateway 1006")
        s["config_version"] = 40
        card = hermes.view(hermes.summary(s))
        assert card["gateway_pill"] == "ok" and card["config_behind"]
        assert [(p["label"], p["pill"]) for p in card["platforms"]] == [("Discord", "warn"), ("Telegram", "ok")]
        assert card["warn"] == ["Discord is retrying: gateway 1006"]
        assert hermes.view(None) is None


# --------------------------------------------------------------------------
# A real server
# --------------------------------------------------------------------------


ANSWER: dict = {}


class _Hermes(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:  # type: ignore[no-untyped-def]
        pass

    def do_GET(self) -> None:  # noqa: N802 - http.server's name
        ANSWER["path"] = self.path
        code, body = ANSWER.get("code", 200), ANSWER.get("body", json.dumps(HEALTHY).encode())
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


@pytest.fixture(scope="module")
def server():  # type: ignore[no-untyped-def]
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Hermes)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture
def answer():  # type: ignore[no-untyped-def]
    ANSWER.clear()
    yield ANSWER
    ANSWER.clear()


def probe(address: str):  # type: ignore[no-untyped-def]
    return asyncio.run(run_check("hermes", CheckSpec(address=address, timeout_seconds=5)))


class TestOverHttp:
    def test_reads_it(self, server, answer):
        o = probe(server)
        assert answer["path"] == "/api/status"
        assert o.ok and not o.degraded and o.detail.startswith("Hermes 0.21.3")
        assert o.readings["version"] == "0.21.3" and o.latency_ms is not None

    def test_a_wrong_host_header_says_what_to_use(self, server, answer):
        answer.update(code=400, body=b'{"detail": "Invalid Host header. Dashboard requests must use '
                                     b'the bound hostname or the configured public hostname."}')
        o = probe(server)
        assert not o.ok and "listens on (its --host), or its dashboard.public_url" in o.detail

    @pytest.mark.parametrize("code, body, why", [
        (500, b"{}", "HTTP 500 from /api/status"),
        (200, b"<html>a router</html>", "not a Hermes dashboard: the answer is not JSON"),
        (200, b'{"status": "ok"}', "not a Hermes dashboard: there is no gateway in the answer"),
        (200, b"[" + b"1," * 200_000 + b"1]", "not a Hermes dashboard: the answer is over 256 KB"),
    ])
    def test_not_hermes(self, server, answer, code, body, why):
        answer.update(code=code, body=body)
        o = probe(server)
        assert not o.ok and o.detail == why and o.readings is None

    def test_nothing_there(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        o = probe(f"127.0.0.1:{port}")
        assert not o.ok and "ConnectError" in o.detail


# --------------------------------------------------------------------------
# In SPARK: the target, the alert, the card
# --------------------------------------------------------------------------


@pytest.fixture
def site():  # type: ignore[no-untyped-def]
    config = _config()
    with TestClient(create_app(config), follow_redirects=False) as client:
        client.post("/setup", data={"setup_code": client.app.state.setup_code, "username": "admin",
                                    "password": PASSWORD, "password_confirm": PASSWORD,
                                    "timezone": "UTC"})
        yield client


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def add(client, address: str, name: str = "Hermes",  # type: ignore[no-untyped-def]
        check_type: str = "hermes") -> int:
    page = client.post("/targets/new", data={"name": name, "check_type": check_type,
                                             "address": address, "params": ""})
    assert page.status_code == 303, page.text

    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            return await s.scalar(select(Target.id).where(Target.name == name))
    return run(go())


def the_target(target_id: int) -> Target:
    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            row = await s.get(Target, target_id)
            s.expunge(row)
            return row
    return run(go())


def check(target_id: int) -> None:
    run(runner.run_target(target_id))


def subjects() -> list[tuple[str, str]]:
    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            return [(n.subject, n.body or "") for n in (await s.execute(
                select(Notification).where(Notification.kind.in_(("down", "recovered")))
                .order_by(Notification.id))).scalars()]
    return run(go())


def device_at(ip: str) -> int:
    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            d = Device(mac=f"aa:00:00:00:{ip.split('.')[-2]:0>2}:{ip.split('.')[-1]:0>2}"[-17:],
                       primary_ip=ip, friendly_name="agent-vm", last_seen=utcnow())
            s.add(d)
            await s.flush()
            return d.id
    return run(go())


class TestInSpark:
    def test_offered_on_the_form_with_its_hint(self, site):
        page = site.get("/targets/new").text
        assert 'data-defaults="{&#34;&#34;: 15, &#34;hermes&#34;: 60}"' in page
        assert '<option value="hermes"' in page and 'data-hint="hermes"' in page
        assert "for hermes the Hermes dashboard's address" in flat(page)

    def test_readings_kept_and_kept_through_an_outage(self, site, server, answer):
        tid = add(site, server)
        check(tid)
        t = the_target(tid)
        assert t.status == HealthStatus.UP and t.readings["platforms"][0]["label"] == "Discord"
        answer.update(code=500, body=b"{}")
        check(tid)
        assert the_target(tid).readings == t.readings, "an answer with none keeps the last"

    def test_down_alert_says_why(self, site, server, answer):
        tid = add(site, server)
        answer.update(body=json.dumps(platform("telegram", "fatal", "Unauthorized (401)")).encode())
        for _ in range(the_target(tid).failure_threshold):
            check(tid)
        assert the_target(tid).status == HealthStatus.DOWN
        [(subject, body)] = subjects()
        assert subject == "Hermes is down" and "Telegram failed: Unauthorized (401)" in body
        answer.clear()
        for _ in range(the_target(tid).recovery_threshold):
            check(tid)
        assert the_target(tid).status == HealthStatus.UP and len(subjects()) == 2

    def test_degraded_is_shown_not_sent(self, site, server, answer):
        tid = add(site, server)
        answer.update(body=json.dumps(platform("discord", "retrying")).encode())
        for _ in range(the_target(tid).failure_threshold):
            check(tid)
        assert the_target(tid).status == HealthStatus.DEGRADED and subjects() == []

    def test_the_card_on_its_device(self, site, server, answer):
        here = device_at("127.0.0.1")
        elsewhere = device_at("10.9.9.9")
        tid = add(site, server)
        answer.update(body=json.dumps(platform("discord", "retrying", "gateway 1006")).encode())
        check(tid)
        page = flat(site.get(f"/devices/{here}").text)
        assert f'id="hermes-{tid}"' in page and "Hermes 0.21.3" in page
        assert '<span class="pill warn">degraded</span> Discord is retrying: gateway 1006' in page
        assert "<td>Telegram</td>" in page and "41000 MB free, 31.7% used" in page
        assert f'href="/targets/{tid}/edit"' in page
        assert f'id="hermes-{tid}"' not in site.get(f"/devices/{elsewhere}").text

    def test_the_card_says_when_what_it_shows_is_old(self, site, server, answer):
        here = device_at("127.0.0.1")
        tid = add(site, server)
        check(tid)
        answer.update(code=500, body=b"{}")

        async def earlier():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                t = await s.get(Target, tid)
                t.readings = {**t.readings,
                              "read_at": (utcnow() - timedelta(minutes=5)).isoformat()}
        run(earlier())
        check(tid)
        page = flat(site.get(f"/devices/{here}").text)
        assert "Last check: HTTP 500 from /api/status. Below is what it said last time it answered." in page

    def test_not_read_yet(self, site):
        here = device_at("127.0.0.1")
        tid = add(site, "127.0.0.1:1")
        page = flat(site.get(f"/devices/{here}").text)
        assert f'id="hermes-{tid}"' in page and "Hermes, checked every 1 min: not read yet." in page
        assert "Last check: ConnectError" in page

    def test_once_a_minute_unless_tuned(self, site):
        def tuned() -> bool:
            page = site.get(f"/targets/{tid}/edit").text
            return re.search(r'id="tune" name="tune" value="1"\s*checked', page) is not None

        tid = add(site, "127.0.0.1:1")
        assert the_target(tid).interval_seconds == 60 and not tuned()
        edit = {"name": "Hermes", "check_type": "hermes", "address": "127.0.0.1:1", "params": ""}
        site.post(f"/targets/{tid}/edit", data=edit)
        assert the_target(tid).interval_seconds == 60, "an edit with the box closed keeps it"
        site.post(f"/targets/{tid}/edit", data={**edit, "interval_seconds": "300"})
        assert the_target(tid).interval_seconds == 300 and tuned()
        web = add(site, "http://x.test", name="web", check_type="http")
        assert the_target(web).interval_seconds == 15, "other types keep theirs"

    def test_a_new_address_starts_its_card_again(self, site, server):
        tid = add(site, server)
        check(tid)
        assert the_target(tid).readings
        site.post(f"/targets/{tid}/edit", data={"name": "Hermes", "check_type": "hermes",
                                                 "address": "127.0.0.1:1", "params": ""})
        assert the_target(tid).readings is None


class TestWatchAService:
    """Watch on a discovered service: a Hermes check when a Hermes dashboard
    answers there, a TCP check for anything else."""

    def service(self, port: int, name: str | None) -> int:  # type: ignore[no-untyped-def]
        async def go():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                d = await s.scalar(select(Device).where(Device.primary_ip == "127.0.0.1"))
                if d is None:
                    d = Device(mac="aa:00:00:00:00:61", primary_ip="127.0.0.1",
                               friendly_name="agent-vm", last_seen=utcnow())
                    s.add(d)
                    await s.flush()
                svc = Service(device_id=d.id, port=port, name=name, state="open", last_seen=utcnow())
                s.add(svc)
                await s.flush()
                return svc.id
        return run(go())

    def watched(self, site, service_id: int) -> Target:  # type: ignore[no-untyped-def]
        assert site.post(f"/devices/services/{service_id}/watch").status_code == 303

        async def go():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                row = await s.scalar(select(Target).where(Target.service_id == service_id))
                s.expunge(row)
                return row
        return run(go())

    def test_the_scan_names_9119(self):
        assert port_name(9119) == "hermes dashboard"

    def test_a_hermes_dashboard_gets_a_hermes_check(self, site, server, answer):
        port = int(server.rsplit(":", 1)[1])
        t = self.watched(site, self.service(port, "hermes dashboard"))
        assert (t.check_type, t.name, t.address, t.interval_seconds) == (
            CheckType.HERMES, "agent-vm Hermes", server, 60)
        assert the_target(t.id).readings["version"] == "0.21.3", "checked straight away"

    def test_something_else_on_its_port_stays_tcp(self, site, server, answer):
        answer.update(body=b'{"status": "ok"}')
        port = int(server.rsplit(":", 1)[1])
        t = self.watched(site, self.service(port, "hermes dashboard"))
        assert (t.check_type, t.name) == (CheckType.TCP, "agent-vm hermes dashboard")

    def test_other_services_are_not_asked(self, site, server, answer):
        port = int(server.rsplit(":", 1)[1])
        t = self.watched(site, self.service(port, "grafana"))
        assert t.check_type == CheckType.TCP and t.interval_seconds == 15
        assert "path" not in answer, "nothing was sent to it"

    def test_the_button_says_what_it_will_make(self, site, server, answer):
        port = int(server.rsplit(":", 1)[1])
        self.service(port, "hermes dashboard")
        self.service(5000, "synology dsm")
        page = site.get("/services").text
        assert f"(a Hermes check if Hermes answers, else a TCP check, on port {port})" in page
        assert "(TCP check on port 5000)" in page

    def test_likely(self):
        assert hermes.likely(9119, None) and hermes.likely(8080, "My Hermes")
        assert not hermes.likely(8080, "http-alt")


class TestFoundByTheScan:
    """A dashboard the port scan found is read without anyone asking, and
    shown on its device (hermes_scan.py)."""

    service = TestWatchAService.service
    watched = TestWatchAService.watched

    def readings(self, service_id: int) -> dict | None:
        async def go():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                return (await s.get(Service, service_id)).readings
        return run(go())

    def test_when_it_is_asked(self):
        now = utcnow()
        ago = lambda minutes: (now - timedelta(minutes=minutes)).isoformat()  # noqa: E731
        assert hermes_scan.due(None, now) and hermes_scan.due({"tried_at": "nonsense"}, now)
        assert not hermes_scan.due({"tried_at": ago(4), "read_at": ago(4)}, now)
        assert hermes_scan.due({"tried_at": ago(6), "read_at": ago(6)}, now)
        assert not hermes_scan.due({"tried_at": ago(4)}, now) and hermes_scan.due({"tried_at": ago(6)}, now), \
            "not answering yet: every 5 minutes too"
        other = {"tried_at": ago(10), "not_hermes": True}
        assert not hermes_scan.due(other, now), "something else on the port: every 15 minutes"
        assert hermes_scan.due({**other, "tried_at": ago(16)}, now)
        assert hermes_scan.not_hermes("not a Hermes dashboard: the answer is not JSON")
        assert hermes_scan.not_hermes("HTTP 404 from /api/status")
        assert not hermes_scan.not_hermes("ConnectError: All connection attempts failed")

    def test_read_kept_and_kept_through_a_failure(self, site, server, answer):
        sid = self.service(int(server.rsplit(":", 1)[1]), "hermes dashboard")
        now = utcnow()
        assert run(hermes_scan.read_due(now)) == 1
        first = self.readings(sid)
        assert first["version"] == "0.21.3" and first["read_at"] == first["tried_at"] == now.isoformat()
        assert run(hermes_scan.read_due(now + timedelta(minutes=1))) == 0, "not due yet"
        answer.update(code=500, body=b"{}")
        later = now + timedelta(minutes=6)
        assert run(hermes_scan.read_due(later)) == 1
        kept = self.readings(sid)
        assert kept["version"] == "0.21.3" and kept["read_at"] == now.isoformat()
        assert kept["tried_at"] == later.isoformat() and kept["error"] == "HTTP 500 from /api/status"

    def test_not_hermes_leaves_no_card(self, site, server, answer):
        answer.update(body=b'{"status": "ok"}')
        sid = self.service(int(server.rsplit(":", 1)[1]), "hermes dashboard")
        run(hermes_scan.read_due())
        assert self.readings(sid)["not_hermes"] is True
        assert "hermes-found" not in site.get("/devices/1").text
        answer.update(code=404, body=b"{}")
        run(hermes_scan.read_due(utcnow() + timedelta(minutes=16)))
        assert self.readings(sid)["not_hermes"] is True, "no /api/status: not Hermes either"
        assert "hermes-found" not in site.get("/devices/1").text

    def test_what_it_leaves_alone(self, site, server, answer):
        port = int(server.rsplit(":", 1)[1])
        self.service(port, "grafana")
        assert run(hermes_scan.read_due()) == 0 and "path" not in answer
        watched = self.service(9119, "hermes dashboard")

        async def mark():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                s.add(Target(name="h", check_type=CheckType.HERMES, address="127.0.0.1:9119",
                             service_id=watched))
                (await s.get(Service, watched)).state = "open"
        run(mark())
        assert run(hermes_scan.read_due()) == 0, "a Hermes check reads it already"

        async def close():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                d = await s.scalar(select(Device))
                s.add(Service(device_id=d.id, port=port + 1 if port < 65535 else 1,
                              name="hermes dashboard", state="closed"))
                s.add(Device(mac="aa:00:00:00:00:62", primary_ip="10.9.9.8", ignored=True,
                             last_seen=utcnow()))
        run(close())
        assert run(hermes_scan.read_due()) == 0, "closed ports are not asked"

    def test_a_card_from_the_moment_it_is_found(self, site, server, answer):
        sid = self.service(9119, "hermes dashboard")
        page = flat(site.get("/devices/1").text)
        assert f'id="hermes-found-{sid}"' in page and '<span class="pill neutral">reading</span>' in page
        assert "Found on port 9119 by the port scan: SPARK is asking it for its status." in page
        assert f'action="/devices/services/{sid}/watch"' not in page, "nothing to watch yet"

    def test_a_card_that_says_why_it_cannot_read(self, site):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        sid = self.service(port, "Hermes")
        run(hermes_scan.read_due())
        assert self.readings(sid)["not_hermes"] is False
        page = flat(site.get("/devices/1").text)
        assert f'id="hermes-found-{sid}"' in page and "not answering</span>" in page
        assert "SPARK could not read it: ConnectError" in page
        assert f"<code>http://127.0.0.1:{port}/api/status</code>" in page
        assert "hermes dashboard --host 0.0.0.0" in page and "Shown, not alerted on" not in page

    def test_the_card_without_a_target(self, site, server, answer):
        port = int(server.rsplit(":", 1)[1])
        sid = self.service(port, "hermes dashboard")
        run(hermes_scan.read_due())
        page = flat(site.get("/devices/1").text)
        assert f'id="hermes-found-{sid}"' in page and '<span class="pill dot status-up">up</span>' in page
        assert f"found on port {port} by the port scan, read every 5 min" in page
        assert f'action="/devices/services/{sid}/watch"' in page
        assert "Shown, not alerted on." in page and "<td>Telegram</td>" in page

    def test_the_card_says_when_it_stopped_answering(self, site, server, answer):
        sid = self.service(int(server.rsplit(":", 1)[1]), "hermes dashboard")
        now = utcnow()
        run(hermes_scan.read_due(now - timedelta(minutes=6)))
        answer.update(code=500, body=b"{}")
        run(hermes_scan.read_due(now))
        page = flat(site.get("/devices/1").text)
        assert f'id="hermes-found-{sid}"' in page and "not answering</span>" in page
        assert "Last read: HTTP 500 from /api/status. Below is what it said" in page

    def test_watch_from_the_card_comes_back_to_a_checked_card(self, site, server, answer):
        sid = self.service(int(server.rsplit(":", 1)[1]), "hermes dashboard")
        run(hermes_scan.read_due())
        moved = site.post(f"/devices/services/{sid}/watch", data={"back": "/devices/1"})
        tid = the_target_for(sid)
        assert moved.status_code == 303 and moved.headers["location"] == f"/devices/1#hermes-{tid}"
        page = flat(site.get("/devices/1").text)
        assert f'id="hermes-{tid}"' in page and "hermes-found" not in page, "one card, the check's"
        assert run(hermes_scan.read_due(utcnow() + timedelta(hours=1))) == 0, "and the job lets it be"

    def tcp(self, address: str, *, service_id: int | None, interval: int = 15) -> int:
        async def go():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                d = await s.scalar(select(Device).where(Device.primary_ip == "127.0.0.1"))
                t = Target(name="agent-vm Hermes", check_type=CheckType.TCP, address=address,
                           params={"port": 1}, device_id=d.id if service_id else None,
                           service_id=service_id, interval_seconds=interval,
                           status=HealthStatus.UP)
                s.add(t)
                await s.flush()
                s.add(CheckResult(target_id=t.id, status=HealthStatus.UP, detail="connected"))
                return t.id
        return run(go())

    def results(self, target_id: int) -> list[str]:
        async def go():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                return [r.detail for r in (await s.execute(select(CheckResult).where(
                    CheckResult.target_id == target_id).order_by(CheckResult.id))).scalars()]
        return run(go())

    def test_a_tcp_check_on_the_port_is_offered_and_turned(self, site, server, answer):
        sid = self.service(int(server.rsplit(":", 1)[1]), "hermes dashboard")
        tid = self.tcp(server, service_id=sid)
        run(hermes_scan.read_due())
        page = flat(site.get("/devices/1").text)
        assert f'id="hermes-found-{sid}"' in page, "a TCP check does not hide the card"
        assert ("Watched by a TCP check, " f'<a href="/targets/{tid}/edit">agent-vm Hermes</a>, '
                "which knows only that the port is open.") in page
        assert "Turn its TCP check, agent-vm Hermes, into a Hermes check, keeping its history" in page
        moved = site.post(f"/devices/services/{sid}/watch", data={"back": "/devices/1"})
        assert moved.headers["location"] == f"/devices/1#hermes-{tid}", "the same target"
        t = the_target(tid)
        assert (t.check_type, t.address, t.params, t.interval_seconds) == (CheckType.HERMES, server, {}, 60)
        assert t.readings["version"] == "0.21.3", "checked straight away"
        assert self.results(tid)[0] == "connected" and len(self.results(tid)) == 2, "history kept"
        page = flat(site.get("/devices/1").text)
        assert f'id="hermes-{tid}"' in page and "hermes-found" not in page

    def test_a_chosen_interval_stays(self, site, server, answer):
        sid = self.service(int(server.rsplit(":", 1)[1]), "hermes dashboard")
        tid = self.tcp(server, service_id=sid, interval=300)
        site.post(f"/devices/services/{sid}/watch", data={"back": "/devices/1"})
        assert the_target(tid).interval_seconds == 300

    def test_one_made_by_hand_at_the_address_too(self, site, server, answer):
        sid = self.service(int(server.rsplit(":", 1)[1]), "hermes dashboard")
        tid = self.tcp(server, service_id=None)
        site.post(f"/devices/services/{sid}/watch", data={"back": "/devices/1"})
        t = the_target(tid)
        assert t.check_type == CheckType.HERMES and t.service_id == sid and t.device_id == 1
        assert the_target_for(sid) == tid, "not a second target"

    def test_not_hermes_leaves_the_tcp_check_alone(self, site, server, answer):
        sid = self.service(int(server.rsplit(":", 1)[1]), "hermes dashboard")
        tid = self.tcp(server, service_id=sid)
        answer.update(body=b'{"status": "ok"}')
        moved = site.post(f"/devices/services/{sid}/watch", data={"back": "/devices/1"})
        assert moved.headers["location"] == "/targets"
        assert the_target(tid).check_type == CheckType.TCP and the_target(tid).params == {"port": 1}

    def test_anywhere_else_back_goes_to_targets(self, site, server, answer):
        sid = self.service(int(server.rsplit(":", 1)[1]), "hermes dashboard")
        moved = site.post(f"/devices/services/{sid}/watch", data={"back": "https://elsewhere.test/"})
        assert moved.headers["location"] == "/targets"

    def test_the_job_runs_every_minute(self, site):
        from spark import scheduler as scheduler_module
        job = scheduler_module._scheduler.get_job(hermes_scan.JOB_ID)
        assert job is not None and job.name == "Hermes dashboards"
        assert (job.next_run_time - utcnow()).total_seconds() <= hermes_scan.FIRST_TICK_SECONDS + 1, \
            "first asks soon after SPARK starts"


def the_target_for(service_id: int) -> int:
    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            return await s.scalar(select(Target.id).where(Target.service_id == service_id))
    return run(go())


def test_migration_30_from_a_version_29_database():
    async def go():  # type: ignore[no-untyped-def]
        config = _config()
        D.init_engine(config)
        await D.init_db(config)
        async with D.session_scope() as s:
            await s.execute(text("ALTER TABLE service DROP COLUMN readings"))
            await D._set_version(s, 29)
        await D.close_engine()
        D.init_engine(config)
        await D.init_db(config)
        async with D.session_scope() as s:
            d = Device(mac="aa:00:00:00:00:71", primary_ip="10.0.0.5", last_seen=utcnow())
            s.add(d)
            await s.flush()
            s.add(Service(device_id=d.id, port=9119, readings={"tried_at": "x"}))
        async with D.session_scope() as s:
            row = await s.scalar(select(Service))
            version = await D._get_version(s)
        await D.close_engine()
        return row.readings, version

    readings, version = asyncio.run(go())
    assert readings == {"tried_at": "x"} and version == D.CURRENT_VERSION >= 30


def test_migration_29_from_a_version_28_database():
    async def go():  # type: ignore[no-untyped-def]
        config = _config()
        D.init_engine(config)
        await D.init_db(config)
        async with D.session_scope() as s:
            await s.execute(text("ALTER TABLE target DROP COLUMN readings"))
            await D._set_version(s, 28)
        await D.close_engine()
        D.init_engine(config)
        await D.init_db(config)
        async with D.session_scope() as s:
            s.add(Target(name="h", check_type=CheckType.HERMES, address="10.0.0.5",
                         readings={"read_at": "2026-10-20T13:00:00+00:00"}))
        async with D.session_scope() as s:
            row = await s.scalar(select(Target))
            version = await D._get_version(s)
        await D.close_engine()
        return row.readings, version

    readings, version = asyncio.run(go())
    assert readings == {"read_at": "2026-10-20T13:00:00+00:00"} and version == D.CURRENT_VERSION >= 29
