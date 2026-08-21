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
from pydantic import BaseModel, Field, field_validator

DEFAULT_CONFIG_PATH = Path(os.environ.get("SPARK_CONFIG", "/config/spark.yaml"))
ENV_PREFIX = "SPARK__"


class AppConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 9700
    data_dir: Path = Path("/data")
    # Cosmetic only; used in page titles and Discord messages so friends running
    # their own copy can tell instances apart.
    instance_name: str = "SPARK"
    log_level: str = "INFO"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "spark.db"

    @property
    def secret_key_path(self) -> Path:
        return self.data_dir / "secret.key"


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

    def secret_key(self) -> str:
        """Session-signing key, generated once and persisted.

        Kept in a file rather than the database so that restoring a database
        backup onto a fresh install doesn't silently resurrect old sessions.
        """
        path = self.app.secret_key_path
        if path.exists():
            return path.read_text().strip()
        path.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_urlsafe(48)
        path.write_text(key)
        path.chmod(0o600)
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
    path = path or DEFAULT_CONFIG_PATH
    data: dict[str, Any] = {}
    if path.exists():
        loaded = yaml.safe_load(path.read_text()) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"{path} must contain a YAML mapping at the top level")
        data = loaded
    data = _deep_merge(data, _env_overlay())
    config = Config.model_validate(data)
    config.auth.validate_for_use()
    config.app.data_dir.mkdir(parents=True, exist_ok=True)
    return config
