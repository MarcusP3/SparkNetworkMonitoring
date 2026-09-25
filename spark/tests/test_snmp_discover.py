"""Finding SNMP devices, and seeing which devices are polled.

Find must only ever suggest -- a device that answers is offered, never added
-- and it must not try devices already on the list. The Devices list must say,
for every device, whether SPARK polls it, without opening each one.
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from spark import db as D
from spark import snmp_discover
from spark.config import Config
from spark.main import create_app
from spark.models import Device, SnmpDevice, SnmpPoll, utcnow
from spark.snmp_config import ProfileInput, save_profile
from spark.vault import vault_for
from spark.web.routes_devices import snmp_state

PASSWORD = "correct horse battery"


def _config() -> Config:
    tmp = Path(tempfile.mkdtemp(prefix="spark-find-"))
    config = Config.model_validate({
        "app": {"data_dir": str(tmp / "data"), "log_level": "WARNING"},
        "network": {"subnets": []},
    })
    config.app.data_dir.mkdir(parents=True, exist_ok=True)
    return config


async def _seed(config, profiles: list[ProfileInput], *, devices=("127.0.0.1", "127.0.0.2"),
                ignored=()) -> None:
    D.init_engine(config)
    await D.init_db(config)
    async with D.session_scope() as s:
        for n, ip in enumerate(devices, start=1):
            s.add(Device(mac=f"aa:bb:cc:00:00:{n:02x}", primary_ip=ip, friendly_name=f"dev{n}",
                         ignored=ip in ignored, last_seen=utcnow()))
        await s.flush()
        for p in profiles:
            await save_profile(s, vault_for(config), p)


def _rows(model):
    async def go():
        async with D.session_scope() as s:
            result = list((await s.execute(select(model))).scalars())
            for r in result:
                s.expunge(r)
            return result
    return asyncio.run(go())


# --------------------------------------------------------------------------
# The SNMP column's wording
# --------------------------------------------------------------------------


class TestState:
    def test_not_listed_is_nothing(self):
        assert snmp_state(None, None) is None

    def test_paused_and_waiting_are_neutral(self):
        assert snmp_state(SnmpDevice(enabled=False), None) == {"label": "paused", "kind": "neutral"}
        assert snmp_state(SnmpDevice(enabled=True), None)["label"] == "waiting"

    def test_answering_and_not(self):
        ok = SnmpPoll(last_polled_at=utcnow(), last_error=None)
        bad = SnmpPoll(last_polled_at=utcnow(), last_error="Unreachable: timeout")
        assert snmp_state(SnmpDevice(enabled=True), ok) == {"label": "polling", "kind": "ok"}
        assert snmp_state(SnmpDevice(enabled=True), bad) == {"label": "no answer", "kind": "bad"}


def test_a_run_cut_off_by_a_restart_does_not_block_the_button_forever():
    now = utcnow()
    assert snmp_discover.is_running({"running": True, "started_at": now.isoformat()}, now)
    old = (now - timedelta(minutes=11)).isoformat()
    assert not snmp_discover.is_running({"running": True, "started_at": old}, now)
    assert not snmp_discover.is_running({}, now)


# --------------------------------------------------------------------------
# Discovery, with the network faked
# --------------------------------------------------------------------------


@pytest.fixture
def fake_network(monkeypatch):
    """127.0.0.1 answers profile "right"; 127.0.0.3 refuses every profile."""
    asked: list[tuple[str, str]] = []

    async def ask(address, credential):  # type: ignore[no-untyped-def]
        asked.append((address, credential.community or credential.username))
        if address == "127.0.0.3":
            return "refused", None
        if address == "127.0.0.1" and credential.community == "right":
            return "found", {"sys_name": "lab-switch", "vendor": "net-snmp", "sys_descr": "Linux"}
        return "silent", None

    monkeypatch.setattr(snmp_discover, "_ask", ask)
    return asked


class TestDiscovery:
    def test_the_first_profile_that_answers_is_recorded_and_nothing_is_added(self, fake_network):
        config = _config()

        async def go():
            await _seed(config, [
                ProfileInput(name="a-wrong", version="v2c", community="wrong"),
                ProfileInput(name="b-right", version="v2c", community="right"),
                ProfileInput(name="c-never", version="v2c", community="never"),
            ], devices=("127.0.0.1", "127.0.0.2", "127.0.0.3"))
            return await snmp_discover.run_discovery(config)

        state = asyncio.run(go())
        assert state["running"] is False and state["devices"] == 3
        assert [(f["address"], f["profile"]) for f in state["found"]] == [("127.0.0.1", "b-right")]
        assert state["found"][0]["sys_name"] == "lab-switch"
        assert [r["address"] for r in state["refused"]] == ["127.0.0.3"]
        # Stops at the first answer: "never" is not sent to 127.0.0.1.
        assert ("127.0.0.1", "never") not in fake_network
        assert _rows(SnmpDevice) == [], "Find suggests; it must never add"
        asyncio.run(D.close_engine())

    def test_listed_and_ignored_devices_are_not_tried(self, fake_network):
        config = _config()

        async def go():
            await _seed(config, [ProfileInput(name="p", version="v2c", community="right")],
                        devices=("127.0.0.1", "127.0.0.2", "127.0.0.4"), ignored=("127.0.0.4",))
            async with D.session_scope() as s:
                s.add(SnmpDevice(device_id=1, profile_id=1, enabled=True))
            return await snmp_discover.run_discovery(config)

        state = asyncio.run(go())
        assert {a for a, _ in fake_network} == {"127.0.0.2"}
        assert state["devices"] == 1 and state["found"] == []
        asyncio.run(D.close_engine())

    def test_a_crash_is_recorded_and_clears_running(self, monkeypatch):
        config = _config()

        async def boom(config, started):  # type: ignore[no-untyped-def]
            raise RuntimeError("socket exhaustion")

        monkeypatch.setattr(snmp_discover, "_run", boom)

        async def go():
            await _seed(config, [])
            await snmp_discover.run_discovery(config)
            async with D.session_scope() as s:
                return await snmp_discover.load_state(s)

        state = asyncio.run(go())
        assert state["running"] is False and "socket exhaustion" in state["error"]
        asyncio.run(D.close_engine())


# --------------------------------------------------------------------------
# The pages
# --------------------------------------------------------------------------


@pytest.fixture
def site(fake_network):
    config = _config()

    async def seed():
        await _seed(config, [ProfileInput(name="lab", version="v2c", community="right")],
                    devices=("127.0.0.1", "127.0.0.2", "127.0.0.3", "127.0.0.5"))
        # dev3 polled and answering, dev4 on the list but paused.
        async with D.session_scope() as s:
            s.add(SnmpDevice(device_id=3, profile_id=1, enabled=True))
            s.add(SnmpDevice(device_id=4, profile_id=1, enabled=False))
            await s.flush()
            s.add(SnmpPoll(snmp_device_id=1, last_polled_at=utcnow(), last_ok_at=utcnow()))
        await D.close_engine()

    asyncio.run(seed())
    with TestClient(create_app(config), follow_redirects=False) as client:
        client.post("/setup", data={"username": "admin", "password": PASSWORD,
                                    "password_confirm": PASSWORD})
        yield client, config


def _table(page: str) -> str:
    start = page.index("<tbody>", page.index("<th>SNMP</th>"))
    return page[start: page.index("</tbody>", start)]


class TestDevicesList:
    def test_every_device_says_whether_it_is_polled(self, site):
        client, _ = site
        body = _table(client.get("/devices").text)
        assert 'class="pill ok dot"' in body and ">polling</a>" in body
        assert ">paused</a>" in body
        assert body.count('href="/devices/3" class="pill') == 1

    def test_the_filter_narrows_to_polled_or_not(self, site):
        client, _ = site
        on = _table(client.get("/devices?snmp=on").text)
        assert "dev3" in on and "dev4" in on and "dev1" not in on
        off = _table(client.get("/devices?snmp=off").text)
        assert "dev1" in off and "dev3" not in off
        everything = _table(client.get("/devices?snmp=bogus").text)
        assert "dev1" in everything and "dev3" in everything

    def test_paging_links_keep_the_filter(self, site):
        client, _ = site
        page = client.get("/devices?snmp=off&per_page=25").text
        assert 'name="snmp" id="snmp-filter"' in page
        from spark.web.routes_devices import _query
        assert _query("", 25, 2, "on") == "?snmp=on&per_page=25&page=2"
        assert _query("", 50, 1, "bogus") == ""


class TestFindButton:
    def test_it_needs_a_profile(self, site):
        client, config = site

        async def drop():
            D.init_engine(config)
            async with D.session_scope() as s:
                from spark.models import SnmpProfile
                for row in (await s.execute(select(SnmpDevice))).scalars():
                    await s.delete(row)
                await s.flush()
                for p in (await s.execute(select(SnmpProfile))).scalars():
                    await s.delete(p)
        asyncio.run(drop())
        response = client.post("/settings/snmp/discover")
        assert response.status_code == 400 and "Add a profile first" in response.text

    def test_found_devices_are_offered_and_add_all_adds_them_with_their_profile(self, site):
        client, config = site
        assert client.post("/settings/snmp/discover").status_code == 303
        page = client.get("/settings/snmp").text
        assert "Searching" in page  # marked running before the job starts

        asyncio.run(snmp_discover.run_discovery(config))
        page = client.get("/settings/snmp").text
        assert "lab-switch" in page and "Add all" not in page  # one found: no "all"
        assert '<input type="hidden" name="device_id" value="1">' in page
        # The device that refuses every profile (127.0.0.3) is already on the
        # list, so it was never tried and is not reported.
        assert "refused the credentials" not in page

        client.post("/settings/snmp/discover/add-all")
        listed = {(r.device_id, r.profile_id) for r in _rows(SnmpDevice)}
        assert (1, 1) in listed
        assert "lab-switch" not in client.get("/settings/snmp").text, "added: no longer suggested"

    def test_add_all_skips_a_device_ignored_since(self, site):
        client, config = site
        asyncio.run(snmp_discover.run_discovery(config))

        async def ignore():
            async with D.session_scope() as s:
                (await s.get(Device, 1)).ignored = True
        asyncio.run(ignore())
        client.post("/settings/snmp/discover/add-all")
        assert 1 not in {r.device_id for r in _rows(SnmpDevice)}


# --------------------------------------------------------------------------
# Against a real agent
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


@pytest.mark.skipif(not _agent_up(), reason="no agent on 127.0.0.1:11161")
def test_a_real_agent_is_found_and_a_wrong_v3_password_is_reported_as_refused():
    config = _config()

    async def go():
        await _seed(config, [
            ProfileInput(name="v3-wrong", version="v3", username="sparkv3", auth_protocol="SHA",
                         auth_key="not-the-key", priv_protocol="AES",
                         priv_key="sparktest-priv", port="11161"),
        ], devices=("127.0.0.1", "127.0.0.2"))
        refused = await snmp_discover.run_discovery(config)
        async with D.session_scope() as s:
            await save_profile(s, vault_for(config),
                               ProfileInput(name="z-v2c", version="v2c", community="sparktest",
                                            port="11161"))
        found = await snmp_discover.run_discovery(config)
        await D.close_engine()
        return refused, found

    refused, found = asyncio.run(go())
    assert [r["address"] for r in refused["refused"]] == ["127.0.0.1"]
    assert refused["found"] == []
    assert [(f["address"], f["profile"]) for f in found["found"]] == [("127.0.0.1", "z-v2c")]
    assert found["found"][0]["sys_name"]
    assert found["seconds"] < 10
