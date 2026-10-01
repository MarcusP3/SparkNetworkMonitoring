"""First-run setup needs the code from the log (review finding #23).

Before: the first request to reach /setup on a fresh install created the
administrator -- from the LAN before the owner got round to it, from the
internet on an exposed instance, or from a victim's browser by DNS rebinding.
Now the form needs a code SPARK printed to its own log, wrong guesses count
toward the login lockout, and the database allows one administrator however
many requests race to create one.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from spark import db as D
from spark.auth import new_setup_code, setup_code_matches
from spark.config import Config
from spark.main import create_app
from spark.models import User

PASSWORD = "correct horse battery"


def _config(**auth) -> Config:  # type: ignore[no-untyped-def]
    tmp = Path(tempfile.mkdtemp(prefix="spark-setup-"))
    config = Config.model_validate(
        {"app": {"data_dir": str(tmp / "data"), "log_level": "WARNING"},
         "auth": auth, "network": {"subnets": []}}
    )
    config.app.data_dir.mkdir(parents=True, exist_ok=True)
    return config


def _form(code: str, **over: str) -> dict:
    form = {"setup_code": code, "username": "admin", "password": PASSWORD,
            "password_confirm": PASSWORD, "timezone": "UTC"}
    form.update(over)
    return form


def _users(config: Config) -> int:
    with sqlite3.connect(config.app.db_path) as con:
        return con.execute("select count(*) from user").fetchone()[0]


class TestTheCode:
    def test_it_is_twelve_characters_in_three_groups(self):
        code = new_setup_code()
        parts = code.split("-")
        assert len(parts) == 3 and all(len(p) == 4 and p.isalnum() for p in parts)
        assert new_setup_code() != code

    def test_case_spaces_and_dashes_do_not_matter(self):
        assert setup_code_matches("A1B2-C3D4-E5F6", "a1b2c3d4e5f6")
        assert setup_code_matches("A1B2-C3D4-E5F6", " a1b2 c3d4 e5f6 ")
        assert not setup_code_matches("A1B2-C3D4-E5F6", "A1B2-C3D4-E5F7")
        assert not setup_code_matches(None, "A1B2-C3D4-E5F6")
        assert not setup_code_matches("A1B2-C3D4-E5F6", "")


class TestSetup:
    def test_the_code_is_logged_at_start_and_never_on_the_page(self, caplog):
        config = _config()
        with caplog.at_level(logging.WARNING, logger="spark.auth"):
            app = create_app(config)
            with TestClient(app, follow_redirects=False) as c:
                code = app.state.setup_code
                assert code and code in caplog.text
                assert "setup code" in caplog.text.lower()
                page = c.get("/setup")
        assert page.status_code == 200
        assert 'name="setup_code"' in page.text
        assert code not in page.text

    def test_without_the_code_nothing_is_created(self):
        config = _config()
        app = create_app(config)
        with TestClient(app, follow_redirects=False) as c:
            response = c.post("/setup", data=_form(""))
            assert response.status_code == 400
            assert "not the setup code" in response.text
            assert "docker compose logs" in response.text
            assert "set-cookie" not in response.headers
            wrong = c.post("/setup", data=_form("0000-0000-0000"))
            assert wrong.status_code == 400
        assert _users(config) == 0

    def test_with_the_code_the_account_is_created_and_the_code_is_spent(self):
        config = _config()
        app = create_app(config)
        with TestClient(app, follow_redirects=False) as c:
            code = app.state.setup_code
            response = c.post("/setup", data=_form(code.lower().replace("-", " ")))
            assert response.status_code == 303 and "spark_session" in c.cookies
            assert app.state.setup_code is None
            assert c.get("/").status_code == 200
        assert _users(config) == 1

    def test_a_typo_elsewhere_keeps_the_right_code_on_the_page(self):
        config = _config()
        app = create_app(config)
        with TestClient(app, follow_redirects=False) as c:
            code = app.state.setup_code
            response = c.post("/setup", data=_form(code, password_confirm="something else!"))
            assert response.status_code == 400 and "do not match" in response.text
            assert f'value="{code}"' in response.text

    def test_wrong_codes_count_toward_the_lockout(self):
        config = _config(max_attempts=3)
        app = create_app(config)
        with TestClient(app, follow_redirects=False) as c:
            codes = [c.post("/setup", data=_form("0000-0000-0000")).status_code for _ in range(4)]
            assert codes == [400, 400, 400, 429]
            # Locked out even with the right code, from this address.
            assert c.post("/setup", data=_form(app.state.setup_code)).status_code == 429
        assert _users(config) == 0

    def test_a_code_is_made_on_demand_if_startup_had_none(self, caplog):
        # The admin was deleted while SPARK ran (spark-reset-password does not
        # do this, but a person with sqlite3 might): /setup opens again and
        # must not be open without a code.
        config = _config()
        app = create_app(config)
        with TestClient(app, follow_redirects=False) as c:
            c.post("/setup", data=_form(app.state.setup_code))
            assert app.state.setup_code is None
            with sqlite3.connect(config.app.db_path) as con:
                con.execute("delete from user_session")
                con.execute("delete from user")
            c.cookies.clear()
            with caplog.at_level(logging.WARNING, logger="spark.auth"):
                assert c.get("/setup").status_code == 200
            code = app.state.setup_code
            assert code and code in caplog.text
            assert c.post("/setup", data=_form("")).status_code == 400
            assert c.post("/setup", data=_form(code)).status_code == 303


class TestOneAdministrator:
    def test_a_fresh_database_has_the_index(self):
        config = _config()
        with TestClient(create_app(config)):
            pass
        with sqlite3.connect(config.app.db_path) as con:
            sql = con.execute("select sql from sqlite_master where name = 'ix_user_single_admin'"
                              ).fetchone()[0]
        assert "UNIQUE" in sql.upper() and "is_admin = 1" in sql

    def test_a_second_admin_row_is_refused_by_the_database(self):
        config = _config()
        with TestClient(create_app(config)) as c:
            c.post("/setup", data=_form(c.app.state.setup_code))
        with sqlite3.connect(config.app.db_path) as con, pytest.raises(sqlite3.IntegrityError):
            con.execute("insert into user (username, password_hash, is_admin, created_at, updated_at) "
                        "values ('other', 'x', 1, '2026-01-01', '2026-01-01')")

    def test_migration_adds_the_index_to_an_older_database(self):
        config = _config()
        with TestClient(create_app(config)) as c:
            c.post("/setup", data=_form(c.app.state.setup_code))
        with sqlite3.connect(config.app.db_path) as con:
            con.execute("drop index ix_user_single_admin")
            con.execute("update schema_version set version = 19")
        with TestClient(create_app(config)):
            pass
        with sqlite3.connect(config.app.db_path) as con:
            assert con.execute("select count(*) from sqlite_master where name = 'ix_user_single_admin'"
                               ).fetchone()[0] == 1
            assert con.execute("select version from schema_version").fetchone()[0] >= 20

    def test_migration_refuses_to_guess_between_two_admins(self):
        config = _config()
        with TestClient(create_app(config)) as c:
            c.post("/setup", data=_form(c.app.state.setup_code))
        with sqlite3.connect(config.app.db_path) as con:
            con.execute("drop index ix_user_single_admin")
            con.execute("insert into user (username, password_hash, is_admin, created_at, updated_at) "
                        "values ('other', 'x', 1, '2026-01-01', '2026-01-01')")
            con.execute("update schema_version set version = 19")
        async def start():
            D.init_engine(config)
            try:
                await D.init_db(config)
            finally:
                await D.close_engine()
        with pytest.raises(SystemExit) as stopped:
            asyncio.run(start())
        message = str(stopped.value)
        assert "2 administrator accounts" in message and "'admin'" in message and "'other'" in message
        assert "DELETE FROM user WHERE id != " in message

    def test_create_admin_turns_the_constraint_into_a_message(self, monkeypatch):
        from spark import auth
        from spark.auth import AuthError, create_admin

        config = _config()
        with TestClient(create_app(config)) as c:
            c.post("/setup", data=_form(c.app.state.setup_code))

        # The race, compressed: the count said "none yet" (patched here, the
        # way a request that checked a moment too early would have seen it)
        # and by the time this insert flushes another has landed.
        async def none_yet(_session):  # type: ignore[no-untyped-def]
            return True
        monkeypatch.setattr(auth, "setup_required", none_yet)

        async def race():
            D.init_engine(config)
            async with D.session_scope() as session:
                try:
                    await create_admin(session, "second", PASSWORD)
                except AuthError as exc:
                    return str(exc)
                return None
        assert asyncio.run(race()) == "An administrator account already exists"
        assert _users(config) == 1


class TestConfigFile:
    """`config/spark.yaml` is the operator's copy and stays out of git (#32);
    a container whose SPARK_CONFIG points at nothing must say so, not run on
    defaults."""

    def test_the_shipped_file_is_the_example_and_the_real_one_is_ignored(self):
        spark = Path(__file__).resolve().parents[1]
        assert (spark / "config" / "spark.example.yaml").exists()
        assert "config/spark.yaml" in (spark / ".gitignore").read_text()

    def test_a_dangling_spark_config_stops_with_the_copy_command(self, monkeypatch, tmp_path):
        from spark.config import load_config

        monkeypatch.setenv("SPARK_CONFIG", str(tmp_path / "missing.yaml"))
        with pytest.raises(SystemExit) as stopped:
            load_config()
        assert "cp config/spark.example.yaml config/spark.yaml" in str(stopped.value)

    def test_without_spark_config_the_defaults_still_serve_development(self, monkeypatch, tmp_path):
        from spark.config import load_config

        monkeypatch.delenv("SPARK_CONFIG", raising=False)
        monkeypatch.setenv("SPARK__APP__DATA_DIR", str(tmp_path / "data"))
        config = load_config()
        assert config.app.data_dir == tmp_path / "data"
        assert config.app.port == 9700

    def test_the_example_loads_as_shipped(self, monkeypatch, tmp_path):
        from spark.config import load_config

        example = Path(__file__).resolve().parents[1] / "config" / "spark.example.yaml"
        monkeypatch.setenv("SPARK__APP__DATA_DIR", str(tmp_path / "data"))
        config = load_config(example)
        assert config.auth.mode == "password"
        assert [s.cidr for s in config.network.subnets] == ["192.168.1.0/24"]
