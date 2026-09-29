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
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import ssl
from dataclasses import dataclass
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import InvalidHandshake, WebSocketException

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
    """Log in and read who answered. Raises TrueNASError (or a subclass)."""
    async with session(host, api_key, pinned) as client:
        info = await client.call("system.info") or {}
        return Info(version=info.get("version"), hostname=info.get("hostname"),
                    fingerprint=client.fingerprint)
