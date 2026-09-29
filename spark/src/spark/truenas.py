"""TrueNAS over its API: JSON-RPC 2.0 on a WebSocket, HTTPS only.

What the TrueNAS docs settle, and this module follows (checked 2026-09-29):

  * The REST API was deprecated in 25.04 and is removed in 26; the supported
    API is JSON-RPC 2.0 over a WebSocket at ``wss://<host>/api/current``.
  * TrueNAS revokes a user-linked API key that is ever sent over plain HTTP.
    So there is no unencrypted WebSocket here at all, not even as a fallback.
  * Login is ``auth.login_with_api_key`` with the key as its one argument.

TrueNAS ships with a self-signed certificate, so ordinary verification
would fail. Instead the certificate is pinned, the way SSH pins a host key:
the first Test fetches it and sends nothing else, a person presses Trust
after reading its SHA-256 fingerprint, and from then on SPARK sends the key
only down a connection whose certificate matches. A replaced certificate
stops everything until it is trusted again.

Each Test (and the 5-minute check, credentials.py) also reads storage over
the same login, with query methods only, which a Read-only Administrator
may call -- checked against a real 25.10 box:

  * ``pool.query``: each pool's status and last scrub, and its topology,
    whose DISK leaves carry each drive's status and read/write/checksum
    error counts, and which pool and vdev it belongs to;
  * ``disk.query``: model, size, type and bus of every disk;
  * ``disk.temperatures``: {"sda": 43.0, ...}, None for a disk with no sensor;
  * ``alert.list``: TrueNAS's own alerts (SMART failures among them).

A method that fails (a role without the right, an older TrueNAS) leaves its
part out rather than failing the check. `parse` turns the answers into the
small, plain shape stored on the credential (`ApiCredential.readings`).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import InvalidHandshake, WebSocketException

log = logging.getLogger(__name__)

CONNECT_TIMEOUT = 10.0
CALL_TIMEOUT = 30.0
MAX_MESSAGE = 16 * 1024 * 1024      # a large pool's topology is a big answer


class TrueNASError(Exception):
    """Something a person should read: what failed and, where known, why."""


class CertificateNotTrusted(TrueNASError):
    """The certificate is not the pinned one (or none is pinned yet).
    Nothing was sent. `fingerprint` is what the server presented."""

    def __init__(self, fingerprint: str, *, changed: bool) -> None:
        self.fingerprint = fingerprint
        self.changed = changed
        super().__init__(
            "TrueNAS presented a different certificate from the one trusted. Nothing was "
            "sent. If the certificate was renewed on purpose, check the fingerprint and "
            "trust it again." if changed else
            "Check this certificate's fingerprint against TrueNAS (System → Certificates) "
            "and trust it. Nothing was sent."
        )


class LoginFailed(TrueNASError):
    pass


def fingerprint_of(der: bytes) -> str:
    """SHA-256, as TrueNAS and browsers show it: AA:BB:..."""
    return ":".join(f"{b:02X}" for b in hashlib.sha256(der).digest())


def url_for(host: str) -> str:
    return f"wss://{host}/api/current"


def _tls() -> ssl.SSLContext:
    # Verification is done by pinning (above), not by a CA; the handshake
    # itself still has to be real TLS.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


@dataclass
class Info:
    version: str | None
    hostname: str | None
    fingerprint: str
    readings: dict = field(default_factory=dict)


class Client:
    """One logged-in connection. Use `session()`."""

    def __init__(self, ws: ClientConnection, fingerprint: str) -> None:
        self.ws = ws
        self.fingerprint = fingerprint
        self._next = 0

    async def call(self, method: str, *params: Any) -> Any:
        self._next += 1
        ident = self._next
        await self.ws.send(json.dumps({"jsonrpc": "2.0", "id": ident, "method": method,
                                       "params": list(params)}))
        try:
            async with asyncio.timeout(CALL_TIMEOUT):
                while True:
                    message = json.loads(await self.ws.recv())
                    if message.get("id") != ident:
                        continue            # an event notification; not ours
                    if "error" in message:
                        error = message["error"] or {}
                        detail = (error.get("data") or {}).get("reason") or error.get("message")
                        raise TrueNASError(f"{method}: {detail or 'error'}")
                    return message.get("result")
        except TimeoutError:
            raise TrueNASError(f"{method}: no answer within {CALL_TIMEOUT:.0f} seconds.") from None
        except WebSocketException as exc:
            raise TrueNASError(f"{method}: the connection closed ({exc}).") from None


class session:  # noqa: N801 - used as `async with truenas.session(...)`
    """Connect, check the certificate against the pin, log in.

    ``pinned=None`` never logs in: it connects, reads the certificate and
    raises CertificateNotTrusted with its fingerprint.
    """

    def __init__(self, host: str, api_key: str, pinned: str | None) -> None:
        self.host, self.api_key, self.pinned = host, api_key, pinned
        self._cm = None

    async def __aenter__(self) -> Client:
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT):
                self._cm = connect(url_for(self.host), ssl=_tls(), max_size=MAX_MESSAGE,
                                   open_timeout=CONNECT_TIMEOUT, user_agent_header="SPARK")
                ws = await self._cm.__aenter__()
        except TimeoutError:
            raise TrueNASError(f"No answer from {self.host} within {CONNECT_TIMEOUT:.0f} seconds.") from None
        except (OSError, InvalidHandshake, WebSocketException) as exc:
            raise TrueNASError(f"Could not open the TrueNAS API at {url_for(self.host)}: {exc}") from None
        try:
            der = ws.transport.get_extra_info("ssl_object").getpeercert(binary_form=True)
            seen = fingerprint_of(der)
            if self.pinned is None or seen != self.pinned:
                raise CertificateNotTrusted(seen, changed=self.pinned is not None)
            client = Client(ws, seen)
            if await client.call("auth.login_with_api_key", self.api_key) is not True:
                raise LoginFailed("TrueNAS refused the API key. Check it has not been revoked "
                                  "or expired (Credentials → Users, or My API Keys).")
            return client
        except BaseException:
            await self._cm.__aexit__(None, None, None)
            raise

    async def __aexit__(self, *exc) -> None:  # type: ignore[no-untyped-def]
        if self._cm is not None:
            await self._cm.__aexit__(*exc)


async def test(host: str, api_key: str, pinned: str | None) -> Info:
    """Log in, read who answered, and read storage. Raises TrueNASError (or
    a subclass) if the login or system.info fails; storage it cannot read
    is left out of `readings` instead."""
    async with session(host, api_key, pinned) as client:
        info = await client.call("system.info") or {}
        readings = await read_storage(client)
        return Info(version=info.get("version"), hostname=info.get("hostname"),
                    fingerprint=client.fingerprint, readings=readings)


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

# TrueNAS's alert levels, least to most severe.
LEVELS = ("INFO", "NOTICE", "WARNING", "ERROR", "CRITICAL", "ALERT", "EMERGENCY")
MAX_ALERTS = 50
MAX_TEXT = 500


async def _optional(client: Client, method: str) -> Any:
    try:
        return await client.call(method)
    except TrueNASError as exc:
        log.info("TrueNAS: %s", exc)
        return None


async def read_storage(client: Client) -> dict:
    return parse(pools=await _optional(client, "pool.query"),
                 disks=await _optional(client, "disk.query"),
                 temperatures=await _optional(client, "disk.temperatures"),
                 alerts=await _optional(client, "alert.list"))


def _when(value: Any) -> str | None:
    """TrueNAS dates arrive as {"$date": milliseconds}. ISO 8601, UTC."""
    if isinstance(value, dict):
        value = value.get("$date")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _text(value: Any, limit: int = 128) -> str | None:
    return str(value)[:limit] if value not in (None, "") else None


def _members(node: Any, *, pool: str, group: str, vdev: str | None, out: dict) -> None:
    """The DISK leaves under one topology node."""
    if not isinstance(node, dict):
        return
    if str(node.get("type", "")).upper() == "DISK":
        name = _text(node.get("disk")) or _text(node.get("name")) or "?"
        stats = node.get("stats") or {}
        out[name] = {"pool": pool, "group": group, "vdev": vdev,
                     "status": _text(node.get("status"), 32),
                     "read_errors": _count(stats.get("read_errors")),
                     "write_errors": _count(stats.get("write_errors")),
                     "checksum_errors": _count(stats.get("checksum_errors"))}
        return
    for child in node.get("children") or []:
        _members(child, pool=pool, group=group, vdev=_text(node.get("name")), out=out)


def parse(*, pools: Any, disks: Any, temperatures: Any, alerts: Any) -> dict:
    """The answers, cut down to what SPARK shows and alerts on. A part is
    None when TrueNAS did not answer it; an empty list when it had none."""
    out: dict[str, Any] = {"pools": None, "drives": None, "alerts": None}
    members: dict[str, dict] = {}
    if isinstance(pools, list):
        out["pools"] = []
        for p in pools:
            if not isinstance(p, dict) or not p.get("name"):
                continue
            name = str(p["name"])[:128]
            scan = p.get("scan") if isinstance(p.get("scan"), dict) else {}
            out["pools"].append({
                "name": name, "status": _text(p.get("status"), 32), "healthy": p.get("healthy") is True,
                "scrub": {"function": _text(scan.get("function"), 32), "state": _text(scan.get("state"), 32),
                          "end": _when(scan.get("end_time")), "errors": _count(scan.get("errors"))}
                         if scan else None})
            topology = p.get("topology") if isinstance(p.get("topology"), dict) else {}
            for group, nodes in topology.items():
                for node in nodes or []:
                    _members(node, pool=name, group=str(group)[:32], vdev=None, out=members)

    temps = temperatures if isinstance(temperatures, dict) else {}
    if isinstance(disks, list) or members:
        drives: dict[str, dict] = {}
        for d in disks if isinstance(disks, list) else []:
            if not isinstance(d, dict) or not d.get("name"):
                continue
            size = d.get("size")
            drives[str(d["name"])[:128]] = {
                "model": _text(d.get("model")), "type": _text(d.get("type"), 16),
                "bus": _text(d.get("bus"), 16),
                "size": size if isinstance(size, int) and not isinstance(size, bool) else None}
        for name, member in members.items():
            drives.setdefault(name, {"model": None, "type": None, "bus": None, "size": None})
            drives[name].update(member)
        out["drives"] = []
        for name in sorted(drives):
            drive = {"name": name, "pool": None, "group": None, "vdev": None, "status": None,
                     "read_errors": 0, "write_errors": 0, "checksum_errors": 0, **drives[name]}
            celsius = temps.get(name)
            drive["celsius"] = float(celsius) if isinstance(celsius, (int, float)) \
                and not isinstance(celsius, bool) else None
            out["drives"].append(drive)

    if isinstance(alerts, list):
        kept = []
        for a in alerts:
            if not isinstance(a, dict) or a.get("dismissed"):
                continue
            level = str(a.get("level") or "INFO").upper()
            kept.append({
                "uuid": _text(a.get("uuid") or a.get("id"), 40)
                        or f"{a.get('klass')}:{a.get('key')}"[:40],
                "level": level if level in LEVELS else "INFO",
                "klass": _text(a.get("klass"), 64),
                "text": _text(a.get("formatted") or a.get("text"), MAX_TEXT) or "",
                "at": _when(a.get("datetime"))})
        kept.sort(key=lambda a: -LEVELS.index(a["level"]))
        out["alerts"] = kept[:MAX_ALERTS]
    return out
