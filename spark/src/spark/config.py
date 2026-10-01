"""Configuration loading for SPARK.

Config comes from three places, later ones winning:

  1. Defaults defined here
  2. A YAML file (path from SPARK_CONFIG, default /config/spark.yaml)
  3. Environment variables prefixed SPARK__, using __ to nest
     (e.g. SPARK__APP__PORT=9800, SPARK__AUTH__MODE=proxy)

Anything a user is expected to change routinely lives in the database and is
edited in the UI instead. This file is for things needed *before* the database
exists: where the data lives, what port to bind, how to authenticate.
"""

from __future__ import annotations

import ipaddress
import os
import secrets
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator

DEFAULT_CONFIG_PATH = Path(os.environ.get("SPARK_CONFIG", "/config/spark.yaml"))
ENV_PREFIX = "SPARK__"


class TlsConfig(BaseModel):
    """How SPARK serves HTTPS.

      auto    - (default) a self-signed certificate SPARK makes on first start
                and keeps in data/tls/; its fingerprint is in the log, the
                way SPARK asks people to check TrueNAS's and Proxmox's.
      off     - plain HTTP, for a reverse proxy that terminates TLS in front.
      cert +  - an operator's own certificate and key (a real CA, or one
      key       their devices already trust). HSTS is sent only in this mode.

    In YAML either the word or a mapping: `tls: off`, `tls: auto`, or
    `tls: {cert: /config/tls/cert.pem, key: /config/tls/key.pem}`. From the
    environment: `SPARK__APP__TLS=off`.
    """

    mode: Literal["auto", "off", "custom"] = "auto"
    cert: Path | None = None
    key: Path | None = None

    @model_validator(mode="before")
    @classmethod
    def _from_word(cls, value: Any) -> Any:
        if value is None:
            return {}
        if isinstance(value, bool):          # YAML reads a bare `off` as False
            return {"mode": "auto" if value else "off"}
        if isinstance(value, str):
            word = value.strip().lower()
            if word in ("auto", "off"):
                return {"mode": word}
            raise ValueError("app.tls must be 'auto', 'off', or a mapping with cert and key")
        return value

    @model_validator(mode="after")
    def _cert_and_key_together(self) -> "TlsConfig":
        if bool(self.cert) != bool(self.key):
            raise ValueError("app.tls needs both cert and key, or neither")
        if self.cert and self.key:
            self.mode = "custom"
        elif self.mode == "custom":
            raise ValueError("app.tls mode 'custom' needs cert and key")
        return self

    @property
    def enabled(self) -> bool:
        return self.mode != "off"


class AppConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 9700
    data_dir: Path = Path("/data")
    tls: TlsConfig = Field(default_factory=TlsConfig)
    # Cosmetic only; used in page titles and Discord messages so friends running
    # their own copy can tell instances apart.
    instance_name: str = "SPARK"
    log_level: str = "INFO"
    # Names and addresses SPARK answers as. Empty means any (the default);
    # set it -- every name and address you type into a browser to reach
    # SPARK, and a reverse proxy's public name -- and any other Host header
    # gets a 421. Loopback is always allowed for the healthcheck. The defence
    # against DNS rebinding; see web/hardening.py.
    allowed_hosts: list[str] = Field(default_factory=list)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "spark.db"

    @property
    def secret_key_path(self) -> Path:
        return self.data_dir / "secret.key"

    @property
    def tls_dir(self) -> Path:
        return self.data_dir / "tls"

    @property
    def scheme(self) -> str:
        return "https" if self.tls.enabled else "http"


class ProxyAuthConfig(BaseModel):
    """Trusted-header auth, for running behind Authelia / Tailscale / CF Access.

    The allowlist is not optional. A trusted-header setup that accepts the
    header from any source IP can be spoofed by anything on the LAN, which is
    strictly worse than having no auth at all, because it looks secure.
    """

    header: str = "Remote-User"
    trusted_proxies: list[str] = Field(default_factory=list)

    @field_validator("trusted_proxies")
    @classmethod
    def _validate_proxies(cls, v: list[str]) -> list[str]:
        for entry in v:
            # Accepts both bare addresses and CIDRs.
            ipaddress.ip_network(entry, strict=False)
        return v


class AuthConfig(BaseModel):
    mode: Literal["password", "proxy"] = "password"
    session_days: int = 30
    # Failed logins allowed per IP per window before lockout.
    max_attempts: int = 10
    lockout_minutes: int = 15
    proxy: ProxyAuthConfig = Field(default_factory=ProxyAuthConfig)

    def validate_for_use(self) -> None:
        if self.mode == "proxy" and not self.proxy.trusted_proxies:
            raise ValueError(
                "auth.mode is 'proxy' but auth.proxy.trusted_proxies is empty. "
                "Refusing to start: without a source allowlist any host on the "
                "network could forge the identity header."
            )


