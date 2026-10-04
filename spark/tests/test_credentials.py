"""Settings -> Credentials, and the TrueNAS API client under it.

Against a real TLS WebSocket server speaking TrueNAS's JSON-RPC (a
self-signed certificate made per test run), because the property that
matters most can only be tested for real: the API key is never sent until a
person has trusted the certificate, and never to a different one.
"""

from __future__ import annotations

import re

import asyncio
import datetime as dt
import json
import ssl
import tempfile
import threading
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from sqlalchemy import select
from websockets.asyncio.server import serve

from spark import credentials, truenas
from spark import db as D
from spark.config import Config
from spark.main import create_app
from spark.models import ApiCredential, Device
from spark.vault import vault_for

PASSWORD = "correct horse battery"
DENIED = object()
GOOD_KEY = "1-goodkeygoodkeygoodkey"


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def _certificate(tmp: Path, name: str) -> tuple[Path, Path, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=30))
            .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp / f"{name}.crt", tmp / f"{name}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_path, key_path, truenas.fingerprint_of(cert.public_bytes(serialization.Encoding.DER))


class FakeTrueNAS:
    """A TLS JSON-RPC server on 127.0.0.1, run on a thread of its own."""

    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="spark-tn-"))
        self.received: list[dict] = []
        self.paths: list[str] = []
        # method -> result, or DENIED; anything else is "Method not found".
        self.answers: dict = {}
        self.use("first")

    def use(self, name: str) -> None:
        """Serve a new certificate from now on (a renewal, or an impostor)."""
        cert, key, self.fingerprint = _certificate(self.tmp, name)
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(cert, key)

    async def _handler(self, ws):  # type: ignore[no-untyped-def]
        self.paths.append(ws.request.path)
        async for raw in ws:
            msg = json.loads(raw)
            self.received.append(msg)
            await ws.send(json.dumps({"jsonrpc": "2.0", "method": "collection_update", "params": {}}))
            if msg["method"] == "auth.login_with_api_key":
                result = msg["params"] == [GOOD_KEY]
            elif msg["method"] == "system.info":
                result = {"version": "25.10.1", "hostname": "truenas"}
            elif msg["method"] in self.answers and self.answers[msg["method"]] is not DENIED:
                result = self.answers[msg["method"]]
            elif msg["method"] in self.answers:
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {
                    "code": 13, "message": "Not authorized", "data": {"reason": "Not authorized"}}}))
                continue
            else:
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                                          "error": {"code": -32601, "message": "Method not found"}}))
                continue
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}))

    def start(self) -> None:
        ready = threading.Event()

        def serve_forever() -> None:
            async def main() -> None:
                # A fresh SSL context per connection, so use() takes effect.
                async with serve(self._handler, "127.0.0.1", 0,
                                 ssl=_Switch(self)) as server:
                    self.port = server.sockets[0].getsockname()[1]
                    ready.set()
                    await asyncio.Future()
            asyncio.run(main())
        threading.Thread(target=serve_forever, daemon=True).start()
        ready.wait(5)

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.port}"

    def logins(self) -> list[dict]:
        return [m for m in self.received if m["method"] == "auth.login_with_api_key"]


class _Switch(ssl.SSLContext):
    """An SSLContext whose certificate the test can swap mid-run."""

    def __new__(cls, fake):  # type: ignore[no-untyped-def]
        ctx = super().__new__(cls, ssl.PROTOCOL_TLS_SERVER)
        ctx.fake = fake
        ctx.sni_callback = lambda sock, name, c: setattr(sock, "context", fake.ctx)
        ctx.load_cert_chain(*_certificate(fake.tmp, "boot")[:2])
        return ctx


@pytest.fixture(scope="module")
def fake():
    server = FakeTrueNAS()
    server.start()
    return server


@pytest.fixture(autouse=True)
def _fresh(fake):
    fake.received.clear()
    fake.answers = {}
    fake.use("first")


# --------------------------------------------------------------------------
# The client
# --------------------------------------------------------------------------


