"""TLS of SPARK's own (review finding #25): the self-signed certificate, the
config that chooses it, and -- under a real uvicorn -- the cookie and headers
that follow from serving HTTPS."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from spark import tls
from spark.config import Config
from spark.main import create_app, tls_arguments

PASSWORD = "correct horse battery"


def _config(tmp: Path, **app) -> Config:  # type: ignore[no-untyped-def]
    config = Config.model_validate(
        {"app": {"data_dir": str(tmp / "data"), "log_level": "WARNING", **app},
         "network": {"subnets": []}}
    )
    config.app.data_dir.mkdir(parents=True, exist_ok=True)
    return config


class TestCertificate:
    def test_a_pair_is_made_once_and_kept(self, tmp_path):
        cert, key, made = tls.ensure_self_signed(tmp_path / "tls", "SPARK")
        assert made and cert.exists() and key.exists()
        assert stat.S_IMODE(key.stat().st_mode) == 0o600
        assert stat.S_IMODE(cert.stat().st_mode) == 0o600
        first = cert.read_bytes()
        cert2, key2, made2 = tls.ensure_self_signed(tmp_path / "tls", "SPARK")
        assert not made2 and cert2.read_bytes() == first

    def test_a_lone_key_or_certificate_is_replaced_as_a_pair(self, tmp_path):
        cert, key, _ = tls.ensure_self_signed(tmp_path / "tls", "SPARK")
        before = cert.read_bytes()
        key.unlink()
        cert2, key2, made = tls.ensure_self_signed(tmp_path / "tls", "SPARK")
        assert made and key2.exists() and cert2.read_bytes() != before

    def test_what_the_certificate_says(self):
        from cryptography import x509

        cert_pem, key_pem = tls.make_self_signed(
            "Home Lab", [ipaddress.ip_address("192.168.1.10"), ipaddress.ip_address("127.0.0.1")])
        cert = x509.load_pem_x509_certificate(cert_pem)
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        assert san.get_values_for_type(x509.DNSName) == ["localhost"]      # "Home Lab" is not a host name
        assert [str(a) for a in san.get_values_for_type(x509.IPAddress)] == ["192.168.1.10", "127.0.0.1"]
        assert cert.subject == cert.issuer
        assert (cert.not_valid_after_utc - cert.not_valid_before_utc).days >= tls.VALID_DAYS
        assert b"BEGIN PRIVATE KEY" in key_pem

        cert_pem, _ = tls.make_self_signed("spark", [])
        cert = x509.load_pem_x509_certificate(cert_pem)
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        assert san.get_values_for_type(x509.DNSName) == ["localhost", "spark"]

    def test_the_fingerprint_is_what_browsers_show(self):
        cert_pem, _ = tls.make_self_signed("SPARK", [])
        shown = tls.fingerprint(cert_pem)
        assert re.fullmatch(r"([0-9A-F]{2}:){31}[0-9A-F]{2}", shown)
        # openssl agrees, if it is there to ask.
        try:
            out = subprocess.run(["openssl", "x509", "-noout", "-fingerprint", "-sha256"],
                                 input=cert_pem, capture_output=True, check=True).stdout.decode()
        except (FileNotFoundError, subprocess.CalledProcessError):
            pytest.skip("no openssl")
        assert out.strip().split("=")[-1] == shown

    def test_local_addresses_end_with_loopback_and_never_repeat(self):
        found = tls.local_addresses()
        assert found[-2:] == [ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("::1")]
        assert len(found) == len(set(found))
        assert not any(a.is_loopback for a in found[:-2])


class TestConfig:
    @pytest.mark.parametrize("raw, mode, scheme", [
        ({}, "auto", "https"), ({"tls": "auto"}, "auto", "https"),
        ({"tls": "off"}, "off", "http"), ({"tls": False}, "off", "http"),
        ({"tls": {"cert": "/c", "key": "/k"}}, "custom", "https"),
    ])
    def test_every_spelling(self, tmp_path, raw, mode, scheme):
        config = _config(tmp_path, **raw)
        assert config.app.tls.mode == mode and config.app.scheme == scheme

    @pytest.mark.parametrize("raw", [{"tls": "maybe"}, {"tls": {"cert": "/c"}},
                                     {"tls": {"mode": "custom"}}])
    def test_nonsense_is_refused(self, tmp_path, raw):
        with pytest.raises(ValueError):
            _config(tmp_path, **raw)

    def test_the_environment_can_turn_it_off(self, monkeypatch, tmp_path):
        from spark.config import load_config

        monkeypatch.delenv("SPARK_CONFIG", raising=False)
        monkeypatch.setenv("SPARK__APP__DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("SPARK__APP__TLS", "off")
        assert load_config().app.tls.mode == "off"

    def test_uvicorn_arguments_per_mode(self, tmp_path, caplog):
        assert tls_arguments(_config(tmp_path, tls="off")) == {}
        with caplog.at_level("WARNING", logger="spark.tls"):
            args = tls_arguments(_config(tmp_path))
        assert set(args) == {"ssl_certfile", "ssl_keyfile"}
        assert Path(args["ssl_certfile"]).parent == tmp_path / "data" / "tls"
        assert "fingerprint" in caplog.text and "https://" in caplog.text
        with pytest.raises(SystemExit):
            tls_arguments(_config(tmp_path, tls={"cert": str(tmp_path / "no.pem"),
                                                 "key": str(tmp_path / "no.key")}))
        cert, key, _ = tls.ensure_self_signed(tmp_path / "own", "x")
        assert tls_arguments(_config(tmp_path, tls={"cert": str(cert), "key": str(key)})) == {
            "ssl_certfile": str(cert), "ssl_keyfile": str(key)}


# --------------------------------------------------------------------------
# Under a real uvicorn, over HTTPS
# --------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Https:
    def __init__(self, config: Config) -> None:
        self.port = _free_port()
        self.base = f"https://127.0.0.1:{self.port}"
        self.app = create_app(config)
        self.server = uvicorn.Server(uvicorn.Config(
            self.app, host="127.0.0.1", port=self.port, log_level="warning",
            proxy_headers=False, log_config=None, **tls_arguments(config),
        ))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.client = httpx.Client(verify=False, follow_redirects=False)

    def __enter__(self) -> "Https":
        self.thread.start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                if self.client.get(self.base + "/healthz", timeout=0.5).status_code == 200:
                    return self
            except httpx.HTTPError:
                time.sleep(0.05)
        raise RuntimeError("uvicorn did not come up over TLS")

    def __exit__(self, *exc) -> None:  # type: ignore[no-untyped-def]
        self.client.close()
        self.server.should_exit = True
        self.thread.join(timeout=10)


class TestServedOverTls:
    def test_the_cookie_is_secure_and_host_prefixed_and_plain_http_is_refused(self):
        tmp = Path(tempfile.mkdtemp(prefix="spark-tls-"))
        config = _config(tmp)
        with Https(config) as live:
            code = live.app.state.setup_code
            response = live.client.post(live.base + "/setup", data={
                "setup_code": code, "username": "admin", "password": PASSWORD,
                "password_confirm": PASSWORD})
            assert response.status_code == 303
            cookie = response.headers["set-cookie"]
            assert cookie.startswith("__Host-spark_session=")
            assert "secure" in cookie.lower() and "httponly" in cookie.lower()
            assert "; path=/" in cookie.lower()
            assert "domain" not in cookie.lower()
            # The session works, under that name.
            assert live.client.get(live.base + "/").status_code == 200
            # Self-signed: no HSTS, so a person can still click through the warning.
            assert "strict-transport-security" not in live.client.get(live.base + "/").headers
            # Plain HTTP on the same port is not served.
            with pytest.raises(httpx.HTTPError):
                httpx.get(f"http://127.0.0.1:{live.port}/healthz", timeout=2)
            # Sign out clears the prefixed cookie.
            out = live.client.post(live.base + "/logout")
            assert "__Host-spark_session=" in out.headers.get("set-cookie", "")
            assert live.client.get(live.base + "/").status_code == 303

    def test_the_certificate_served_is_the_one_in_the_log(self):
        import ssl

        tmp = Path(tempfile.mkdtemp(prefix="spark-tls-"))
        config = _config(tmp)
        with Https(config) as live:
            on_disk = tls.fingerprint((config.app.tls_dir / "cert.pem").read_bytes())
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with socket.create_connection(("127.0.0.1", live.port), timeout=5) as raw:
                with ctx.wrap_socket(raw, server_hostname="127.0.0.1") as tls_sock:
                    der = tls_sock.getpeercert(binary_form=True)
        import hashlib

        served = ":".join(f"{b:02X}" for b in hashlib.sha256(der).digest())
        assert served == on_disk

    def test_the_healthcheck_script_passes_over_tls_and_over_http(self):
        script = Path(__file__).resolve().parents[1] / "docker" / "healthcheck.py"
        tmp = Path(tempfile.mkdtemp(prefix="spark-tls-"))
        with Https(_config(tmp)) as live:
            result = subprocess.run([sys.executable, str(script)], capture_output=True,
                                    env={"SPARK__APP__PORT": str(live.port)}, timeout=30)
            assert result.returncode == 0, result.stderr
        from tests.test_live_server import Live

        with Live(_config(Path(tempfile.mkdtemp(prefix="spark-tls-")), tls="off")) as plain:
            result = subprocess.run([sys.executable, str(script)], capture_output=True,
                                    env={"SPARK__APP__PORT": str(plain.port)}, timeout=30)
            assert result.returncode == 0, result.stderr
        result = subprocess.run([sys.executable, str(script)], capture_output=True,
                                env={"SPARK__APP__PORT": str(_free_port())}, timeout=30)
        assert result.returncode == 1
