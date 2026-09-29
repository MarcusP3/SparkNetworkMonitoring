"""Settings -> Credentials, and the TrueNAS API client under it.

Against a real TLS WebSocket server speaking TrueNAS's JSON-RPC (a
self-signed certificate made per test run), because the property that
matters most can only be tested for real: the API key is never sent until a
person has trusted the certificate, and never to a different one.
"""

from __future__ import annotations

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
        client.post("/setup", data={"username": "admin", "password": PASSWORD,
                                    "password_confirm": PASSWORD, "timezone": "UTC"})
        yield client


def flat(html: str) -> str:
    return " ".join(html.split())


class TestPage:
    def test_the_tab(self, site):
        page = flat(site.get("/settings/credentials").text)
        assert '<a href="/settings/credentials" class="active" aria-current="page"> <span>Credentials</span>' in page
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

