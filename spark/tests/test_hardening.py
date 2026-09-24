"""The browser-side defences, and the fixes from the security review.

Every test here was written to fail against the code as it stood before the
review: no security headers, an OpenAPI document served to anyone, a login
route that verified passwords in proxy mode, a rate limit keyed on the first
`X-Forwarded-For` entry (the one the client writes), and a target form that
answered a bad check type with a 500.
"""

from __future__ import annotations

import asyncio
import re
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from spark import db as D
from spark.auth import client_ip
from spark.checks.base import CheckSpec
from spark.config import AuthConfig, Config, ProxyAuthConfig
from spark.main import create_app
from spark.models import CheckType, HealthStatus, Target
from spark.web.hardening import same_origin

PASSWORD = "correct horse battery"


def make_config(tmp: Path, **auth) -> Config:  # type: ignore[no-untyped-def]
    config = Config.model_validate(
        {"app": {"data_dir": str(tmp / "data"), "port": 9704, "log_level": "WARNING"},
         "auth": auth, "network": {"subnets": []}}
    )
    config.app.data_dir.mkdir(parents=True, exist_ok=True)
    return config


def fresh_client(**auth) -> TestClient:  # type: ignore[no-untyped-def]
    tmp = Path(tempfile.mkdtemp(prefix="spark-hardening-"))
    config = make_config(tmp, **auth)
    client = TestClient(create_app(config), follow_redirects=False)
    client.__enter__()
    client.config = config  # type: ignore[attr-defined]
    return client


def signed_in_client() -> TestClient:
    client = fresh_client()
    response = client.post("/setup", data={"username": "admin", "password": PASSWORD,
                                           "password_confirm": PASSWORD})
    assert response.status_code == 303
    return client


def in_db(client: TestClient, work):  # type: ignore[no-untyped-def]
    """Run a coroutine against the client's database, the way the other suites do."""
    async def go():
        D.init_engine(client.config)  # type: ignore[attr-defined]
        async with D.session_scope() as session:
            return await work(session)
    return asyncio.run(go())


# --------------------------------------------------------------------------
# Headers
# --------------------------------------------------------------------------


class TestHeaders:
    def test_every_response_carries_the_defensive_headers(self):
        client = fresh_client()
        for path in ("/healthz", "/setup", "/login", "/static/app.css"):
            response = client.get(path)
            assert response.headers["x-content-type-options"] == "nosniff", path
            assert response.headers["x-frame-options"] == "DENY", path
            assert "frame-ancestors 'none'" in response.headers["content-security-policy"], path
            assert response.headers["referrer-policy"] == "same-origin", path

    def test_the_csp_nonce_is_the_one_the_page_uses(self):
        client = signed_in_client()
        response = client.get("/")
        assert response.status_code == 200
        csp = response.headers["content-security-policy"]
        match = re.search(r"'nonce-([^']+)'", csp)
        assert match, csp
        nonce = match.group(1)
        # Every inline script on the page carries it -- otherwise the browser
        # refuses to run it and the live refresh silently stops.
        scripts = re.findall(r"<script([^>]*)>", response.text)
        assert scripts, "the shell has an inline script"
        for attrs in scripts:
            assert f'nonce="{nonce}"' in attrs, attrs
        assert "'unsafe-inline'" not in csp

    def test_the_nonce_changes_per_request(self):
        client = signed_in_client()
        first = client.get("/").headers["content-security-policy"]
        second = client.get("/").headers["content-security-policy"]
        assert first != second

    def test_pages_are_not_cached_but_assets_are(self):
        client = signed_in_client()
        assert client.get("/").headers["cache-control"] == "no-store"
        assert client.get("/static/app.css").headers.get("cache-control") != "no-store"

    def test_no_openapi_document_or_swagger_page(self):
        client = fresh_client()
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/api/docs").status_code == 404
        assert client.get("/docs").status_code == 404


# --------------------------------------------------------------------------
# Cross-site writes
# --------------------------------------------------------------------------


