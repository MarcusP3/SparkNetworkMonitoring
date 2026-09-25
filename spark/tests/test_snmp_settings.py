"""SNMP profiles, devices, and the Test button.

What matters most here is where the secrets are *not*:

  * not in the database file -- checked by reading the raw bytes of the
    SQLite file and its WAL, not by trusting the ORM;
  * not in any page -- after saving, and after a validation error, which is the
    easy place to leak one by "helpfully" re-filling the form;
  * not recoverable from a copied database without its secret.key.

Then the ordinary contract: validation messages, blank-means-keep on edit, a
profile in use cannot be deleted, and Test records what a real agent answers.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select, text

from spark import db as D
from spark.config import Config
from spark.main import create_app
from spark.models import Device, SnmpDevice, SnmpProfile, utcnow
from spark.vault import SecretUnavailable, Vault, vault_for

PASSWORD = "correct horse battery"
COMMUNITY = "c0mmunity-that-must-never-leak"
AUTH_KEY = "auth-key-that-must-never-leak"
PRIV_KEY = "priv-key-that-must-never-leak"
SECRETS = (COMMUNITY, AUTH_KEY, PRIV_KEY)


def make_config(tmp: Path) -> Config:
    config = Config.model_validate({
        "app": {"data_dir": str(tmp / "data"), "port": 9708, "log_level": "WARNING"},
        "network": {"subnets": [{"name": "Loop", "cidr": "127.0.0.0/24", "attached": True}]},
    })
    config.app.data_dir.mkdir(parents=True, exist_ok=True)
    return config


@pytest.fixture
def env():
    tmp = Path(tempfile.mkdtemp(prefix="spark-snmp-"))
    cfg = make_config(tmp)

    async def seed():
        D.init_engine(cfg)
        await D.init_db(cfg)
        async with D.session_scope() as s:
            s.add(Device(mac="aa:bb:cc:00:00:01", primary_ip="127.0.0.1",
                         friendly_name="lab-switch", last_seen=utcnow()))
            s.add(Device(mac="aa:bb:cc:00:00:02", primary_ip="127.0.0.2",
                         friendly_name="spare", last_seen=utcnow()))
        await D.close_engine()

    asyncio.run(seed())
    with TestClient(create_app(cfg), follow_redirects=False) as client:
        client.post("/setup", data={"username": "admin", "password": PASSWORD,
                                    "password_confirm": PASSWORD})
        yield client, cfg


def v2c(name="home", community=COMMUNITY, port="161", **extra) -> dict:
    return {"name": name, "version": "v2c", "community": community, "port": port, **extra}


def v3(name="secure", auth_key=AUTH_KEY, priv_key=PRIV_KEY, priv_protocol="AES",
       username="spark", port="161", **extra) -> dict:
    return {"name": name, "version": "v3", "username": username, "auth_protocol": "SHA",
            "auth_key": auth_key, "priv_protocol": priv_protocol, "priv_key": priv_key,
            "port": port, **extra}


def rows(cfg, model):  # type: ignore[no-untyped-def]
    async def go():
        D.init_engine(cfg)
        async with D.session_scope() as s:
            result = list((await s.execute(select(model))).scalars().all())
            for r in result:
                s.expunge(r)
        return result
    return asyncio.run(go())


def raw_database_bytes(cfg) -> bytes:
    """Every byte SQLite has written, main file and WAL alike.

    Reading through the ORM would only prove the ORM hands back ciphertext. A
    secret that sat in the WAL or a freed page would still be in the file, and
    the file is what gets copied into a backup.
    """
    data = b""
    for suffix in ("", "-wal", "-journal"):
        path = Path(str(cfg.app.db_path) + suffix)
        if path.exists():
            data += path.read_bytes()
    return data


# --------------------------------------------------------------------------
# The secrets
# --------------------------------------------------------------------------


class TestSecretsStayPut:
    def test_no_secret_is_in_the_database_file(self, env):
        client, cfg = env
        assert client.post("/settings/snmp/profiles", data=v2c()).status_code == 303
        assert client.post("/settings/snmp/profiles", data=v3()).status_code == 303
        raw = raw_database_bytes(cfg)
        assert raw, "found no database bytes at all -- the test would prove nothing"
        for secret in SECRETS:
            assert secret.encode() not in raw, f"{secret!r} is in the database file in clear"

    def test_what_is_stored_decrypts_to_what_was_typed(self, env):
        client, cfg = env
        client.post("/settings/snmp/profiles", data=v3())
        profile = rows(cfg, SnmpProfile)[0]
        vault = vault_for(cfg)
        assert vault.open(profile.auth_key_sealed) == AUTH_KEY
        assert vault.open(profile.priv_key_sealed) == PRIV_KEY

    def test_no_secret_is_on_the_settings_page(self, env):
        client, _ = env
        client.post("/settings/snmp/profiles", data=v2c())
        client.post("/settings/snmp/profiles", data=v3())
        page = client.get("/settings/snmp").text
        for secret in SECRETS:
            assert secret not in page

    def test_a_validation_error_does_not_echo_the_secret_back(self, env):
        # The easy leak: re-filling the whole form after an error. The name is
        # missing, so this fails -- and the page must come back without the
        # keys that were typed alongside it.
        client, _ = env
        response = client.post("/settings/snmp/profiles", data=v3(name=""))
        assert response.status_code == 400
        assert "Give the profile a name" in response.text
        for secret in (AUTH_KEY, PRIV_KEY):
            assert secret not in response.text
        # ...while what is safe to keep, is kept.
        assert 'value="spark"' in response.text

    def test_a_copied_database_without_its_key_opens_nothing(self, env):
        client, cfg = env
        client.post("/settings/snmp/profiles", data=v2c())
        profile = rows(cfg, SnmpProfile)[0]
        with pytest.raises(SecretUnavailable):
            Vault("a-different-install-" * 3).open(profile.community_sealed)

    def test_password_fields_ask_the_browser_not_to_autofill(self, env):
        # Browsers fill a saved login password into the first password box on
        # a page. On this form that would silently save the SPARK admin
        # password as a community string.
        client, _ = env
        page = client.get("/settings/snmp").text
        for field in ("community", "auth_key", "priv_key"):
            assert f'name="{field}" autocomplete="new-password"' in page, field


# --------------------------------------------------------------------------
# Profiles
# --------------------------------------------------------------------------


class TestProfiles:
    def test_blank_secret_on_edit_keeps_the_stored_one(self, env):
        client, cfg = env
        client.post("/settings/snmp/profiles", data=v2c())
        pid = rows(cfg, SnmpProfile)[0].id
        client.post(f"/settings/snmp/profiles/{pid}", data=v2c(name="renamed", community=""))
        profile = rows(cfg, SnmpProfile)[0]
        assert profile.name == "renamed"
        assert vault_for(cfg).open(profile.community_sealed) == COMMUNITY

    def test_a_new_secret_on_edit_replaces_it(self, env):
        client, cfg = env
        client.post("/settings/snmp/profiles", data=v2c())
        pid = rows(cfg, SnmpProfile)[0].id
        client.post(f"/settings/snmp/profiles/{pid}", data=v2c(community="brand-new"))
        assert vault_for(cfg).open(rows(cfg, SnmpProfile)[0].community_sealed) == "brand-new"

    def test_a_failed_edit_changes_nothing(self, env):
        """A new auth key that is fine, alongside a privacy key that is not.

        The first version assigned fields as it validated them, so this edit
        replaced the stored auth key and then failed on the privacy key --
        leaving a profile that matched neither the old credentials nor the new
        ones. Validate everything, then apply.
        """
        client, cfg = env
        client.post("/settings/snmp/profiles", data=v3())
        pid = rows(cfg, SnmpProfile)[0].id
        response = client.post(f"/settings/snmp/profiles/{pid}",
                               data=v3(name="renamed", auth_key="a-new-auth-key", priv_key="short"))
        assert response.status_code == 400
        profile = rows(cfg, SnmpProfile)[0]
        assert profile.name == "secure"
        assert vault_for(cfg).open(profile.auth_key_sealed) == AUTH_KEY

    def test_switching_to_v2c_drops_the_v3_secrets(self, env):
        # Ciphertext for credentials nothing uses any more is risk kept for
        # no reason.
        client, cfg = env
        client.post("/settings/snmp/profiles", data=v3())
        pid = rows(cfg, SnmpProfile)[0].id
        client.post(f"/settings/snmp/profiles/{pid}", data=v2c(name="secure"))
        profile = rows(cfg, SnmpProfile)[0]
        assert profile.version == "v2c"
        assert profile.auth_key_sealed is None and profile.priv_key_sealed is None
        assert profile.username is None

    def test_auth_no_priv_is_allowed_and_labelled_as_such(self, env):
        client, cfg = env
        assert client.post("/settings/snmp/profiles",
                           data=v3(priv_protocol="none", priv_key="")).status_code == 303
        profile = rows(cfg, SnmpProfile)[0]
        assert profile.priv_protocol is None and profile.priv_key_sealed is None
        assert "authNoPriv" in client.get("/settings/snmp").text

    @pytest.mark.parametrize("data,message", [
        (v2c(name=""), "Give the profile a name"),
        (v2c(community=""), "Enter the community string"),
        (v2c(port="70000"), "not a port number"),
        (v2c(version="v1"), "Choose SNMP v2c or v3"),
        (v3(username=""), "Enter the SNMPv3 user name"),
        (v3(auth_key="short"), "at least 8 characters"),
        (v3(priv_key="short"), "at least 8 characters"),
        (v3(auth_key=""), "Enter the authentication password"),
        (v3(priv_key=""), "Enter the privacy password"),
        ({**v3(), "auth_protocol": "SHA-1"}, "authentication protocol from the list"),
        ({**v3(), "priv_protocol": "ROT13"}, "privacy protocol from the list"),
    ])
    def test_validation(self, env, data, message):
        client, cfg = env
        response = client.post("/settings/snmp/profiles", data=data)
        assert response.status_code == 400
        assert message in response.text
        assert rows(cfg, SnmpProfile) == []

    def test_names_are_unique_ignoring_case(self, env):
        client, _ = env
        client.post("/settings/snmp/profiles", data=v2c(name="Home"))
        response = client.post("/settings/snmp/profiles", data=v2c(name="home"))
        assert response.status_code == 400
        assert "already a profile" in response.text

    def test_a_profile_in_use_cannot_be_deleted(self, env):
        client, cfg = env
        client.post("/settings/snmp/profiles", data=v2c())
        pid = rows(cfg, SnmpProfile)[0].id
        client.post("/settings/snmp/devices", data={"device_id": "1", "profile_id": str(pid)})
        response = client.post(f"/settings/snmp/profiles/{pid}/delete")
        assert response.status_code == 400
        assert "used by 1 device" in response.text
        assert len(rows(cfg, SnmpProfile)) == 1

    def test_an_unused_profile_can_be_deleted(self, env):
        client, cfg = env
        client.post("/settings/snmp/profiles", data=v2c())
        pid = rows(cfg, SnmpProfile)[0].id
        assert client.post(f"/settings/snmp/profiles/{pid}/delete").status_code == 303
        assert rows(cfg, SnmpProfile) == []


# --------------------------------------------------------------------------
# Devices
# --------------------------------------------------------------------------


class TestDevices:
    def test_adding_a_device_takes_it_off_the_candidate_list(self, env):
        client, cfg = env
        client.post("/settings/snmp/profiles", data=v2c())
        client.post("/settings/snmp/devices", data={"device_id": "1", "profile_id": "1"})
        page = client.get("/settings/snmp").text
        assert len(rows(cfg, SnmpDevice)) == 1
        assert '<option value="1">lab-switch' not in page, "still offered after being added"
        assert '<option value="2">spare' in page

    def test_a_device_cannot_be_added_twice(self, env):
        client, _ = env
        client.post("/settings/snmp/profiles", data=v2c())
        client.post("/settings/snmp/devices", data={"device_id": "1", "profile_id": "1"})
        response = client.post("/settings/snmp/devices", data={"device_id": "1", "profile_id": "1"})
        assert response.status_code == 400
        assert "already on the list" in response.text

    def test_an_empty_choice_is_a_message_not_a_crash(self, env):
        client, _ = env
        client.post("/settings/snmp/profiles", data=v2c())
        response = client.post("/settings/snmp/devices", data={"device_id": "", "profile_id": "1"})
        assert response.status_code == 400
        assert "Choose a device" in response.text

    def test_changing_profile_clears_the_old_result(self, env):
        # The recorded result was for the old credentials; keeping it would
        # show a claim nobody has checked.
        client, cfg = env
        client.post("/settings/snmp/profiles", data=v2c(name="a"))
        client.post("/settings/snmp/profiles", data=v2c(name="b"))
        client.post("/settings/snmp/devices", data={"device_id": "1", "profile_id": "1"})

        async def fake_result():
            D.init_engine(cfg)
            async with D.session_scope() as s:
                row = (await s.execute(select(SnmpDevice))).scalars().one()
                row.last_checked_at = utcnow()
                row.last_probe = {"reachable": True, "capabilities": []}
        asyncio.run(fake_result())

        client.post("/settings/snmp/devices/1/profile", data={"profile_id": "2"})
        row = rows(cfg, SnmpDevice)[0]
        assert row.profile_id == 2
        assert row.last_checked_at is None and row.last_probe is None

    def test_removing_a_device(self, env):
        client, cfg = env
        client.post("/settings/snmp/profiles", data=v2c())
        client.post("/settings/snmp/devices", data={"device_id": "1", "profile_id": "1"})
        client.post("/settings/snmp/devices/1/delete")
        assert rows(cfg, SnmpDevice) == []

    def test_every_route_needs_a_login(self, env):
        client, cfg = env
        client.post("/logout")
        for path, data in (("/settings/snmp/profiles", v2c()),
                           ("/settings/snmp/devices", {"device_id": "1", "profile_id": "1"}),
                           ("/settings/snmp/devices/1/test", {})):
            response = client.post(path, data=data)
            assert response.status_code == 303 and "/login" in response.headers["location"], path
        assert rows(cfg, SnmpProfile) == []


# --------------------------------------------------------------------------
# Test, against the live agent
# --------------------------------------------------------------------------


def _agent_up() -> bool:
    from spark.collectors import SnmpCollector, SnmpCredential

    async def ask():
        c = SnmpCollector("127.0.0.1", SnmpCredential(community="sparktest", port=11161,
                                                      timeout=1.0, retries=0))
        try:
            return bool(await c.get("1.3.6.1.2.1.1.5.0"))
        except Exception:  # noqa: BLE001
            return False
        finally:
            await c.close()
    return asyncio.run(ask())


needs_agent = pytest.mark.skipif(not _agent_up(),
                                 reason="no agent on 127.0.0.1:11161 -- run tests/local_agent.sh start")


@needs_agent
class TestTheTestButton:
    def _add(self, client, profile: dict) -> None:
        assert client.post("/settings/snmp/profiles", data=profile).status_code == 303
        client.post("/settings/snmp/devices", data={"device_id": "1", "profile_id": "1"})

    def test_v2c_records_what_the_device_supports(self, env):
        client, cfg = env
        self._add(client, v2c(community="sparktest", port="11161"))
        assert client.post("/settings/snmp/devices/1/test").status_code == 303
        row = rows(cfg, SnmpDevice)[0]
        assert row.last_ok_at is not None and row.last_error is None
        assert row.last_probe["sys_name"] == "test-switch-01"
        page = client.get("/settings/snmp").text
        assert "answering" in page and "test-switch-01" in page
        assert "Supports" in page

    def test_v3_auth_priv_works_through_the_ui(self, env):
        # End to end: typed into the form, sealed, stored, unsealed, and used
        # for an encrypted exchange with a real agent.
        client, cfg = env
        self._add(client, v3(username="sparkv3", auth_key="sparktest-auth",
                             priv_key="sparktest-priv", port="11161"))
        client.post("/settings/snmp/devices/1/test")
        row = rows(cfg, SnmpDevice)[0]
        assert row.last_error is None, row.last_error
        assert row.last_probe["reachable"] is True

    def test_a_wrong_v3_key_says_rejected_not_unreachable(self, env):
        client, cfg = env
        self._add(client, v3(username="sparkv3", auth_key="not-the-auth-key",
                             priv_key="sparktest-priv", port="11161"))
        client.post("/settings/snmp/devices/1/test")
        row = rows(cfg, SnmpDevice)[0]
        assert row.last_ok_at is None
        assert "AuthFailed" in row.last_error and "rejected" in row.last_error
        assert "no answer" in client.get("/settings/snmp").text

    def test_an_undecryptable_credential_says_what_to_do(self, env):
        client, cfg = env
        self._add(client, v2c(community="sparktest", port="11161"))
        # The restored-onto-a-fresh-install case: same database, new key.
        # The key is read once per process and cached on the config, so
        # rewriting the file alone changes nothing mid-run -- the cache is
        # what a restart onto another install actually replaces.
        cfg.app.secret_key_path.write_text("an-entirely-different-install-key-" * 2)
        cfg._secret_key = None
        from spark import vault as vault_module
        vault_module._vault.cache_clear()

        assert client.post("/settings/snmp/devices/1/test").status_code == 303
        assert "enter the credential again" in rows(cfg, SnmpDevice)[0].last_error


# --------------------------------------------------------------------------
# Migration
# --------------------------------------------------------------------------


async def _wind_back_to_5(session) -> None:  # type: ignore[no-untyped-def]
    """Make a database look like version 5: no SNMP tables of any kind.

    The polling tables (migration 7) reference snmp_device, so they go first;
    a real version-5 database never had them.
    """
    for table in ("snmp_interface_rollup", "snmp_interface_sample", "snmp_health_rollup",
                  "snmp_health_sample", "snmp_interface", "snmp_poll",
                  "snmp_device", "snmp_profile"):
        await session.execute(text(f"DROP TABLE {table}"))
    await D._set_version(session, 5)


class TestMigration:
    def test_an_existing_database_gains_the_tables(self):
        """A version-5 database, with no SNMP tables, upgrades cleanly."""
        cfg = make_config(Path(tempfile.mkdtemp(prefix="spark-snmp-mig-")))

        async def build_v5():
            D.init_engine(cfg)
            await D.init_db(cfg)
            async with D.session_scope() as s:
                await _wind_back_to_5(s)
            await D.close_engine()

        async def upgrade():
            D.init_engine(cfg)
            await D.init_db(cfg)
            async with D.session_scope() as s:
                connection = await s.connection()
                tables = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))
                version = await D._get_version(s)
            await D.close_engine()
            return tables, version

        asyncio.run(build_v5())
        tables, version = asyncio.run(upgrade())
        assert {"snmp_profile", "snmp_device"} <= tables
        assert version == D.CURRENT_VERSION >= 6

    def test_the_migrated_tables_match_a_fresh_install(self):
        fresh = make_config(Path(tempfile.mkdtemp(prefix="spark-snmp-fresh-")))
        migrated = make_config(Path(tempfile.mkdtemp(prefix="spark-snmp-migd-")))

        async def columns(cfg, wind_back):
            D.init_engine(cfg)
            await D.init_db(cfg)
            if wind_back:
                async with D.session_scope() as s:
                    await _wind_back_to_5(s)
                await D.close_engine()
                D.init_engine(cfg)
                await D.init_db(cfg)
            async with D.session_scope() as s:
                connection = await s.connection()
                shape = await connection.run_sync(lambda sync: {
                    table: sorted((c["name"], str(c["type"]), c["nullable"])
                                  for c in inspect(sync).get_columns(table))
                    for table in ("snmp_profile", "snmp_device")
                })
            await D.close_engine()
            return shape

        assert asyncio.run(columns(fresh, False)) == asyncio.run(columns(migrated, True))


# --------------------------------------------------------------------------
# Polling controls on the card
# --------------------------------------------------------------------------


class TestPollingControls:
    def _listed(self, client) -> None:  # type: ignore[no-untyped-def]
        assert client.post("/settings/snmp/profiles", data=v2c()).status_code == 303
        assert client.post("/settings/snmp/devices",
                           data={"device_id": "1", "profile_id": "1"}).status_code == 303

    def test_the_interval_is_saved_and_only_offered_values_are_accepted(self, env):
        client, cfg = env
        assert "Poll every" in client.get("/settings/snmp").text
        assert client.post("/settings/snmp/polling",
                           data={"interval_seconds": "300"}).status_code == 303

        async def read():
            D.init_engine(cfg)
            async with D.session_scope() as s:
                return await D.get_setting(s, "snmp")
        assert asyncio.run(read())["poll_interval_seconds"] == 300

        bad = client.post("/settings/snmp/polling", data={"interval_seconds": "45"})
        assert bad.status_code == 400 and "polling interval" in bad.text
        assert asyncio.run(read())["poll_interval_seconds"] == 300

    def test_pause_and_resume(self, env):
        client, cfg = env
        self._listed(client)
        client.post("/settings/snmp/devices/1/polling", data={})
        assert rows(cfg, SnmpDevice)[0].enabled is False
        assert "paused" in client.get("/settings/snmp").text
        client.post("/settings/snmp/devices/1/polling", data={"enabled": "1"})
        assert rows(cfg, SnmpDevice)[0].enabled is True

    def test_adding_a_device_schedules_its_poll(self, env):
        client, _cfg = env
        from spark import scheduler as scheduler_module
        self._listed(client)
        assert scheduler_module.get_scheduler().get_job("snmp:1") is not None
        client.post("/settings/snmp/devices/1/delete")
        assert scheduler_module.get_scheduler().get_job("snmp:1") is None

    def test_the_latest_poll_is_shown(self, env):
        client, cfg = env
        self._listed(client)
        from spark.models import SnmpPoll

        async def seed():
            D.init_engine(cfg)
            async with D.session_scope() as s:
                s.add(SnmpPoll(snmp_device_id=1, last_polled_at=utcnow(),
                               last_ok_at=utcnow(), cpu_percent=12.6, memory_percent=40.2,
                               interfaces_total=26, interfaces_up=4))
        asyncio.run(seed())
        page = client.get("/settings/snmp").text
        assert "CPU 13%" in page and "Memory 40%" in page
        assert "4 of 26 interfaces up" in page
        assert "Polled " in page
