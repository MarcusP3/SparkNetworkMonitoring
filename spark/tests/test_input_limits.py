"""Hostile and malformed input: refused plainly, never a 500, never run.

The injection side -- parameterised queries, escaped output, no shell -- was
already true; the tests for it here are regressions. The rest is what an
audit found missing: ids too large for SQLite, a 1 MB name accepted and shown
on every page, "nan" as a timeout, and FastAPI's JSON dump where a person
should see a page.
"""

from __future__ import annotations

import asyncio
import re
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from spark import db as D
from spark import limits
from spark.config import Config
from spark.main import create_app
from spark.models import Device, SnmpDevice, Target, utcnow

PASSWORD = "correct horse battery"
BIG = "99999999999999999999"           # > 2**63 - 1
XSS = "<script>alert(1)</script>"


def _config() -> Config:
    tmp = Path(tempfile.mkdtemp(prefix="spark-limits-"))
    config = Config.model_validate({
        "app": {"data_dir": str(tmp / "data"), "log_level": "WARNING"},
        "network": {"subnets": []},
    })
    config.app.data_dir.mkdir(parents=True, exist_ok=True)
    return config


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


async def _count(model) -> int:  # type: ignore[no-untyped-def]
    async with D.session_scope() as s:
        return await s.scalar(select(func.count()).select_from(model)) or 0


@pytest.fixture
def client():
    config = _config()

    async def seed():
        D.init_engine(config)
        await D.init_db(config)
        async with D.session_scope() as s:
            s.add(Device(mac="aa:bb:cc:00:00:01", primary_ip="10.0.0.5",
                         friendly_name="dev", last_seen=utcnow()))
        await D.close_engine()
    run(seed())
    with TestClient(create_app(config), follow_redirects=False,
                    raise_server_exceptions=False) as c:
        c.post("/setup", data={"username": "admin", "password": PASSWORD,
                               "password_confirm": PASSWORD, "timezone": "UTC"})
        yield c


def target_form(**over: str) -> dict:
    return {"name": "gw", "check_type": "tcp", "address": "127.0.0.1:9",
            "interval_seconds": "60", "timeout_seconds": "2", "failure_threshold": "3",
            "recovery_threshold": "2", "depends_on_target_id": "", "params": "", **over}


class TestImpossibleIds:
    @pytest.mark.parametrize("path", [f"/devices/{BIG}", f"/targets/{BIG}/edit", "/devices/0",
                                      "/devices/-1"])
    def test_a_page_for_an_id_that_cannot_exist_is_not_found(self, client, path):
        response = client.get(path)
        assert response.status_code == 404
        assert "text/html" in response.headers["content-type"]
        assert "There is nothing at that address." in response.text

    @pytest.mark.parametrize("path", [
        f"/devices/{BIG}/name", f"/devices/{BIG}/ignore", f"/devices/{BIG}/watch",
        f"/devices/{BIG}/snmp", f"/devices/services/{BIG}/watch", f"/targets/{BIG}/delete",
        f"/targets/{BIG}/check", f"/targets/{BIG}/toggle", f"/settings/subnets/{BIG}/delete",
        f"/settings/snmp/profiles/{BIG}/delete", f"/settings/snmp/devices/{BIG}/test",
        f"/settings/snmp/devices/{BIG}/polling",
    ])
    def test_a_write_to_one_is_not_found_either(self, client, path):
        assert client.post(path, data={"profile_id": "1", "friendly_name": "x"}).status_code == 404

    def test_ids_inside_forms_are_checked_too(self, client):
        response = client.post("/settings/snmp/devices", data={"device_id": BIG, "profile_id": BIG})
        assert response.status_code == 400 and "Choose a device and a profile." in response.text
        response = client.post("/targets/new", data=target_form(depends_on_target_id=BIG))
        assert response.status_code == 400 and "Choose a target from the list" in response.text
        assert client.get(f"/devices/1?port={BIG}").status_code == 200

    def test_as_id(self):
        assert limits.as_id("42") == 42 and limits.as_id(" 7 ") == 7
        assert limits.as_id(str(limits.MAX_ID)) == limits.MAX_ID
        for bad in (BIG, "0", "-3", "abc", "", None, "1.5"):
            assert limits.as_id(bad) is None, bad