class TestSameOrigin:
    def h(self, **headers: str) -> list[tuple[bytes, bytes]]:
        return [(k.replace("_", "-").encode(), v.encode()) for k, v in headers.items()]

    def test_matching_origin_passes(self):
        assert same_origin(self.h(host="spark.lan:9700", origin="http://spark.lan:9700"))
        assert same_origin(self.h(host="10.1.10.7:9700", origin="HTTP://10.1.10.7:9700"))

    def test_foreign_origin_is_refused(self):
        assert not same_origin(self.h(host="spark.lan:9700", origin="http://evil.example"))
        assert not same_origin(self.h(host="spark.lan:9700", origin="http://spark.lan:9701"))
        assert not same_origin(self.h(host="spark.lan:9700", origin="null"))

    def test_referer_is_the_fallback(self):
        assert same_origin(self.h(host="spark.lan:9700", referer="http://spark.lan:9700/targets"))
        assert not same_origin(self.h(host="spark.lan:9700", referer="http://evil.example/x"))

    def test_origin_wins_over_referer(self):
        assert not same_origin(self.h(host="spark.lan:9700", origin="http://evil.example",
                                      referer="http://spark.lan:9700/"))

    def test_no_header_at_all_is_allowed(self):
        # curl, scripts, and the test client: not a browser carrying a cookie
        # across sites, which is the only thing this check is for.
        assert same_origin(self.h(host="spark.lan:9700"))

    def test_cross_site_post_is_refused_before_auth(self):
        client = signed_in_client()
        response = client.post(
            "/logout", headers={"Origin": "http://evil.example"}
        )
        assert response.status_code == 403
        # And the session survived: the logout never ran.
        assert client.get("/").status_code == 200

    def test_same_site_post_still_works(self):
        client = signed_in_client()
        response = client.post(
            "/logout", headers={"Origin": "http://testserver"}
        )
        assert response.status_code == 303
        assert client.get("/").status_code == 303  # signed out: bounced to login

    def test_reads_are_never_refused(self):
        client = signed_in_client()
        response = client.get("/", headers={"Origin": "http://evil.example"})
        assert response.status_code == 200


# --------------------------------------------------------------------------
# Proxy mode
# --------------------------------------------------------------------------


class _Request:
    """Just enough of a Starlette request for client_ip()."""

    def __init__(self, host: str, **headers: str) -> None:
        self.client = type("C", (), {"host": host})()
        self.headers = {k.replace("_", "-"): v for k, v in headers.items()}


class TestProxyMode:
    proxy = AuthConfig(mode="proxy", proxy=ProxyAuthConfig(trusted_proxies=["10.0.0.5"]))

    def test_forwarded_for_uses_the_entry_the_proxy_added(self):
        # A client that sends its own X-Forwarded-For gets it *prepended* to
        # the proxy's; only the last entry is the proxy's own observation.
        request = _Request("10.0.0.5", x_forwarded_for="1.2.3.4, 203.0.113.9")
        assert client_ip(request, self.proxy) == "203.0.113.9"

    def test_forwarded_for_is_ignored_from_an_untrusted_source(self):
        request = _Request("192.168.1.50", x_forwarded_for="203.0.113.9")
        assert client_ip(request, self.proxy) == "192.168.1.50"

    def test_forwarded_for_is_ignored_in_password_mode(self):
        request = _Request("10.0.0.5", x_forwarded_for="203.0.113.9")
        assert client_ip(request, AuthConfig()) == "10.0.0.5"

    def test_password_login_is_refused_in_proxy_mode(self):
        client = fresh_client(mode="proxy", proxy={"trusted_proxies": ["10.0.0.5"]})
        response = client.post("/login", data={"username": "admin", "password": PASSWORD})
        assert response.status_code == 303
        assert response.headers["location"] == "/login"
        assert "set-cookie" not in response.headers


# --------------------------------------------------------------------------
# Session cookie
# --------------------------------------------------------------------------


class TestSessionCookie:
    def test_secure_follows_the_scheme_of_the_login(self):
        tmp = Path(tempfile.mkdtemp(prefix="spark-hardening-"))
        app = create_app(make_config(tmp))
        with TestClient(app, base_url="https://testserver", follow_redirects=False) as tls:
            response = tls.post("/setup", data={"username": "admin", "password": PASSWORD,
                                                "password_confirm": PASSWORD})
            assert response.status_code == 303
            cookie = response.headers["set-cookie"].lower()
            assert "secure" in cookie
            assert "httponly" in cookie
            assert "samesite=lax" in cookie

    def test_plain_http_login_gets_a_cookie_the_browser_will_send(self):
        client = signed_in_client()
        cookie = client.post("/login", data={"username": "admin", "password": PASSWORD}) \
            .headers["set-cookie"].lower()
        assert "secure" not in cookie


