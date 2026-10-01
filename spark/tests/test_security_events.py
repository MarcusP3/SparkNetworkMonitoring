"""Sign-in events go to Discord (defence-in-depth from the 2026-09-30 review):
the lockout tripping, a sign-in from a new address, a password change or
reset, and first-run setup. Off under Settings -> Alerts.

Sending itself is the dispatcher's job and is tested in test_alerts.py; here
the question is what gets queued, and when nothing does.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from spark.config import Config
from spark.main import create_app

PASSWORD = "correct horse battery"


def _config(**auth) -> Config:  # type: ignore[no-untyped-def]
    tmp = Path(tempfile.mkdtemp(prefix="spark-secevents-"))
    config = Config.model_validate(
        {"app": {"data_dir": str(tmp / "data"), "log_level": "WARNING"},
         "auth": auth, "network": {"subnets": []}}
    )
    config.app.data_dir.mkdir(parents=True, exist_ok=True)
    return config


def _client(app, address: str) -> TestClient:  # type: ignore[no-untyped-def]
    """A browser at `address`: the test client lets the peer be named."""
    return TestClient(app, follow_redirects=False, client=(address, 40000))


def _queued(config: Config) -> list[tuple[str, str]]:
    with sqlite3.connect(config.app.db_path) as con:
        return con.execute("select kind, subject from notification order by id").fetchall()


def _setup(app, address: str = "192.168.1.20") -> TestClient:  # type: ignore[no-untyped-def]
    c = _client(app, address)
    response = c.post("/setup", data={"setup_code": app.state.setup_code, "username": "admin",
                                      "password": PASSWORD, "password_confirm": PASSWORD})
    assert response.status_code == 303
    return c


def _login(app, address: str, password: str = PASSWORD) -> TestClient:  # type: ignore[no-untyped-def]
    c = _client(app, address)
    response = c.post("/login", data={"username": "admin", "password": password})
    assert response.status_code == 303, response.status_code
    return c


class TestWhatIsQueued:
    def test_setup_is_an_event_naming_the_address(self):
        config = _config()
        app = create_app(config)
        with TestClient(app):
            _setup(app, "192.168.1.20")
        kinds = _queued(config)
        assert kinds == [("security_setup", "SPARK set up: administrator 'admin' created from 192.168.1.20")]

    def test_lockout_is_one_event_per_window_however_many_attempts(self):
        config = _config(max_attempts=3)
        app = create_app(config)
        with TestClient(app):
            _setup(app)
            guesser = _client(app, "203.0.113.9")
            for _ in range(6):
                guesser.post("/login", data={"username": "admin", "password": "nope"})
        events = [k for k, _ in _queued(config)]
        assert events.count("security_lockout") == 1
        assert any("3 failed attempts from 203.0.113.9" in s for _, s in _queued(config))

    def test_wrong_setup_codes_trip_the_same_lockout_event(self):
        config = _config(max_attempts=2)
        app = create_app(config)
        with TestClient(app):
            c = _client(app, "203.0.113.9")
            for _ in range(4):
                c.post("/setup", data={"setup_code": "0000-0000-0000", "username": "admin",
                                       "password": PASSWORD, "password_confirm": PASSWORD})
        assert [k for k, _ in _queued(config)].count("security_lockout") == 1
        assert any(s.startswith("Setup code:") for _, s in _queued(config))

    def test_a_sign_in_from_a_new_address_is_news_the_second_time_only(self):
        config = _config()
        app = create_app(config)
        with TestClient(app):
            _setup(app, "192.168.1.20")             # first session: not news
            _login(app, "192.168.1.20")             # same address: not news
            _login(app, "192.168.1.77")             # new address: news
            _login(app, "192.168.1.77")             # seen now: not news
        subjects = [s for k, s in _queued(config) if k == "security_new_address"]
        assert subjects == ["Signed in from a new address: 192.168.1.77"]

    def test_changing_the_password_is_an_event(self):
        config = _config()
        app = create_app(config)
        with TestClient(app):
            c = _setup(app, "192.168.1.20")
            response = c.post("/preferences/password", data={
                "current_password": PASSWORD, "new_password": "a different long phrase",
                "new_password_confirm": "a different long phrase"})
            assert response.status_code == 303
        subjects = [s for k, s in _queued(config) if k == "security_password"]
        assert subjects == ["The password was changed under Preferences from 192.168.1.20"]

    def test_resetting_it_from_the_command_line_is_an_event(self, monkeypatch):
        import io

        from spark.cli import reset_password_main

        config = _config()
        app = create_app(config)
        with TestClient(app):
            _setup(app)
            monkeypatch.delenv("SPARK_CONFIG", raising=False)
            monkeypatch.setenv("SPARK__APP__DATA_DIR", str(config.app.data_dir))
            monkeypatch.setattr("sys.stdin", io.StringIO("a different long phrase\n"))
            assert reset_password_main(["--password-stdin"]) == 0
        subjects = [s for k, s in _queued(config) if k == "security_password"]
        assert subjects == ["The password was reset with spark-reset-password"]


class TestSwitchedOff:
    def test_the_toggle_silences_every_sign_in_event(self):
        config = _config(max_attempts=2)
        app = create_app(config)
        with TestClient(app):
            c = _setup(app, "192.168.1.20")
            page = c.get("/settings/alerts").text
            assert 'name="notify_on_security"' in page and "checked" in page.split('name="notify_on_security"')[1][:40]
            response = c.post("/settings/alerts", data={"enabled": "1", "notify_on_recovery": "1"})
            assert response.status_code == 303
            _login(app, "192.168.1.77")
            guesser = _client(app, "203.0.113.9")
            for _ in range(3):
                guesser.post("/login", data={"username": "admin", "password": "nope"})
            c.post("/preferences/password", data={
                "current_password": PASSWORD, "new_password": "a different long phrase",
                "new_password_confirm": "a different long phrase"})
        kinds = [k for k, _ in _queued(config)]
        assert kinds == ["security_setup"]      # from before the toggle went off