class TestClientPinning:
    def test_no_pin_means_nothing_is_sent(self, fake):
        with pytest.raises(truenas.CertificateNotTrusted) as caught:
            run(truenas.test(fake.host, GOOD_KEY, None))
        assert caught.value.fingerprint == fake.fingerprint and not caught.value.changed
        assert fake.received == [], "not the key, not anything"

    def test_the_pinned_certificate_logs_in(self, fake):
        info = run(truenas.test(fake.host, GOOD_KEY, fake.fingerprint))
        assert (info.version, info.hostname) == ("25.10.1", "truenas")
        assert fake.paths[-1] == "/api/current"
        assert fake.logins() == [{"jsonrpc": "2.0", "id": 1, "method": "auth.login_with_api_key",
                                  "params": [GOOD_KEY]}]

    def test_a_different_certificate_gets_nothing(self, fake):
        trusted = fake.fingerprint
        fake.use("impostor")
        with pytest.raises(truenas.CertificateNotTrusted) as caught:
            run(truenas.test(fake.host, GOOD_KEY, trusted))
        assert caught.value.changed and caught.value.fingerprint == fake.fingerprint
        assert fake.logins() == []

    def test_a_refused_key(self, fake):
        with pytest.raises(truenas.LoginFailed, match="refused the API key"):
            run(truenas.test(fake.host, "1-wrong", fake.fingerprint))

    def test_nothing_listening(self):
        with pytest.raises(truenas.TrueNASError, match="Could not open the TrueNAS API"):
            run(truenas.test("127.0.0.1:9", GOOD_KEY, None))

    def test_only_ever_https(self):
        assert truenas.url_for("truenas") == "wss://truenas/api/current"
        source = Path(truenas.__file__).read_text()
        assert "ws://" not in source.replace("wss://", ""), "no plain-HTTP path at all"


# --------------------------------------------------------------------------
# Storing and testing a credential
# --------------------------------------------------------------------------


def _config() -> Config:
    tmp = Path(tempfile.mkdtemp(prefix="spark-creds-"))
    config = Config.model_validate({
        "app": {"data_dir": str(tmp / "data"), "log_level": "WARNING"},
        "network": {"subnets": []},
    })
    config.app.data_dir.mkdir(parents=True, exist_ok=True)
    return config


@pytest.fixture
def db():
    config = _config()

    async def setup():
        D.init_engine(config)
        await D.init_db(config)
        async with D.session_scope() as s:
            s.add(Device(mac="aa:00:00:00:00:01", primary_ip="172.16.10.17", friendly_name="truenas"))
            s.add(Device(mac="aa:00:00:00:00:02", primary_ip="172.16.10.99", ignored=True))
    run(setup())
    yield config
    run(D.close_engine())


class TestHosts:
    @pytest.mark.parametrize("raw, clean", [
        ("172.16.10.17", "172.16.10.17"), (" truenas.lan ", "truenas.lan"), ("truenas:8443", "truenas:8443"),
        ("[fe80::1]:443", "[fe80::1]:443"), ("fe80::1", "fe80::1"),
    ])
    def test_good(self, raw, clean):
        assert credentials.clean_host(raw) == clean

    @pytest.mark.parametrize("raw", ["https://truenas", "truenas/api", "truenas:0", "truenas:99999",
                                     "bad host", "", "[nope]:1", "a" * 300])
    def test_refused(self, raw):
        with pytest.raises(credentials.CredentialError):
            credentials.clean_host(raw)


async def _add(config, **over):  # type: ignore[no-untyped-def]
    async with D.session_scope() as s:
        args = {"kind": "truenas", "name": "truenas", "device_id": 1, "host": "", "api_key": GOOD_KEY,
                **over}
        return (await credentials.add(s, vault_for(config), **args)).id


async def _row(ident):  # type: ignore[no-untyped-def]
    async with D.session_scope() as s:
        return await s.get(ApiCredential, ident)


