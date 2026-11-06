"""The first-run wizard (/welcome) and the open-item banners (onboarding.py).

What matters: a fresh install is asked, in order, and an upgrade is not;
each step saves the same rows Settings would and can be skipped; a failed
step saves nothing; a credential the wizard saved by address gets its device
from the sweep, and only those; and a dismissed banner stays dismissed until
the thing it is about changes.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from spark import credentials, exclusions, onboarding
from spark import db as D
from spark import scheduler as scheduler_module
from spark.config import Config
from spark.main import create_app
from spark.models import ApiCredential, Device, DeviceAddress, SnmpProfile, Subnet, utcnow
from spark.vault import vault_for
from spark.web import routes_welcome

PASSWORD = "correct horse battery"
GOOD_KEY = "1-goodkeygoodkeygoodkey"
HOOK = "https://discord.com/api/webhooks/1234567890/abcdefghijklmnop"


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def _config() -> Config:
    tmp = Path(tempfile.mkdtemp(prefix="spark-welcome-"))
    config = Config.model_validate({
        "app": {"data_dir": str(tmp / "data"), "log_level": "WARNING"},
        "network": {"subnets": []},
    })
    config.app.data_dir.mkdir(parents=True, exist_ok=True)
    return config


INTERFACES = [
    ("lo", "127.0.0.1", "255.0.0.0"),
    ("eth0", "172.16.10.7", "255.255.255.0"),
    ("eth0.30", "172.16.30.7", "255.255.255.0"),
    ("docker0", "172.17.0.1", "255.255.0.0"),
    ("tailscale0", "100.101.102.103", "255.192.0.0"),
    ("wg0", "10.99.0.1", "255.255.255.0"),
    ("br-1234", "172.18.0.1", "255.255.0.0"),
    ("eth1", "10.0.0.2", "255.255.0.0"),            # a /16: listed, too big to sweep
    ("eth2", "192.0.2.1", "255.255.255.255"),       # a /32
]


@pytest.fixture
def site(monkeypatch):
    monkeypatch.setattr(onboarding, "_read_interfaces", lambda: list(INTERFACES))
    # Nothing goes on the network from these tests.
    queued = {"sweep": 0, "snmp": 0}
    monkeypatch.setattr(scheduler_module, "trigger_discovery_now",
                        lambda config: queued.__setitem__("sweep", queued["sweep"] + 1) or True)
    monkeypatch.setattr(scheduler_module, "trigger_snmp_discovery",
                        lambda config: queued.__setitem__("snmp", queued["snmp"] + 1) or True)

    async def fake_test(session, vault, row):  # type: ignore[no-untyped-def]
        row.pending_sha256 = "AA:BB"
        row.last_checked_at, row.last_error = utcnow(), "Certificate not trusted yet."
        return False
    monkeypatch.setattr(routes_welcome.credentials, "test", fake_test)

    config = _config()
    with TestClient(create_app(config), follow_redirects=False) as client:
        answer = client.post("/setup", data={"setup_code": client.app.state.setup_code,
                                             "username": "admin", "password": PASSWORD,
                                             "password_confirm": PASSWORD, "timezone": "UTC"})
        client.setup_answer = answer
        client.queued = queued
        client.config = config
        yield client


def flat(html: str) -> str:
    return " ".join(html.split())


async def _all(model):  # type: ignore[no-untyped-def]
    async with D.session_scope() as s:
        return list((await s.execute(select(model))).scalars())


async def _state() -> dict:
    async with D.session_scope() as s:
        return await onboarding.load(s)


# --------------------------------------------------------------------------


class TestStart:
    def test_a_fresh_install_goes_to_the_wizard(self, site):
        assert site.setup_answer.status_code == 303
        assert site.setup_answer.headers["location"] == "/welcome"
        assert site.get("/welcome").headers["location"] == "/welcome/subnets"
        assert run(_state())["active"] is True

    def test_the_dashboard_says_setup_is_unfinished_but_the_wizard_does_not(self, site):
        assert "SPARK is not fully set up yet" in site.get("/").text
        assert "SPARK is not fully set up yet" not in site.get("/welcome/snmp").text

    def test_an_upgrade_never_sees_it(self):
        """No `onboarding` setting -- an install from before the wizard."""
        config = _config()

        async def go():  # type: ignore[no-untyped-def]
            D.init_engine(config)
            await D.init_db(config)
            async with D.session_scope() as s:
                return await onboarding.open_items(s)
        try:
            assert run(go()) == []
        finally:
            run(D.close_engine())

    def test_the_account_form_no_longer_asks_for_discord(self):
        config = _config()
        with TestClient(create_app(config), follow_redirects=False) as client:
            page = client.get("/setup").text
        assert "discord_webhook_url" not in page


class TestHostSubnets:
    def test_keeps_real_networks_and_drops_the_plumbing(self):
        found = {s.cidr: s for s in onboarding.host_subnets(INTERFACES)}
        assert set(found) == {"172.16.10.0/24", "172.16.30.0/24", "10.0.0.0/16"}
        assert found["172.16.30.0/24"].vlan == 30 and found["172.16.10.0/24"].vlan is None
        assert found["10.0.0.0/16"].too_large and not found["172.16.10.0/24"].too_large

    def test_sorted_by_address(self):
        cidrs = [s.cidr for s in onboarding.host_subnets(INTERFACES)]
        assert cidrs == ["10.0.0.0/16", "172.16.10.0/24", "172.16.30.0/24"]

    def test_reading_this_machine_never_raises(self):
        assert isinstance(onboarding.host_subnets(), list)


class TestSubnets:
    def test_prefilled_from_the_host(self, site):
        page = site.get("/welcome/subnets").text
        assert 'value="172.16.10.0/24"' in page and 'value="172.16.30.0/24"' in page
        assert "too large to sweep" in page
        assert "172.17.0.0" not in page

    def test_saves_rows_and_exclusions(self, site):
        answer = site.post("/welcome/subnets", data={
            "action": "next",
            "cidr": ["172.16.10.0/24", "172.16.30.0/24", "172.16.20.5/24", ""],
            "name": ["LAN", "Servers", "IoT", ""],
            "vlan": ["1", "30", "20", ""],
            "include": ["0", "2", "3"],          # the second unticked
            "attached": ["0"],
            "exclude": "172.16.10.25\n172.16.10.100-172.16.10.150\n",
        })
        assert answer.status_code == 303 and answer.headers["location"] == "/welcome/snmp"
        rows = {s.cidr: s for s in run(_all(Subnet))}
        assert set(rows) == {"172.16.10.0/24", "172.16.20.0/24"}
        assert rows["172.16.10.0/24"].attached and rows["172.16.10.0/24"].vlan == 1
        assert not rows["172.16.20.0/24"].attached and rows["172.16.20.0/24"].name == "IoT"

        async def excluded():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                return [e["spec"] for e in await exclusions.entries(s)]
        assert run(excluded()) == ["172.16.10.25", "172.16.10.100-172.16.10.150"]
        assert run(_state())["steps"]["subnets"] == "done"

    def test_one_bad_row_saves_nothing_and_keeps_what_was_typed(self, site):
        answer = site.post("/welcome/subnets", data={
            "action": "next", "cidr": ["172.16.10.0/24", "not-a-net"], "name": ["LAN", "Oops"],
            "vlan": ["", ""], "include": ["0", "1"], "attached": ["0", "1"], "exclude": ""})
        assert answer.status_code == 400 and "not-a-net" in answer.text
        assert 'value="Oops"' in answer.text
        assert run(_all(Subnet)) == []

    def test_a_bad_exclusion_saves_nothing(self, site):
        answer = site.post("/welcome/subnets", data={
            "action": "next", "cidr": ["172.16.10.0/24"], "name": [""], "vlan": [""],
            "include": ["0"], "attached": ["0"], "exclude": "banana"})
        assert answer.status_code == 400
        assert run(_all(Subnet)) == []

    def test_nothing_ticked_asks_for_one_or_skip(self, site):
        answer = site.post("/welcome/subnets", data={
            "action": "next", "cidr": ["172.16.10.0/24"], "name": [""], "vlan": [""], "exclude": ""})
        assert answer.status_code == 400 and "Skip for now" in answer.text

    def test_skip(self, site):
        answer = site.post("/welcome/subnets", data={"action": "skip"})
        assert answer.headers["location"] == "/welcome/snmp"
        assert run(_state())["steps"]["subnets"] == "skipped"
        assert run(_all(Subnet)) == []

    def test_an_existing_one_can_be_removed(self, site):
        site.post("/welcome/subnets", data={"action": "next", "cidr": ["192.168.1.0/24"], "name": [""],
                                             "vlan": [""], "include": ["0"], "attached": ["0"]})
        subnet = run(_all(Subnet))[0]
        page = site.get("/welcome/subnets").text
        assert "Already listed" in page
        site.post(f"/welcome/subnets/{subnet.id}/delete")
        assert run(_all(Subnet)) == []


class TestSnmp:
    def test_a_v2c_profile(self, site):
        answer = site.post("/welcome/snmp", data={"action": "next", "version": "v2c",
                                                  "community": "readonly-community"})
        assert answer.headers["location"] == "/welcome/integrations"
        (profile,) = run(_all(SnmpProfile))
        assert profile.name == "SNMP v2c" and profile.version == "v2c"
        assert "readonly-community" not in str(profile.community_sealed)

    def test_a_v3_profile_and_a_second_name(self, site):
        data = {"action": "next", "version": "v3", "username": "spark", "auth_protocol": "SHA",
                "auth_key": "authpassword", "priv_protocol": "AES", "priv_key": "privpassword"}
        site.post("/welcome/snmp", data=data)
        site.post("/welcome/snmp", data=data)
        names = sorted(p.name for p in run(_all(SnmpProfile)))
        assert names == ["SNMP v3", "SNMP v3 2"]

    def test_an_error_is_shown_and_secrets_are_not_echoed(self, site):
        answer = site.post("/welcome/snmp", data={"action": "next", "version": "v3", "username": "spark",
                                                  "auth_key": "short", "priv_key": "privpassword"})
        assert answer.status_code == 400
        assert "privpassword" not in answer.text
        assert run(_all(SnmpProfile)) == []

    def test_skip(self, site):
        site.post("/welcome/snmp", data={"action": "skip"})
        assert run(_state())["steps"]["snmp"] == "skipped"


class TestIntegrations:
    def test_chosen_without_a_key_becomes_a_banner(self, site):
        answer = site.post("/welcome/integrations", data={"action": "connect", "use": ["truenas", "unifi"]})
        assert answer.status_code == 303
        assert run(_state())["chosen"] == ["truenas", "unifi"]
        page = site.get("/").text
        assert "You chose TrueNAS during setup" in page and "You chose UniFi during setup" in page
        assert "/settings/credentials?add=truenas" in page

    def test_a_key_makes_a_credential_with_no_device_waiting_for_trust(self, site):
        site.post("/welcome/integrations", data={"action": "connect", "use": ["truenas"],
                                                 "truenas_host": "172.16.10.17", "truenas_key": GOOD_KEY})
        (row,) = run(_all(ApiCredential))
        assert row.device_id is None and row.host == "172.16.10.17" and row.name == "TrueNAS"
        assert row.options[onboarding.LINK_FLAG] is True
        assert GOOD_KEY not in row.key_sealed
        page = site.get("/welcome/integrations").text
        assert 'action="/welcome/integrations/%d/trust"' % row.id in page
        assert "AA:BB" in page
        # Chosen and added: no "chosen" banner; the certificate still to check is.
        home = site.get("/").text
        assert "You chose TrueNAS" not in home
        assert "TrueNAS “TrueNAS” needs its certificate checked" in home

    def test_a_proxmox_token(self, site):
        site.post("/welcome/integrations", data={
            "action": "connect", "use": ["proxmox"], "proxmox_host": "172.16.10.5",
            "proxmox_token_id": "spark@pve!monitor", "proxmox_secret": "0f1e2d3c-aaaa-bbbb-cccc-1234567890ab"})
        (row,) = run(_all(ApiCredential))
        assert row.kind == "proxmox"
        assert vault_for(site.config).open(row.key_sealed).startswith("spark@pve!monitor=")

    def test_a_key_without_an_address_is_refused(self, site):
        answer = site.post("/welcome/integrations", data={"action": "connect", "use": ["unifi"],
                                                          "unifi_key": "a-unifi-api-key-abcdefghij"})
        assert answer.status_code == 400 and "enter its address" in answer.text
        assert "a-unifi-api-key" not in answer.text
        assert run(_all(ApiCredential)) == []

    def test_a_name_already_taken_gets_a_number(self, site):
        for _ in range(2):
            site.post("/welcome/integrations", data={"action": "connect", "use": ["truenas"],
                                                     "truenas_host": "172.16.10.17", "truenas_key": GOOD_KEY})
        assert sorted(r.name for r in run(_all(ApiCredential))) == ["TrueNAS", "TrueNAS 2"]

    def test_remove(self, site):
        site.post("/welcome/integrations", data={"action": "connect", "use": ["truenas"],
                                                 "truenas_host": "172.16.10.17", "truenas_key": GOOD_KEY})
        (row,) = run(_all(ApiCredential))
        site.post(f"/welcome/integrations/{row.id}/delete")
        assert run(_all(ApiCredential)) == []

    def test_continue_and_skip(self, site):
        assert site.post("/welcome/integrations", data={"action": "next"}).headers["location"] == "/welcome/alerts"
        site.post("/welcome/integrations", data={"action": "skip"})
        assert run(_state())["steps"]["integrations"] == "skipped"


class TestAlerts:
    def test_a_webhook_is_sealed(self, site):
        answer = site.post("/welcome/alerts", data={"action": "next", "webhook": HOOK})
        assert answer.headers["location"] == "/welcome/finish"
        assert run(_state())["steps"]["alerts"] == "done"
        assert "saved" in site.get("/welcome/alerts").text

    def test_not_discord_is_refused(self, site):
        answer = site.post("/welcome/alerts", data={"action": "next", "webhook": "https://example.com/x"})
        assert answer.status_code == 400

    def test_nothing_given_is_skipped(self, site):
        site.post("/welcome/alerts", data={"action": "next", "webhook": ""})
        assert run(_state())["steps"]["alerts"] == "skipped"


class TestFinish:
    def _subnet_and_profile(self, site):  # type: ignore[no-untyped-def]
        site.post("/welcome/subnets", data={"action": "next", "cidr": ["172.16.10.0/24"], "name": [""],
                                             "vlan": [""], "include": ["0"], "attached": ["0"]})
        site.post("/welcome/snmp", data={"action": "next", "version": "v2c", "community": "readonly-c"})

    def test_starts_the_first_scan_and_ends_the_setup_banner(self, site):
        self._subnet_and_profile(site)
        assert "look for devices that answer SNMP" in site.get("/welcome/finish").text
        answer = site.post("/welcome/finish", data={"find_snmp": "1"})
        assert answer.headers["location"] == "/devices"
        assert site.queued["sweep"] == 1 and site.queued["snmp"] == 0   # SNMP waits for the sweep
        state = run(_state())
        assert state["done"] and state["find_snmp_after_sweep"] is True
        assert "SPARK is not fully set up yet" not in site.get("/").text

    def test_find_snmp_is_off_unless_asked(self, site):
        self._subnet_and_profile(site)
        site.post("/welcome/finish", data={})
        assert run(_state())["find_snmp_after_sweep"] is False

    def test_without_subnets_there_is_no_scan(self, site):
        answer = site.post("/welcome/finish", data={"find_snmp": "1"})
        assert answer.headers["location"] == "/"
        assert site.queued["sweep"] == 0 and run(_state())["find_snmp_after_sweep"] is False


# --------------------------------------------------------------------------
# After the sweep: linking, and Find SNMP once
# --------------------------------------------------------------------------


@pytest.fixture
def db():
    config = _config()

    async def setup():  # type: ignore[no-untyped-def]
        D.init_engine(config)
        await D.init_db(config)
    run(setup())
    yield config
    run(D.close_engine())


async def _cred(config, host, *, flagged=True, name="TrueNAS"):  # type: ignore[no-untyped-def]
    async with D.session_scope() as s:
        row = await credentials.add(s, vault_for(config), kind="truenas", name=name,
                                    device_id=None, host=host, api_key=GOOD_KEY)
        if flagged:
            row.options = {onboarding.LINK_FLAG: True}
        return row.id


async def _devices(*rows):  # type: ignore[no-untyped-def]
    async with D.session_scope() as s:
        for row in rows:
            s.add(row)


async def _get(ident):  # type: ignore[no-untyped-def]
    async with D.session_scope() as s:
        return await s.get(ApiCredential, ident)


async def _after_sweep():  # type: ignore[no-untyped-def]
    async with D.session_scope() as s:
        return await onboarding.after_sweep(s)


class TestLinking:
    def test_the_device_at_the_address_is_given_and_the_flag_dropped(self, db):
        run(_devices(Device(mac="aa:00:00:00:00:01", primary_ip="172.16.10.17")))
        ident = run(_cred(db, "172.16.10.17:443"))
        run(_after_sweep())
        row = run(_get(ident))
        assert row.device_id == 1 and onboarding.LINK_FLAG not in (row.options or {})

    def test_by_another_address_of_the_device(self, db):
        run(_devices(Device(mac="aa:00:00:00:00:01", primary_ip="172.16.10.1")))
        run(_devices(DeviceAddress(device_id=1, ip="172.16.30.1")))
        ident = run(_cred(db, "172.16.30.1"))
        run(_after_sweep())
        assert run(_get(ident)).device_id == 1

    def test_by_host_name(self, db):
        run(_devices(Device(mac="aa:00:00:00:00:01", primary_ip="172.16.10.17", hostname="nas.lan")))
        ident = run(_cred(db, "nas.lan"))
        run(_after_sweep())
        assert run(_get(ident)).device_id == 1

    def test_nothing_there_yet_keeps_trying(self, db):
        ident = run(_cred(db, "172.16.10.17"))
        run(_after_sweep())
        assert run(_get(ident)).options[onboarding.LINK_FLAG] is True
        run(_devices(Device(mac="aa:00:00:00:00:01", primary_ip="172.16.10.17")))
        run(_after_sweep())
        assert run(_get(ident)).device_id == 1

    def test_an_ignored_device_is_not_given(self, db):
        run(_devices(Device(mac="aa:00:00:00:00:01", primary_ip="172.16.10.17", ignored=True)))
        ident = run(_cred(db, "172.16.10.17"))
        run(_after_sweep())
        assert run(_get(ident)).device_id is None

    def test_one_left_without_a_device_in_settings_is_left_alone(self, db):
        run(_devices(Device(mac="aa:00:00:00:00:01", primary_ip="172.16.10.17")))
        ident = run(_cred(db, "172.16.10.17", flagged=False))
        run(_after_sweep())
        assert run(_get(ident)).device_id is None

    def test_find_snmp_is_asked_for_once(self, db):
        async def ask():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                await onboarding.start(s)
                await onboarding.finish(s, find_snmp=True)
        run(ask())
        assert run(_after_sweep()) is True
        assert run(_after_sweep()) is False


# --------------------------------------------------------------------------
# Banners
# --------------------------------------------------------------------------


async def _fail(ident, *, ok_before=None):  # type: ignore[no-untyped-def]
    async with D.session_scope() as s:
        row = await s.get(ApiCredential, ident)
        row.cert_sha256 = "AA:BB"
        row.last_ok_at = ok_before
        row.last_checked_at, row.last_error = utcnow(), "Connection refused"


async def _recover(ident):  # type: ignore[no-untyped-def]
    async with D.session_scope() as s:
        row = await s.get(ApiCredential, ident)
        row.last_ok_at, row.last_error = utcnow(), None


async def _shown() -> list[str]:
    async with D.session_scope() as s:
        return [b["key"] for b in await onboarding.banners(s)]


async def _dismiss(key) -> bool:  # type: ignore[no-untyped-def]
    async with D.session_scope() as s:
        sig = {i["key"]: i["sig"] for i in await onboarding.open_items(s)}[key]
        return await onboarding.dismiss(s, key, sig)


class TestBanners:
    def test_a_failing_credential(self, db):
        ident = run(_cred(db, "172.16.10.17", flagged=False))
        assert run(_shown()) == []                     # untested is not news
        run(_fail(ident))
        assert run(_shown()) == [f"cred:{ident}"]

    def test_one_that_worked_and_stopped_is_an_incident_not_a_banner(self, db):
        """The API alert rule and Firing now have it; a banner would say it twice."""
        ident = run(_cred(db, "172.16.10.17", flagged=False))
        run(_fail(ident, ok_before=utcnow()))
        assert run(_shown()) == []

    def test_gone_once_it_connects_and_not_back_when_it_fails_later(self, db):
        ident = run(_cred(db, "172.16.10.17", flagged=False))
        run(_fail(ident))
        assert run(_shown()) == [f"cred:{ident}"]
        run(_recover(ident))
        assert run(_shown()) == []

        async def fail_again():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                (await s.get(ApiCredential, ident)).last_error = "Connection refused"
        run(fail_again())
        assert run(_shown()) == []

    def test_a_certificate_still_to_check(self, db):
        ident = run(_cred(db, "172.16.10.17", flagged=False))

        async def waiting():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                row = await s.get(ApiCredential, ident)
                row.pending_sha256 = "AA:BB"
                row.last_checked_at, row.last_error = utcnow(), "Certificate not trusted yet."

        async def items():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                return await onboarding.open_items(s)
        run(waiting())
        (item,) = run(items())
        assert item["tone"] == "info" and "needs its certificate checked" in item["title"]

    def test_dismissed_until_its_state_changes(self, db):
        ident = run(_cred(db, "172.16.10.17", flagged=False))
        run(_fail(ident))
        assert run(_dismiss(f"cred:{ident}")) is True
        assert run(_shown()) == []
        run(_fail(ident))                              # still failing: still hidden
        assert run(_shown()) == []

        async def new_certificate():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                row = await s.get(ApiCredential, ident)
                row.cert_sha256, row.pending_sha256 = None, "CC:DD"
        run(new_certificate())                         # now waiting: a different to-do
        assert run(_shown()) == [f"cred:{ident}"]

    def test_a_disabled_credential_is_not_news(self, db):
        ident = run(_cred(db, "172.16.10.17", flagged=False))
        run(_fail(ident))
        async def off():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                (await s.get(ApiCredential, ident)).enabled = False
        run(off())
        assert run(_shown()) == []

    def test_only_the_present_signature_dismisses(self, db):
        ident = run(_cred(db, "172.16.10.17", flagged=False))
        run(_fail(ident))

        async def stale():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                return await onboarding.dismiss(s, f"cred:{ident}", "2020-01-01T00:00:00")
        assert run(stale()) is False
        assert run(_shown()) == [f"cred:{ident}"]

    def test_dismiss_from_the_page(self, site):
        site.post("/welcome/integrations", data={"action": "connect", "use": ["unifi"]})
        page = site.get("/").text
        assert "You chose UniFi during setup" in page
        answer = site.post("/banners/dismiss", data={"key": "chosen:unifi", "sig": "1", "next": "/targets"})
        assert answer.headers["location"] == "/targets"
        assert "You chose UniFi during setup" not in site.get("/").text
        assert "SPARK is not fully set up yet" in site.get("/").text   # the others stay

    def test_dismiss_does_not_redirect_off_site(self, site):
        answer = site.post("/banners/dismiss", data={"key": "setup", "sig": "1",
                                                     "next": "https://evil.example.com"})
        assert answer.headers["location"] == "/"

    def test_the_text_is_escaped(self, db):
        ident = run(_cred(db, "172.16.10.17", flagged=False, name="<b>nas</b>"))
        run(_fail(ident))
        config = db
        run(D.close_engine())
        with TestClient(create_app(config), follow_redirects=False) as client:
            client.post("/setup", data={"setup_code": client.app.state.setup_code, "username": "admin",
                                        "password": PASSWORD, "password_confirm": PASSWORD})
            page = client.get("/").text
        assert "<b>nas</b>" not in page and "&lt;b&gt;nas&lt;/b&gt;" in page


class TestBannerLayout:
    def test_the_reason_is_the_readable_part(self):
        error = ("Could not open the TrueNAS API at wss://172.16.10.18/api/current: "
                 "[Errno 113] No route to host")
        assert onboarding._reason(error) == "No route to host"
        assert onboarding._reason("Certificate changed") == "Certificate changed"
        assert onboarding._reason(None) == ""

    def test_a_failing_credential_reads_as_title_and_detail(self, db):
        ident = run(_cred(db, "172.16.10.18", flagged=False, name="nas"))

        async def fail():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                row = await s.get(ApiCredential, ident)
                row.last_checked_at = utcnow()
                row.last_error = ("Could not open the TrueNAS API at wss://172.16.10.18/api/current: "
                                  "[Errno 113] No route to host")

        async def items():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                return await onboarding.open_items(s)
        run(fail())
        (item,) = run(items())
        assert item["title"] == "TrueNAS “nas” has never connected"
        assert item["detail"] == "No route to host · 172.16.10.18"

    def test_on_the_dashboard_between_the_greeting_and_the_tiles(self, site):
        page = site.get("/").text
        greeting, banners, tiles = (page.index('class="greeting"'), page.index('class="banner-stack"'),
                                    page.index('class="stat-row"'))
        assert greeting < banners < tiles

    def test_under_the_heading_on_other_pages(self, site):
        for path in ("/devices", "/targets", "/settings", "/alerts"):
            page = site.get(path).text
            assert page.index('class="page-head') < page.index('class="banner-stack"'), path

    def test_every_page_with_a_heading_shows_them(self):
        """So a page added later cannot quietly leave them out."""
        templates = Path(__file__).resolve().parents[1] / "src" / "spark" / "templates"
        missing = [f.name for f in templates.glob("*.html")
                   if ('class="page-head' in f.read_text() or 'class="greeting"' in f.read_text())
                   and '{% include "_banners.html" %}' not in f.read_text()]
        assert missing == []