# --------------------------------------------------------------------------
# Target form
# --------------------------------------------------------------------------


def _target_form(**overrides: str) -> dict[str, str]:
    form = {"name": "gateway", "check_type": "ping", "address": "127.0.0.1"}
    form.update(overrides)
    return form


class TestTargetForm:
    def test_unknown_check_type_is_a_400_not_a_500(self):
        client = signed_in_client()
        response = client.post("/targets/new", data=_target_form(check_type="telepathy"))
        assert response.status_code == 400
        assert "not a check type" in response.text

    def test_docker_check_type_is_not_offered(self):
        client = signed_in_client()
        response = client.post("/targets/new", data=_target_form(check_type="docker"))
        assert response.status_code == 400

    def test_dependency_must_exist(self):
        client = signed_in_client()
        response = client.post("/targets/new", data=_target_form(depends_on_target_id="999"))
        assert response.status_code == 400
        assert "no longer exists" in response.text

    def test_dependency_must_be_a_number(self):
        client = signed_in_client()
        response = client.post("/targets/new", data=_target_form(depends_on_target_id="x"))
        assert response.status_code == 400

    def test_a_target_cannot_depend_on_itself(self):
        client = signed_in_client()
        assert client.post("/targets/new", data=_target_form()).status_code == 303
        page = client.get("/targets").text
        target_id = int(re.search(r"/targets/(\d+)/edit", page).group(1))
        response = client.post(
            f"/targets/{target_id}/edit",
            data=_target_form(depends_on_target_id=str(target_id)),
        )
        assert response.status_code == 400
        assert "depend on itself" in response.text

    def test_tuning_values_are_clamped(self):
        client = signed_in_client()
        response = client.post("/targets/new", data=_target_form(
            interval_seconds="1", timeout_seconds="9999", failure_threshold="0",
            recovery_threshold="100000",
        ))
        assert response.status_code == 303

        async def read(session):  # type: ignore[no-untyped-def]
            target = (await session.execute(select(Target))).scalars().one()
            session.expunge(target)
            return target

        target = in_db(client, read)
        assert target.interval_seconds == 5
        assert target.timeout_seconds == 60.0
        assert target.failure_threshold == 1
        assert target.recovery_threshold == 100


class TestPingParams:
    def test_count_is_bounded(self):
        # Pure: the bounded values are what reach icmplib. Patch it and read
        # the call back rather than pinging anything.
        from spark.checks import net

        seen: dict = {}

        async def fake_ping(address, **kwargs):  # type: ignore[no-untyped-def]
            seen.update(kwargs)
            raise OSError("no network in tests")

        import icmplib

        original = icmplib.async_ping
        icmplib.async_ping = fake_ping  # type: ignore[assignment]
        try:
            asyncio.run(net.check_ping(
                CheckSpec(address="127.0.0.1", params={"count": 500, "interval": 0.001})
            ))
        finally:
            icmplib.async_ping = original  # type: ignore[assignment]
        assert seen["count"] == 20
        assert seen["interval"] == 0.05


# --------------------------------------------------------------------------
# The check-now button no longer holds the write lock across the check
# --------------------------------------------------------------------------


class TestCheckNow:
    def test_check_now_records_a_result(self):
        client = signed_in_client()
        assert client.post("/targets/new", data=_target_form(
            check_type="tcp", address="127.0.0.1:1")).status_code == 303
        page = client.get("/targets").text
        target_id = int(re.search(r"/targets/(\d+)/edit", page).group(1))

        # Make the session's last-seen stale, so resolving the cookie on the
        # next request performs a write inside the request's transaction --
        # the condition under which "Check now" used to collide with itself.
        from datetime import timedelta

        from spark.models import UserSession, utcnow

        async def age_session(session):  # type: ignore[no-untyped-def]
            for row in (await session.execute(select(UserSession))).scalars():
                row.last_seen_at = utcnow() - timedelta(hours=1)

        in_db(client, age_session)

        response = client.post(f"/targets/{target_id}/check")
        assert response.status_code == 303

        async def read(session):  # type: ignore[no-untyped-def]
            target = await session.get(Target, target_id)
            session.expunge(target)
            return target

        target = in_db(client, read)
        assert target.last_checked_at is not None
        assert target.status in (HealthStatus.UNKNOWN, HealthStatus.DOWN, HealthStatus.UP)
        assert target.check_type is CheckType.TCP