class TestCredentials:
    def test_sealed_and_defaulting_to_the_devices_address(self, db):
        row = run(_row(run(_add(db))))
        assert row.host == "172.16.10.17" and row.device_id == 1
        assert GOOD_KEY not in row.key_sealed and vault_for(db).open(row.key_sealed) == GOOD_KEY

    @pytest.mark.parametrize("over, message", [
        ({"name": " "}, "Give it a name."),
        ({"api_key": "  "}, "Paste the API key."),
        ({"kind": "ftp"}, "Choose what kind"),
        ({"device_id": 2}, "not on the list"),
        ({"device_id": None}, "Enter the address"),
    ])
    def test_refusals(self, db, over, message):
        with pytest.raises(credentials.CredentialError, match=message):
            run(_add(db, **over))

    def test_names_are_unique_whatever_the_case(self, db):
        run(_add(db))
        with pytest.raises(credentials.CredentialError, match="already a credential"):
            run(_add(db, name="TRUENAS"))

    def test_test_trust_test(self, db, fake):
        ident = run(_add(db, host=fake.host))

        async def step(fn, *args):  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                row = await s.get(ApiCredential, ident)
                return await fn(s, vault_for(db), row, *args)
        assert run(step(credentials.test)) is False
        row = run(_row(ident))
        assert row.pending_sha256 == fake.fingerprint and row.cert_sha256 is None
        assert fake.logins() == []

        with pytest.raises(credentials.CredentialError, match="no longer the one"):
            run(step(credentials.trust, "AA:BB"))
        assert run(step(credentials.trust, fake.fingerprint)) is True
        row = run(_row(ident))
        assert row.cert_sha256 == fake.fingerprint and row.pending_sha256 is None
        assert row.last_info == {"version": "25.10.1", "hostname": "truenas"} and row.last_error is None

        fake.use("renewed")
        assert run(step(credentials.test)) is False
        row = run(_row(ident))
        assert row.cert_sha256 != fake.fingerprint == row.pending_sha256
        assert "different certificate" in row.last_error
        assert len(fake.logins()) == 1, "the renewed certificate got nothing"

    def test_a_new_address_or_key_forgets_the_certificate(self, db):
        ident = run(_add(db))

        async def pin_then(**change):  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                row = await s.get(ApiCredential, ident)
                row.cert_sha256 = "AA"
            async with D.session_scope() as s:
                row = await s.get(ApiCredential, ident)
                await credentials.update(s, vault_for(db), row, **{
                    "name": "truenas", "device_id": 1, "host": "", "api_key": "", **change})

        run(pin_then())
        assert run(_row(ident)).cert_sha256 == "AA", "nothing changed: still trusted"
        run(pin_then(host="truenas.lan"))
        assert run(_row(ident)).cert_sha256 is None
        run(pin_then(host="truenas.lan", api_key="1-another"))      # same address, new key
        row = run(_row(ident))
        assert row.cert_sha256 is None and vault_for(db).open(row.key_sealed) == "1-another"


# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------


@pytest.fixture
def site(db):
    with TestClient(create_app(db), follow_redirects=False) as client:
        client.post("/setup", data={"setup_code": client.app.state.setup_code, "username": "admin", "password": PASSWORD,
                                    "password_confirm": PASSWORD, "timezone": "UTC"})
        yield client


def flat(html: str) -> str:
    return " ".join(html.split())


