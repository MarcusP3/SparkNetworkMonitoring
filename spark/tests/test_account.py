"""The Account card: change password, see sessions, sign out everywhere else,
and the `spark-reset-password` command (review finding #24).

Before this there was no way to change the password or end another session
short of editing the database -- so a leaked or claimed password had no
in-app remedy.
"""

from __future__ import annotations

import io
import sqlite3
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from spark.config import Config
from spark.main import create_app

PASSWORD = "correct horse battery"
NEW = "a different long phrase"


def _config(**auth) -> Config:  # type: ignore[no-untyped-def]
    tmp = Path(tempfile.mkdtemp(prefix="spark-account-"))
    config = Config.model_validate(
        {"app": {"data_dir": str(tmp / "data"), "log_level": "WARNING"},
         "auth": auth, "network": {"subnets": []}}
    )
    config.app.data_dir.mkdir(parents=True, exist_ok=True)
    return config


def _signed_in(config: Config) -> TestClient:
    client = TestClient(create_app(config), follow_redirects=False)
    client.__enter__()
    response = client.post("/setup", data={"setup_code": client.app.state.setup_code,
                                           "username": "admin", "password": PASSWORD,
                                           "password_confirm": PASSWORD})
    assert response.status_code == 303
    return client


def _bare(client: TestClient) -> TestClient:
    """A second client on the same running app, with no cookies of its own:
    another browser. Not entered, so it does not start the app again."""
    return TestClient(client.app, follow_redirects=False)


def _second_browser(client: TestClient, password: str = PASSWORD) -> str:
    """Another sign-in for the same account; returns its cookie value."""
    response = _bare(client).post("/login", data={"username": "admin", "password": password})
    assert response.status_code == 303
    return response.cookies["spark_session"]


def _as(client: TestClient, cookie: str, path: str = "/") -> int:
    """Status another browser gets for `path` with this cookie."""
    return _bare(client).get(path, cookies={"spark_session": cookie}).status_code


def _login(client: TestClient, password: str) -> int:
    return _bare(client).post("/login", data={"username": "admin", "password": password}).status_code


def _live_sessions(config: Config) -> int:
    with sqlite3.connect(config.app.db_path) as con:
        return con.execute("select count(*) from user_session where revoked = 0").fetchone()[0]


class TestChangePassword:
    def test_the_card_is_on_the_page(self):
        config = _config()
        c = _signed_in(config)
        page = c.get("/preferences").text
        assert 'action="/preferences/password"' in page
        assert 'name="current_password"' in page and 'name="new_password"' in page
        assert "this browser" in page          # the current session is marked
        assert "spark-reset-password" in page  # the way back in is stated

    def test_wrong_current_password_changes_nothing(self):
        config = _config()
        c = _signed_in(config)
        old_cookie = c.cookies["spark_session"]
        response = c.post("/preferences/password", data={
            "current_password": "not it", "new_password": NEW, "new_password_confirm": NEW})
        assert response.status_code == 400 and "Current password is incorrect" in response.text
        assert c.cookies["spark_session"] == old_cookie
        assert _login(c, PASSWORD) == 303

    def test_mismatched_new_passwords_change_nothing(self):
        config = _config()
        c = _signed_in(config)
        response = c.post("/preferences/password", data={
            "current_password": PASSWORD, "new_password": NEW, "new_password_confirm": NEW + "x"})
        assert response.status_code == 400 and "do not match" in response.text

    def test_a_short_new_password_is_refused(self):
        config = _config()
        c = _signed_in(config)
        response = c.post("/preferences/password", data={
            "current_password": PASSWORD, "new_password": "short", "new_password_confirm": "short"})
        assert response.status_code == 400 and "at least 12" in response.text

    def test_changing_it_ends_every_other_session_and_keeps_this_browser_going(self):
        config = _config()
        c = _signed_in(config)
        other = _second_browser(c)
        old_cookie = c.cookies["spark_session"]
        assert _live_sessions(config) == 2

        response = c.post("/preferences/password", data={
            "current_password": PASSWORD, "new_password": NEW, "new_password_confirm": NEW})
        assert response.status_code == 303
        assert response.headers["location"].startswith("/preferences?saved=password")
        assert "spark_session" in response.headers.get("set-cookie", "")
        assert c.cookies["spark_session"] != old_cookie

        # This browser carries on; the other one, and the old cookie, are out.
        assert c.get("/preferences").status_code == 200
        assert "Password changed" in c.get("/preferences?saved=password").text
        assert _as(c, other) == 303
        assert _as(c, old_cookie) == 303
        assert _live_sessions(config) == 1

        # The old password is gone, the new one works.
        assert _login(c, PASSWORD) == 401
        assert _login(c, NEW) == 303

    def test_proxy_mode_has_no_password_to_change(self):
        from spark.auth import create_admin

        config = _config(mode="proxy", proxy={"trusted_proxies": ["10.0.0.5"]})
        # The test client's peer is not an address at all, so nothing is
        # trusted -- fine: there must be no password form either way.
        app = create_app(config)
        with TestClient(app, follow_redirects=False) as c:
            import asyncio

            from spark import db as D

            async def seed():
                D.init_engine(config)
                async with D.session_scope() as session:
                    await create_admin(session, "admin", PASSWORD)
            asyncio.run(seed())
            response = c.post("/preferences/password", data={
                "current_password": PASSWORD, "new_password": NEW, "new_password_confirm": NEW},
                headers={"Remote-User": "admin"})
            assert response.status_code == 303  # to /login: not trusted, not a 500


