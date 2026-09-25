"""The sign-in timeout: a session unused for 30 minutes (or as set) ends.

The server is the authority; the page's timer only asks it. So these check the
server: that an idle session is refused everywhere a session is accepted,
that the page's own background requests never count as use, that the
setting takes effect and cannot be set to something outside its list, and
that raising it does not bring a timed-out session back.
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from spark import db as D
from spark import prefs
from spark.auth import SESSION_COOKIE
from spark.config import Config
from spark.main import create_app
from spark.models import UserSession, utcnow

PASSWORD = "correct horse battery"


def _config(**auth) -> Config:  # type: ignore[no-untyped-def]
    tmp = Path(tempfile.mkdtemp(prefix="spark-idle-"))
    config = Config.model_validate({
        "app": {"data_dir": str(tmp / "data"), "log_level": "WARNING"},
        "network": {"subnets": []},
        **({"auth": auth} if auth else {}),
    })
    config.app.data_dir.mkdir(parents=True, exist_ok=True)
    return config


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


async def _age(minutes: float) -> None:
    """Pretend every session was last used `minutes` ago."""
    async with D.session_scope() as s:
        await s.execute(update(UserSession).values(
            last_seen_at=utcnow() - timedelta(minutes=minutes)))


async def _last_seen_ages() -> list[float]:
    async with D.session_scope() as s:
        seen = (await s.scalars(select(UserSession.last_seen_at))).all()
    return [(utcnow() - t).total_seconds() for t in seen]


async def _session_count() -> int:
    async with D.session_scope() as s:
        return await s.scalar(select(func.count()).select_from(UserSession)) or 0


@pytest.fixture
def client():
    config = _config()
    with TestClient(create_app(config), follow_redirects=False) as c:
        c.post("/setup", data={"username": "admin", "password": PASSWORD,
                               "password_confirm": PASSWORD, "timezone": "UTC"})
        yield c


FETCH = {"X-Requested-With": "fetch"}


class TestTheTimeout:
    def test_thirty_minutes_unless_changed(self, client):
        page = client.get("/preferences").text
        assert "<h2>Sign-in timeout</h2>" in page
        assert '<option value="30" selected>30 minutes without activity</option>' in page

    def test_under_the_limit_you_stay_signed_in(self, client):
        run(_age(29))
        assert client.get("/").status_code == 200

    def test_over_the_limit_you_are_sent_to_sign_in(self, client):
        run(_age(31))
        response = client.get("/targets")
        assert response.status_code == 303
        assert response.headers["location"] == "/login?next=/targets&expired=1"

    def test_the_sign_in_page_says_why(self, client):
        run(_age(31))
        page = client.get("/login?expired=1").text
        assert "signed out after 30 minutes without activity" in page
        assert 'class="alert info"' in page

    def test_the_event_stream_refuses_an_idle_session(self, client):
        """Asked of the stream's own check, not over HTTP: a stream that is
        wrongly accepted never ends, and the test would hang instead of fail."""
        from starlette.requests import Request

        from spark.web.routes_events import _authenticate

        token = client.cookies.get(SESSION_COOKIE)
        request = Request({
            "type": "http", "method": "GET", "path": "/events", "app": client.app,
            "headers": [(b"cookie", f"{SESSION_COOKIE}={token}".encode())],
        })
        run(_age(29))
        assert run(_authenticate(request)) is True
        run(_age(31))
        assert run(_authenticate(request)) is False
        assert client.get("/events").status_code == 401

    def test_the_page_carries_the_timeout_for_its_timer(self, client):
        assert '<body data-idle="1800">' in client.get("/").text
        client.post("/logout")
        page = client.get("/login").text
        assert "<body>" in page and "/session" not in page, "no timer when signed out"


class TestWhatCountsAsUse:
    def test_opening_a_page_counts(self, client):
        run(_age(10))
        client.get("/targets")
        assert max(run(_last_seen_ages())) < 5

    def test_the_page_refreshing_itself_does_not(self, client):
        """Else a dashboard left open would never time out."""
        run(_age(10))
        client.get("/targets", headers=FETCH)
        assert min(run(_last_seen_ages())) > 590

    def test_asking_how_long_is_left_does_not(self, client):
        run(_age(10))
        response = client.get("/session", headers=FETCH)
        assert response.status_code == 200
        assert 1190 <= response.json()["remaining"] <= 1200
        assert min(run(_last_seen_ages())) > 590

    def test_typing_or_clicking_counts(self, client):
        """POST /session is what the page sends on a keypress or click."""
        run(_age(10))
        response = client.post("/session", headers=FETCH)
        assert response.status_code == 200 and response.json()["remaining"] > 1700
        assert max(run(_last_seen_ages())) < 5

    def test_nothing_revives_a_timed_out_session(self, client):
        run(_age(31))
        assert client.post("/session", headers=FETCH).status_code == 401
        assert client.get("/session").json() == {"remaining": 0}
        assert client.get("/").status_code == 303


class TestChangingIt:
    def test_a_longer_timeout_keeps_you_signed_in_longer(self, client):
        response = client.post("/preferences", data={"idle_minutes": "60"})
        assert response.headers["location"] == "/preferences?saved=session"
        assert "signed out after 1 hour without activity" in \
            client.get("/preferences?saved=session").text
        run(_age(45))
        assert client.get("/").status_code == 200

    def test_a_shorter_one_applies_at_once(self, client):
        client.post("/preferences", data={"idle_minutes": "15"})
        run(_age(16))
        assert client.get("/").status_code == 303

    @pytest.mark.parametrize("value", ["0", "7", "999999", "abc", ""])
    def test_only_the_listed_choices_are_accepted(self, client, value):
        response = client.post("/preferences", data={"idle_minutes": value})
        assert response.status_code == 400
        if value:
            assert "not one of the timeout choices" in response.text

        async def saved():
            async with D.session_scope() as s:
                return await prefs.get_idle_minutes(s)
        assert run(saved()) == 30

    def test_raising_it_does_not_revive_sessions_that_had_timed_out(self, client):
        """A second session, 40 minutes idle: dead under 30. Raising the
        timeout to 60 must not make it good again."""
        old = client.cookies.get(SESSION_COOKIE)
        client.cookies.clear()
        client.post("/login", data={"username": "admin", "password": PASSWORD})
        assert run(_session_count()) == 2
        run(_age(40))
        client.post("/session", headers=FETCH)      # dead too: this one is idle
        assert client.get("/").status_code == 303

        # Sign in afresh; that session saves the longer timeout.
        client.cookies.clear()
        client.post("/login", data={"username": "admin", "password": PASSWORD})
        client.post("/preferences", data={"idle_minutes": "60"})
        assert run(_session_count()) == 1, "the two idle sessions were deleted"
        assert client.get("/").status_code == 200

        client.cookies.clear()
        client.cookies.set(SESSION_COOKIE, old)
        assert client.get("/").status_code == 303

    def test_the_timezone_form_leaves_the_timeout_alone(self, client):
        client.post("/preferences", data={"idle_minutes": "120"})
        client.post("/preferences", data={"timezone": "Europe/London"})
        assert '<option value="120" selected>' in client.get("/preferences").text


def test_labels():
    assert [prefs.idle_label(m) for m in prefs.IDLE_CHOICES] == [
        "15 minutes", "30 minutes", "1 hour", "2 hours", "4 hours", "8 hours", "24 hours"]


def test_proxy_mode_has_no_timeout_of_its_own():
    config = _config(mode="proxy", proxy={"trusted_proxies": ["10.0.0.1"]})
    with TestClient(create_app(config), follow_redirects=False) as c:
        assert c.get("/session").status_code == 404