class TestPage:
    def test_the_tab(self, site):
        page = flat(site.get("/settings/credentials").text)
        # The menu entry is current; an icon sits between the link and its name.
        assert re.search(r'<a href="/settings/credentials" class="active" aria-current="page"> '
                         r'<span class="subnav-icon">.*?</span> <span>Credentials</span>', page)
        assert '<summary class="sub-title">Add a credential</summary>' in page
        assert 'name="api_key" maxlength="1024" required autocomplete="new-password"' in page

    def test_add_shows_the_certificate_then_trust_connects(self, site, fake):
        response = site.post("/settings/credentials", data={
            "kind": "truenas", "name": "truenas", "device_id": "1", "host": fake.host, "api_key": GOOD_KEY})
        assert response.status_code == 303 and response.headers["location"] == "/settings/credentials#cred-1"
        assert fake.logins() == [], "saving tested without sending the key"
        page = flat(site.get("/settings/credentials").text)
        assert f'<code class="fingerprint">{fake.fingerprint}</code>' in page
        assert "waiting for you" in page and GOOD_KEY not in page
        site.post("/settings/credentials/1/trust", data={"fingerprint": fake.fingerprint})
        page = flat(site.get("/settings/credentials").text)
        assert '<span class="pill ok dot">connected</span> TrueNAS 25.10.1 · truenas' in page
        assert GOOD_KEY not in page

    def test_a_bad_add_says_why_and_keeps_what_was_typed_but_the_key(self, site):
        response = site.post("/settings/credentials", data={
            "kind": "truenas", "name": "nas", "device_id": "", "host": "https://truenas", "api_key": GOOD_KEY})
        assert response.status_code == 400 and "no https://" in response.text
        assert 'value="https://truenas"' in response.text and GOOD_KEY not in response.text

    def test_remove(self, site):
        site.post("/settings/credentials", data={"kind": "truenas", "name": "x", "device_id": "",
                                                 "host": "127.0.0.1:9", "api_key": GOOD_KEY})
        site.post("/settings/credentials/1/delete")

        async def count():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                return len((await s.execute(select(ApiCredential))).all())
        assert run(count()) == 0


# --------------------------------------------------------------------------
# Checked every 5 minutes, and the alert
# --------------------------------------------------------------------------


async def _trusted(config, host, **over):  # type: ignore[no-untyped-def]
    ident = await _add(config, host=host, **over)
    async with D.session_scope() as s:
        row = await s.get(ApiCredential, ident)
        await credentials.test(s, vault_for(config), row)
    async with D.session_scope() as s:
        row = await s.get(ApiCredential, ident)
        assert await credentials.trust(s, vault_for(config), row, row.pending_sha256)
    return ident


async def _set_key(config, ident, key):  # type: ignore[no-untyped-def]
    async with D.session_scope() as s:
        (await s.get(ApiCredential, ident)).key_sealed = vault_for(config).seal(key)


async def _sent():  # type: ignore[no-untyped-def]
    from spark.models import Notification
    async with D.session_scope() as s:
        rows = (await s.execute(select(Notification).order_by(Notification.id))).scalars()
        return [(n.kind, n.subject, n.body) for n in rows]


async def _alert_state(ident):  # type: ignore[no-untyped-def]
    from spark.models import AlertState
    async with D.session_scope() as s:
        return await s.get(AlertState, f"api:{ident}")