class TestSessions:
    def test_other_sessions_are_listed_and_can_be_ended_together(self):
        config = _config()
        c = _signed_in(config)
        other_a = _second_browser(c)
        other_b = _second_browser(c)
        page = c.get("/preferences").text
        assert page.count("<tr>") >= 4  # header + three sessions
        assert "Sign out everywhere else (2)" in page

        response = c.post("/preferences/sessions/end-others")
        assert response.status_code == 303
        assert response.headers["location"].startswith("/preferences?saved=sessions&ended=2")
        assert "Signed out 2 other sessions" in c.get("/preferences?saved=sessions&ended=2").text
        assert c.get("/").status_code == 200
        for cookie in (other_a, other_b):
            assert _as(c, cookie) == 303
        assert _live_sessions(config) == 1

    def test_the_button_is_disabled_with_nothing_else_to_end(self):
        config = _config()
        c = _signed_in(config)
        assert "disabled" in c.get("/preferences").text.split('action="/preferences/sessions/end-others"')[1][:300]
        response = c.post("/preferences/sessions/end-others")
        assert response.headers["location"].endswith("ended=0#account")


class TestResetCommand:
    def test_it_sets_the_password_and_signs_everyone_out(self, monkeypatch, capsys):
        import getpass

        from spark.cli import reset_password_main

        config = _config()
        c = _signed_in(config)
        _second_browser(c)
        assert _live_sessions(config) == 2

        monkeypatch.delenv("SPARK_CONFIG", raising=False)
        monkeypatch.setenv("SPARK__APP__DATA_DIR", str(config.app.data_dir))
        answers = iter([NEW, NEW])
        monkeypatch.setattr(getpass, "getpass", lambda _prompt="": next(answers))
        assert reset_password_main([]) == 0
        assert "Password changed for 'admin'" in capsys.readouterr().out

        assert _live_sessions(config) == 0
        assert c.get("/").status_code == 303
        assert _login(c, PASSWORD) == 401
        assert _login(c, NEW) == 303

    def test_mismatch_and_short_passwords_change_nothing(self, monkeypatch, capsys):
        import getpass

        from spark.cli import reset_password_main

        config = _config()
        c = _signed_in(config)
        monkeypatch.delenv("SPARK_CONFIG", raising=False)
        monkeypatch.setenv("SPARK__APP__DATA_DIR", str(config.app.data_dir))

        answers = iter([NEW, NEW + "x"])
        monkeypatch.setattr(getpass, "getpass", lambda _prompt="": next(answers))
        assert reset_password_main([]) == 1
        assert "do not match" in capsys.readouterr().err

        monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
        assert reset_password_main(["--password-stdin"]) == 1
        assert "at least 12" in capsys.readouterr().err

        assert c.get("/").status_code == 200  # still signed in: nothing changed
        assert _login(c, PASSWORD) == 303

    def test_no_account_yet_points_at_setup(self, monkeypatch, capsys):
        from spark.cli import reset_password_main

        config = _config()
        with TestClient(create_app(config)):
            pass
        monkeypatch.delenv("SPARK_CONFIG", raising=False)
        monkeypatch.setenv("SPARK__APP__DATA_DIR", str(config.app.data_dir))
        monkeypatch.setattr("sys.stdin", io.StringIO(NEW + "\n"))
        assert reset_password_main(["--password-stdin"]) == 1
        assert "setup code" in capsys.readouterr().err