class SubnetConfig(BaseModel):
    """One network segment for SPARK to discover on.

    `attached` records whether the SPARK host has an interface directly on this
    segment. It matters more than it looks: ARP only works on directly-attached
    L2 segments, so on a routed VLAN we can ping hosts but cannot learn their
    MAC addresses. Device identity keys on MAC, so remote VLANs fall back to
    IP-based identity until SNMP collection (Phase 3) can read the ARP table off
    the switch and give us MAC-to-IP for every VLAN at once.
    """

    cidr: str
    name: str = ""
    vlan: int | None = None
    attached: bool = True
    enabled: bool = True

    @field_validator("cidr")
    @classmethod
    def _validate_cidr(cls, v: str) -> str:
        ipaddress.ip_network(v, strict=False)
        return v

    @property
    def network(self) -> ipaddress.IPv4Network | ipaddress.IPv6Network:
        return ipaddress.ip_network(self.cidr, strict=False)

    @property
    def label(self) -> str:
        return self.name or self.cidr


class NetworkConfig(BaseModel):
    subnets: list[SubnetConfig] = Field(default_factory=list)


class Config(BaseModel):
    app: AppConfig = Field(default_factory=AppConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)

    # Read once per process. The vault asks for the key on every credential
    # it seals or opens, and each ask was a file read; the key does not change
    # while SPARK is running, and if the file is replaced underneath a running
    # instance the stored ciphertext is unreadable either way.
    _secret_key: str | None = PrivateAttr(default=None)

    def secret_key(self) -> str:
        if self._secret_key is None:
            self._secret_key = self._load_or_create_secret_key()
        return self._secret_key

    def _load_or_create_secret_key(self) -> str:
        """The install's root secret, generated once and persisted.

        The key the stored SNMP credentials are encrypted under is derived from
        this (see `vault.py`). Kept in a file rather than the database so that a
        copied or restored database is useless on its own: it neither
        resurrects old sessions nor decrypts a single credential.

        Created with 0600 from the first byte. It used to be written with the
        default mode and chmod-ed afterwards, leaving a window in which it sat
        on disk world-readable -- harmless while nothing used the key, not once
        it guards credentials. O_EXCL also means two processes starting at once
        cannot each write a different key and leave one of them decrypting
        garbage.
        """
        path = self.app.secret_key_path
        if path.exists():
            return path.read_text().strip()
        path.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_urlsafe(48)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            # Lost the race to another process; its key is the key.
            return path.read_text().strip()
        with os.fdopen(fd, "w") as handle:
            handle.write(key)
        return key


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _coerce(raw: str) -> Any:
    """Environment variables arrive as strings; give YAML a chance at them."""
    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError:
        return raw


def _env_overlay() -> dict[str, Any]:
    overlay: dict[str, Any] = {}
    for key, value in os.environ.items():
        if not key.startswith(ENV_PREFIX):
            continue
        path = key[len(ENV_PREFIX) :].lower().split("__")
        cursor = overlay
        for part in path[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[path[-1]] = _coerce(value)
    return overlay


def load_config(path: Path | None = None) -> Config:
    """The effective configuration: defaults, then the YAML file, then the environment.

    A file named by `SPARK_CONFIG` (the image sets it) has to exist. It used to
    be skipped quietly, so a container started without its config mount ran on
    defaults -- listening everywhere, in password mode, with no subnets --
    and nothing said so. Only the built-in default path may be absent, which is
    the development case (`spark` from a checkout with env overrides).
    """
    named = os.environ.get("SPARK_CONFIG")
    if path is None:
        path = Path(named) if named else DEFAULT_CONFIG_PATH
    data: dict[str, Any] = {}
    if path.exists():
        loaded = yaml.safe_load(path.read_text()) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"{path} must contain a YAML mapping at the top level")
        data = loaded
    elif named and Path(named) == path:
        raise SystemExit(
            f"SPARK_CONFIG points at {path}, which does not exist.\n"
            "The shipped template is config/spark.example.yaml -- copy it and edit the copy:\n"
            "    cp config/spark.example.yaml config/spark.yaml\n"
            "(from the spark/ directory, the one docker-compose.yml is in), then start again."
        )
    data = _deep_merge(data, _env_overlay())
    config = Config.model_validate(data)
    config.auth.validate_for_use()
    config.app.data_dir.mkdir(parents=True, exist_ok=True)
    return config