class TestScheduled:
    def test_only_trusted_credentials_are_checked(self, db, fake):
        ident = run(_add(db, host=fake.host))
        fake.paths.clear()
        assert run(credentials.check_all(db)) == 0
        assert run(credentials.check_one(db, ident)) is False
        assert fake.paths == [], "an untrusted credential is never connected to on a schedule"

    def test_a_trusted_one_is_checked_and_recorded(self, db, fake):
        ident = run(_trusted(db, fake.host))
        before = run(_row(ident)).last_checked_at
        fake.received.clear()
        assert run(credentials.check_all(db)) == 1
        row = run(_row(ident))
        assert len(fake.logins()) == 1 and row.last_checked_at > before
        assert credentials.state(row) == "ok"

    def test_two_failures_alert_once_then_it_recovers(self, db, fake):
        ident = run(_trusted(db, fake.host))
        run(_set_key(db, ident, "1-revoked"))
        assert run(credentials.check_one(db, ident)) is False
        assert run(_sent()) == [], "one failed check is not news"
        run(credentials.check_one(db, ident))
        run(credentials.check_one(db, ident))
        sent = run(_sent())
        assert [(k, subj) for k, subj, _ in sent] == [
            ("api_down", "truenas: SPARK cannot use the TrueNAS API")]
        assert "refused the API key" in sent[0][2] and "1-revoked" not in sent[0][2]
        assert credentials.state(run(_row(ident))) == "bad"

        run(_set_key(db, ident, GOOD_KEY))
        assert run(credentials.check_one(db, ident)) is True
        assert [k for k, _, _ in run(_sent())] == ["api_down", "api_ok"]
        assert run(_sent())[1][1] == "truenas: the TrueNAS API is working again"
        assert run(_alert_state(ident)) is None

        async def incident():  # type: ignore[no-untyped-def]
            from spark.models import AlertIncident
            async with D.session_scope() as s:
                return (await s.execute(select(AlertIncident))).scalars().one()
        row = run(incident())
        assert (row.key, row.title, row.resolution) == (
            f"api:{ident}", "truenas: SPARK cannot use the TrueNAS API", "recovered")

    def test_one_failure_then_working_says_nothing(self, db, fake):
        ident = run(_trusted(db, fake.host))
        run(_set_key(db, ident, "1-revoked"))
        run(credentials.check_one(db, ident))
        run(_set_key(db, ident, GOOD_KEY))
        run(credentials.check_one(db, ident))
        run(_set_key(db, ident, "1-revoked"))
        run(credentials.check_one(db, ident))
        assert run(_sent()) == [], "the streak started again"

    def test_a_replaced_certificate_alerts_and_gets_nothing(self, db, fake):
        ident = run(_trusted(db, fake.host))
        fake.use("impostor")
        fake.received.clear()
        run(credentials.check_one(db, ident))
        run(credentials.check_one(db, ident))
        assert fake.logins() == []
        (kind, _, body), = run(_sent())
        assert kind == "api_down" and "different certificate" in body

    @pytest.mark.parametrize("how", ["rule off", "muted"])
    def test_quiet_when_the_rule_is_off_or_the_device_muted(self, db, fake, how):
        from spark import snmp_alerts
        from spark.db import save_setting
        from spark.web.routes_device_page import set_device_muted

        async def quiet():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                if how == "rule off":
                    await save_setting(s, snmp_alerts.SETTING,
                                       {**await snmp_alerts.load(s), "api_down": False})
                else:
                    await set_device_muted(s, 1, True)
        ident = run(_trusted(db, fake.host))
        run(quiet())
        run(_set_key(db, ident, "1-revoked"))
        for _ in range(3):
            run(credentials.check_one(db, ident))
        assert run(_sent()) == []

    def test_removing_or_changing_it_forgets_the_alert(self, db, fake, site):
        ident = run(_trusted(db, fake.host))
        run(_set_key(db, ident, "1-revoked"))
        run(credentials.check_one(db, ident))
        assert run(_alert_state(ident)) is not None
        run(credentials.check_one(db, ident))       # two failures: fired, incident open

        async def api_incident(ident):  # type: ignore[no-untyped-def]
            from spark.models import AlertIncident
            async with D.session_scope() as s:
                row = await s.scalar(select(AlertIncident).where(AlertIncident.key == f"api:{ident}"))
                return row and (row.closed_at is None, row.resolution)
        assert run(api_incident(ident)) == (True, None)
        site.post(f"/settings/credentials/{ident}", data={
            "name": "truenas", "device_id": "1", "host": "truenas.lan", "api_key": ""})
        assert run(_alert_state(ident)) is None and run(_row(ident)).cert_sha256 is None
        assert run(api_incident(ident)) == (False, "no longer watched")

        ident = run(_trusted(db, fake.host, name="second"))
        run(_set_key(db, ident, "1-revoked"))
        run(credentials.check_one(db, ident))
        assert run(_alert_state(ident)) is not None
        site.post(f"/settings/credentials/{ident}/delete")
        assert run(_alert_state(ident)) is None

    def test_the_rule_is_on_for_a_setup_that_saved_rules_before_it_existed(self, db, site):
        from spark import snmp_alerts
        from spark.db import save_setting

        async def old():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                await save_setting(s, snmp_alerts.SETTING, {"cpu": False, "cpu_percent": 70})
            async with D.session_scope() as s:
                return await snmp_alerts.load(s)
        rules = run(old())
        assert rules["api_down"] is True and rules["pool_health"] is True
        assert rules["cpu"] is False and rules["cpu_percent"] == 70
        page = flat(site.get("/settings/alerts").text)
        assert 'name="api_down" value="1" checked' in page
        assert 'name="pool_health" value="1" checked' in page

    def test_the_rules_form_saves_it(self, db, site):
        from spark import snmp_alerts
        form = {"port_busy_percent": "80", "port_busy_minutes": "10", "cpu_percent": "90",
                "cpu_minutes": "10", "memory_percent": "90", "memory_minutes": "10",
                "temperature_celsius": "80", "temperature_minutes": "5",
                "pool_space_percent": "85", "disk_space_percent": "90", "drive_celsius": "50",
                "drive_minutes": "10"}
        assert site.post("/settings/alerts/rules", data=form).status_code == 303

        async def rules():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                return await snmp_alerts.load(s)
        assert run(rules())["api_down"] is False
        site.post("/settings/alerts/rules", data={**form, "api_down": "1"})
        assert run(rules())["api_down"] is True