class TestSize:
    def test_a_field_over_its_limit_is_refused_and_nothing_changes(self, client):
        response = client.post("/devices/1/name", data={"friendly_name": "A" * 65})
        assert response.status_code == 400
        assert "friendly name: longer than 64 characters" in response.text
        assert "Nothing was saved" in response.text

        async def name():
            async with D.session_scope() as s:
                return (await s.get(Device, 1)).friendly_name
        assert run(name()) == "dev"

    def test_at_the_limit_is_fine(self, client):
        client.post("/devices/1/name", data={"friendly_name": "A" * 64})
        assert "A" * 64 in client.get("/devices").text

    def test_an_oversized_request_is_refused_before_any_route(self, client):
        response = client.post("/devices/1/name", data={"friendly_name": "A" * (limits.BODY + 1)})
        assert response.status_code == 413

    def test_a_declared_size_is_refused_without_reading_a_byte(self):
        """By Content-Length alone: the app never runs, the body is never read."""
        from spark.web.hardening import Hardening

        called, sent = [], []

        async def app(scope, receive, send):  # type: ignore[no-untyped-def]
            called.append(True)

        async def receive():  # type: ignore[no-untyped-def]
            raise AssertionError("the body was read")

        async def send(message):  # type: ignore[no-untyped-def]
            sent.append(message)

        for declared in (str(limits.BODY + 1), "not-a-number"):
            called.clear(); sent.clear()
            scope = {"type": "http", "method": "POST", "path": "/devices/1/name",
                     "headers": [(b"content-length", declared.encode())]}
            run(Hardening(app)(scope, receive, send))
            assert called == [] and sent[0]["status"] == 413, declared

    def test_so_is_one_that_does_not_say_how_big_it_is(self, client):
        def chunks():
            yield b"friendly_name="
            for _ in range(limits.BODY // 1000 + 2):
                yield b"A" * 1000
        response = client.post("/devices/1/name", content=chunks(),
                               headers={"content-type": "application/x-www-form-urlencoded"})
        assert response.status_code == 413

    def test_every_form_field_has_a_server_side_cap(self):
        """So a field added later cannot quietly accept a megabyte."""
        from annotated_types import MaxLen

        from spark.web import (routes_auth, routes_devices, routes_preferences,
                               routes_settings, routes_targets)
        uncapped = []
        for module in (routes_auth, routes_devices, routes_preferences, routes_settings,
                       routes_targets):
            for route in module.router.routes:
                for param in route.dependant.body_params:
                    if "str" not in str(param.field_info.annotation):
                        continue
                    if not any(isinstance(m, MaxLen) for m in param.field_info.metadata):
                        uncapped.append(f"{route.path} {param.alias}")
        assert uncapped == []

    @pytest.mark.parametrize("page, field, limit", [
        ("/devices", "friendly_name", limits.NAME),
        ("/targets/new", "name", limits.NAME),
        ("/targets/new", "address", limits.ADDRESS),
        ("/targets/new", "params", limits.PARAMS),
        ("/settings", "name", limits.NAME),
        ("/settings", "cidr", limits.NAME),
        ("/settings/alerts", "webhook", limits.WEBHOOK),
    ])
    def test_the_page_stops_you_at_the_same_limit(self, client, page, field, limit):
        html = client.get(page).text
        tags = re.findall(rf'<(?:input|textarea)[^>]*name="{field}"[^>]*>', html)
        assert tags, f"no {field} on {page}"
        assert all(f'maxlength="{limit}"' in tag for tag in tags), tags


class TestValues:
    @pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
    def test_a_timeout_that_is_not_a_number_is_refused(self, client, value):
        response = client.post("/targets/new", data=target_form(timeout_seconds=value))
        assert response.status_code == 400 and "timeout seconds" in response.text
        assert run(_count(Target)) == 0

    def test_a_non_number_gets_a_page_not_json(self, client):
        response = client.post("/targets/new", data=target_form(interval_seconds="abc"),
                               headers={"referer": "http://testserver/targets/new"})
        assert response.status_code == 400
        assert response.headers["content-type"].startswith("text/html")
        assert "interval seconds: not a value SPARK can use" in response.text
        assert '<a href="/targets/new">Go back</a>' in response.text
        assert '"detail"' not in response.text

    def test_go_back_never_leaves_the_site(self, client):
        # A foreign Referer is refused outright by the same-origin check; a
        # same-host one whose path would read as //other-host must not be
        # turned into a link off the site.
        response = client.post("/targets/new", data=target_form(interval_seconds="abc"),
                               headers={"referer": "http://testserver//evil.example/phish"})
        assert response.status_code == 400
        assert '<a href="/">Go back</a>' in response.text

        from starlette.requests import Request

        from spark.main import _back_to

        def back(referer, host="spark.lan"):  # type: ignore[no-untyped-def]
            return _back_to(Request({"type": "http", "headers": [
                (b"referer", referer.encode()), (b"host", host.encode())]}))
        assert back("https://evil.example/x") == "/"
        assert back("http://spark.lan/devices?snmp=on") == "/devices?snmp=on"
        assert back("") == "/"

    @pytest.mark.parametrize("field", ["name", "address"])
    def test_a_name_or_address_of_only_spaces_is_refused(self, client, field):
        response = client.post("/targets/new", data=target_form(**{field: "   "}))
        assert response.status_code == 400
        assert run(_count(Target)) == 0

    def test_setup_refuses_a_blank_username(self):
        config = _config()
        with TestClient(create_app(config), follow_redirects=False) as c:
            response = c.post("/setup", data={"username": "   ", "password": PASSWORD,
                                              "password_confirm": PASSWORD, "timezone": "UTC"})
            assert response.status_code == 400 and "Choose a username." in response.text


class TestInjectionStaysInert:
    """Regressions for what the audit found already safe."""

    def test_markup_in_a_name_is_shown_not_run(self, client):
        client.post("/devices/1/name", data={"friendly_name": XSS})
        for page in ("/devices", "/devices/1"):
            html = client.get(page).text
            assert XSS not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html, page

    def test_sql_in_a_name_is_just_a_name(self, client):
        client.post("/targets/new", data=target_form(name="x'; DROP TABLE target; --"))
        assert run(_count(Target)) == 1
        assert "x&#39;; DROP TABLE target; --" in client.get("/targets").text

    def test_template_syntax_is_not_evaluated(self, client):
        client.post("/devices/1/name", data={"friendly_name": "{{7*7}}"})
        html = client.get("/devices").text
        assert 'value="{{7*7}}"' in html and 'value="49"' not in html

    def test_snmp_rows_untouched_by_a_bad_add(self, client):
        client.post("/settings/snmp/profiles/defaults")
        client.post("/settings/snmp/devices", data={"device_id": "1 OR 1=1", "profile_id": "1"})
        assert run(_count(SnmpDevice)) == 0