def test_the_job_is_scheduled_every_5_minutes(db):
    from datetime import timedelta

    from spark import scheduler as S
    from spark.models import utcnow

    async def body():  # type: ignore[no-untyped-def]
        S.start()
        try:
            assert S.schedule_credentials(db)
            job = S.get_scheduler().get_job(credentials.JOB_ID)
            assert 30 < (job.next_run_time - utcnow()).total_seconds() <= 45
            assert job.trigger.interval == timedelta(minutes=5)
        finally:
            await S.shutdown()
    run(body())


def test_the_app_schedules_it_on_start(site):
    from spark import scheduler as S
    assert S.get_scheduler().get_job(credentials.JOB_ID) is not None


# --------------------------------------------------------------------------
# The device page
# --------------------------------------------------------------------------


class TestDevicePage:
    def test_no_credential_no_card(self, site):
        assert "api-card" not in site.get("/devices/1").text

    def test_connected(self, site, fake, db):
        run(_trusted(db, fake.host))
        page = flat(site.get("/devices/1").text)
        assert '<section class="card api-card" id="api-1">' in page
        assert "<h2>TrueNAS API</h2>" in page
        assert '<span class="pill ok dot">connected</span> TrueNAS 25.10.1 · truenas ·' in page
        assert "every 5 minutes" in page and GOOD_KEY not in page

    def test_waiting_points_at_the_certificate(self, site, fake):
        site.post("/settings/credentials", data={
            "kind": "truenas", "name": "truenas", "device_id": "1", "host": fake.host, "api_key": GOOD_KEY})
        page = flat(site.get("/devices/1").text)
        assert '<span class="pill neutral">waiting for you</span>' in page
        assert '<a class="btn-quiet" href="/settings/credentials#cred-1">Check the certificate</a>' in page
        assert "/settings/credentials/1/test" not in page

    def test_not_connected_says_why(self, site, fake, db):
        ident = run(_trusted(db, fake.host))
        run(_set_key(db, ident, "1-revoked"))
        run(credentials.check_one(db, ident))
        page = flat(site.get("/devices/1").text)
        assert '<span class="pill bad">not connected</span> Last worked' in page
        assert "refused the API key" in page and "1-revoked" not in page

    def test_test_comes_back_to_the_device(self, site, fake, db):
        run(_trusted(db, fake.host))
        response = site.post("/settings/credentials/1/test", data={"back": "/devices/1?range=24h"})
        assert response.headers["location"] == "/devices/1?range=24h#api-1"
        for back in ("https://evil.example/devices/1", "//evil.example/", "/settings"):
            response = site.post("/settings/credentials/1/test", data={"back": back})
            assert response.headers["location"] == "/settings/credentials#cred-1"


def test_migration_15_from_a_version_14_database():
    from sqlalchemy import inspect, text

    async def shape(wind_back: bool):
        config = _config()
        D.init_engine(config)
        await D.init_db(config)
        if wind_back:
            async with D.session_scope() as s:
                await s.execute(text("DROP TABLE api_credential"))
                await D._set_version(s, 14)
            await D.close_engine()
            D.init_engine(config)
            await D.init_db(config)
        async with D.session_scope() as s:
            connection = await s.connection()
            columns = await connection.run_sync(
                lambda sync: sorted(c["name"] for c in inspect(sync).get_columns("api_credential")))
            version = await D._get_version(s)
        await D.close_engine()
        return columns, version

    assert run(shape(True)) == run(shape(False))
    assert run(shape(False))[1] == D.CURRENT_VERSION >= 15

