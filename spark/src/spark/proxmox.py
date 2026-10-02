"""Proxmox VE over its REST API: HTTPS only, an API token, read only.

What the Proxmox docs and source settle, and this module follows (checked
2026-09-30 against the API definitions in pve-manager and pve-storage):

  * The API is ``https://<host>:8006/api2/json/...``; an API token goes in
    the header ``Authorization: PVEAPIToken=USER@REALM!TOKENID=SECRET``, and
    needs no ticket or CSRF token.
  * The PVEAuditor role is read only. With it on ``/`` a token can read
    every call below; it can start, stop or change nothing.

Proxmox ships with a self-signed certificate, so it is pinned the same way
as TrueNAS's (truenas.py): the first Test connects, reads the certificate
and sends nothing; a person presses Trust on its SHA-256; after that the
token only ever goes down a connection whose certificate matches. The
request is written by hand on the TLS connection, after the check, so the
token cannot leave before the certificate has been looked at -- an HTTP
library would send the header first and show the certificate after.

Read on each Test and every 5 minutes (credentials.py):

  * ``/version``: that the token works (the one call that must answer);
  * ``/nodes``: each node, online or not, CPU, memory, uptime;
  * ``/cluster/resources?type=vm``: every VM and container, running or not;
  * per online node, ``/status`` (version, CPU model), ``/storage`` (the
    enabled storages, active or not, and space), ``/disks/zfs`` (each pool's
    health) and ``/disks/list`` (each drive's SMART health and wear).

A call that fails (a narrower role, an older Proxmox) leaves its part out
rather than failing the check. `parse` turns the answers into the small,
plain shape stored on the credential (`ApiCredential.readings`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import ssl
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from .truenas import fingerprint_of

log = logging.getLogger(__name__)

DEFAULT_PORT = 8006
CONNECT_TIMEOUT = 10.0
CALL_TIMEOUT = 30.0
MAX_HEADERS = 64 * 1024
MAX_BODY = 16 * 1024 * 1024

# USER@REALM!TOKENID=SECRET. The secret is a UUID today; anything without
# spaces is let through, in case that changes.
TOKEN = re.compile(r"^[^\s@!=]+@[^\s@!=]+![A-Za-z][A-Za-z0-9._-]*=\S+$")
TOKEN_ID = re.compile(r"^[^\s@!=]+@[^\s@!=]+![A-Za-z][A-Za-z0-9._-]*$")


class ProxmoxError(Exception):
    """Something a person should read: what failed and, where known, why."""


class CertificateNotTrusted(ProxmoxError):
    """The certificate is not the pinned one (or none is pinned yet).
    Nothing was sent. `fingerprint` is what the server presented."""

    def __init__(self, fingerprint: str, *, changed: bool) -> None:
        self.fingerprint = fingerprint
        self.changed = changed
        super().__init__(
            "Proxmox presented a different certificate from the one trusted. Nothing was "
            "sent. If the certificate was renewed on purpose, check the fingerprint and "
            "trust it again." if changed else
            "Check this certificate's fingerprint against Proxmox (the node → System → "
            "Certificates) and trust it. Nothing was sent."
        )


class LoginFailed(ProxmoxError):
    pass


class Denied(ProxmoxError):
    """403: the token may not read this. Its part is left out."""


def check_token(raw: str) -> str:
    """The token as Proxmox wants it in the header, or ValueError saying why."""
    token = (raw or "").strip()
    if token.upper().startswith("PVEAPITOKEN="):
        token = token[len("PVEAPITOKEN="):]
    if not TOKEN.match(token):
        raise ValueError(
            "Paste the whole token: USER@REALM!TOKENID=SECRET, for example "
            "spark@pve!monitor=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx.")
    return token


def join_token(token_id: str, secret: str) -> str:
    """The Token ID and Secret as Proxmox shows them when a token is made,
    joined for the header. A whole token pasted as the secret is fine too.
    ValueError says what is wrong, for a person."""
    token_id, secret = (token_id or "").strip(), (secret or "").strip()
    if TOKEN.match(secret.removeprefix("PVEAPIToken=")):
        whole = check_token(secret)
        if token_id and not whole.startswith(f"{token_id}="):
            raise ValueError("The secret pasted is a whole token for a different token ID.")
        return whole
    if not token_id:
        raise ValueError("Enter the token ID as well, for example spark@pve!monitor.")
    if not TOKEN_ID.match(token_id):
        raise ValueError(
            f"{token_id!r} is not a token ID. It is the user, then ! and the token's name, "
            "for example spark@pve!monitor (Datacenter → Permissions → API Tokens lists it).")
    if not secret:
        raise ValueError("Paste the token's secret.")
    if any(c.isspace() for c in secret) or "=" in secret:
        raise ValueError("That is not a token secret. It looks like "
                         "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx.")
    return f"{token_id}={secret}"


def split_token(token: str) -> tuple[str, str]:
    """(token ID, secret) from a whole token."""
    token_id, _, secret = token.partition("=")
    return token_id, secret


def split_host(host: str) -> tuple[str, int]:
    """("address", port) from a cleaned host (credentials.clean_host)."""
    if host.startswith("["):
        inside, _, rest = host[1:].partition("]")
        return inside, int(rest[1:]) if rest.startswith(":") else DEFAULT_PORT
    if host.count(":") == 1:
        name, _, port = host.partition(":")
        return name, int(port)
    return host, DEFAULT_PORT


def url_for(host: str, path: str = "") -> str:
    name, port = split_host(host)
    shown = f"[{name}]" if ":" in name else name
    return f"https://{shown}:{port}/api2/json{path}"


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
    """Pinned GETs to one Proxmox. A connection per call, and the
    certificate checked on each, before anything is written."""

    def __init__(self, host: str, token: str, pinned: str | None) -> None:
        self.host, self.token, self.pinned = host, token, pinned
        self.fingerprint: str | None = None

    async def _open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        name, port = split_host(self.host)
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(name, port, ssl=_tls(), limit=MAX_HEADERS),
                CONNECT_TIMEOUT)
        except TimeoutError:
            raise ProxmoxError(f"No answer from {self.host} within {CONNECT_TIMEOUT:.0f} seconds.") from None
        except (OSError, ssl.SSLError) as exc:
            raise ProxmoxError(f"Could not open the Proxmox API at {url_for(self.host)}: {exc}") from None
        der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
        seen = fingerprint_of(der)
        self.fingerprint = seen
        if self.pinned is None or seen != self.pinned:
            writer.close()
            raise CertificateNotTrusted(seen, changed=self.pinned is not None)
        return reader, writer

    async def get(self, path: str) -> Any:
        reader, writer = await self._open()
        name, port = split_host(self.host)
        host_header = f"[{name}]:{port}" if ":" in name else f"{name}:{port}"
        request = (f"GET /api2/json{path} HTTP/1.1\r\n"
                   f"Host: {host_header}\r\n"
                   f"Authorization: PVEAPIToken={self.token}\r\n"
                   "Accept: application/json\r\n"
                   "User-Agent: SPARK\r\n"
                   "Connection: close\r\n\r\n")
        try:
            async with asyncio.timeout(CALL_TIMEOUT):
                writer.write(request.encode())
                await writer.drain()
                status, reason, body = await _response(reader)
        except TimeoutError:
            raise ProxmoxError(f"{path}: no answer within {CALL_TIMEOUT:.0f} seconds.") from None
        except (OSError, ssl.SSLError, asyncio.IncompleteReadError, ValueError) as exc:
            raise ProxmoxError(f"{path}: the connection closed ({exc}).") from None
        finally:
            writer.close()
        if status == 401:
            raise LoginFailed("Proxmox refused the token. Check it is typed as "
                              "USER@REALM!TOKENID=SECRET and has not been removed or expired "
                              "(Datacenter → Permissions → API Tokens).")
        if status == 403:
            raise Denied(f"{path}: not allowed ({reason}). Give the token the PVEAuditor "
                         "role on / (Datacenter → Permissions).")
        if status != 200:
            raise ProxmoxError(f"{path}: HTTP {status} {reason}".strip())
        try:
            return json.loads(body or b"{}").get("data")
        except (ValueError, AttributeError):
            raise ProxmoxError(f"{path}: the answer was not JSON.") from None


async def _response(reader: asyncio.StreamReader,
                    error: type[Exception] = ProxmoxError) -> tuple[int, str, bytes]:
    """Status, reason and body of one HTTP/1.1 response on a `Connection:
    close` socket: Content-Length, chunked, or to the end. A malformed or
    oversized answer raises `error` (unifi.py shares this)."""
    try:
        head = await reader.readuntil(b"\r\n\r\n")
    except asyncio.LimitOverrunError:
        raise error("The answer's headers were too long.") from None
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ", 2)
    if len(parts) < 2 or not parts[0].startswith("HTTP/") or not parts[1].isdigit():
        raise error("The answer was not HTTP.")
    status, reason = int(parts[1]), parts[2] if len(parts) > 2 else ""
    headers = {}
    for line in lines[1:]:
        key, sep, value = line.partition(":")
        if sep:
            headers[key.strip().lower()] = value.strip()
    if "chunked" in headers.get("transfer-encoding", "").lower():
        body = bytearray()
        while True:
            size_line = await reader.readuntil(b"\r\n")
            size = int(size_line.split(b";")[0].strip() or b"0", 16)
            if size == 0:
                break
            if len(body) + size > MAX_BODY:
                raise error("The answer was too large.")
            body += await reader.readexactly(size)
            await reader.readexactly(2)
        return status, reason, bytes(body)
    if "content-length" in headers:
        length = int(headers["content-length"])
        if length > MAX_BODY:
            raise error("The answer was too large.")
        return status, reason, await reader.readexactly(length)
    body = bytearray()
    while chunk := await reader.read(65536):
        body += chunk
        if len(body) > MAX_BODY:
            raise error("The answer was too large.")
    return status, reason, bytes(body)


async def _optional(client: Client, path: str) -> Any:
    try:
        return await client.get(path)
    except CertificateNotTrusted:
        raise
    except ProxmoxError as exc:
        log.info("Proxmox: %s", exc)
        return None


async def test(host: str, token: str, pinned: str | None) -> Info:
    """Check the token, then read everything. Raises ProxmoxError (or a
    subclass) if the certificate or /version fails; a part it cannot read is
    left out of `readings` instead."""
    client = Client(host, token, pinned)
    version = await client.get("/version") or {}
    nodes = await _optional(client, "/nodes")
    guests = await _optional(client, "/cluster/resources?type=vm")
    status: dict[str, Any] = {}
    storage: dict[str, Any] = {}
    zfs: dict[str, Any] = {}
    disks: dict[str, Any] = {}
    for n in nodes if isinstance(nodes, list) else []:
        if not isinstance(n, dict) or not n.get("node") or n.get("status") != "online":
            continue
        name = str(n["node"])
        base = f"/nodes/{quote(name, safe='')}"
        status[name] = await _optional(client, f"{base}/status")
        storage[name] = await _optional(client, f"{base}/storage?enabled=1")
        zfs[name] = await _optional(client, f"{base}/disks/zfs")
        disks[name] = await _optional(client, f"{base}/disks/list")
    readings = parse(nodes=nodes, guests=guests, status=status, storage=storage,
                     zfs=zfs, disks=disks)
    names = [n["name"] for n in readings.get("nodes") or []]
    return Info(version=_text(version.get("version"), 32) if isinstance(version, dict) else None,
                hostname=", ".join(names)[:128] or None,
                fingerprint=client.fingerprint or "", readings=readings)


# --------------------------------------------------------------------------
# The answers, cut down
# --------------------------------------------------------------------------

MAX_ROWS = 500


def _text(value: Any, limit: int = 128) -> str | None:
    return str(value)[:limit] if value not in (None, "") else None


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value >= 0:
        return int(value)
    return None


def _fraction(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0.0, float(value))


def _per_node(answers: dict[str, Any]) -> tuple[list[tuple[str, dict]], list[str]]:
    """(node, item) for every item, and the nodes that answered at all."""
    items, answered = [], []
    for node, answer in answers.items():
        if isinstance(answer, list):
            answered.append(node)
            items.extend((node, a) for a in answer if isinstance(a, dict))
    return items, answered


def parse(*, nodes: Any, guests: Any, status: dict, storage: dict, zfs: dict,
          disks: dict) -> dict:
    """A part is None when Proxmox did not answer it, an empty list when it
    had none. `answered` names the nodes each per-node part came from, so a
    node that did not answer is not taken to have lost its drives."""
    out: dict[str, Any] = {"nodes": None, "guests": None, "storages": None, "zfs": None,
                           "disks": None, "answered": {}}
    if isinstance(nodes, list):
        out["nodes"] = []
        for n in nodes:
            if not isinstance(n, dict) or not n.get("node"):
                continue
            name = str(n["node"])[:64]
            s = status.get(name) if isinstance(status.get(name), dict) else {}
            memory = s.get("memory") if isinstance(s.get("memory"), dict) else {}
            cpuinfo = s.get("cpuinfo") if isinstance(s.get("cpuinfo"), dict) else {}
            load = s.get("loadavg") if isinstance(s.get("loadavg"), list) else []
            pveversion = _text(s.get("pveversion"), 64)
            out["nodes"].append({
                "name": name, "status": _text(n.get("status"), 16) or "unknown",
                "cpu": _fraction(n.get("cpu")), "cores": _int(n.get("maxcpu")),
                "mem": _int(memory.get("used")) if memory else _int(n.get("mem")),
                "maxmem": _int(memory.get("total")) if memory else _int(n.get("maxmem")),
                "uptime": _int(n.get("uptime")),
                # "pve-manager/9.1.8/abcdef..." -> "9.1.8"
                "version": pveversion.split("/")[1] if pveversion and pveversion.count("/") >= 1
                           else pveversion,
                "kernel": _text((s.get("current-kernel") or {}).get("release")
                                if isinstance(s.get("current-kernel"), dict) else s.get("kversion"), 64),
                "cpu_model": _text(cpuinfo.get("model"), 96),
                "load": [round(float(x), 2) for x in load[:3]
                         if isinstance(x, (int, float, str)) and _is_number(x)],
            })
        out["nodes"].sort(key=lambda n: n["name"].lower())

    if isinstance(guests, list):
        kept = []
        for g in guests:
            if not isinstance(g, dict) or g.get("type") not in ("qemu", "lxc"):
                continue
            if g.get("template") in (1, True, "1"):
                continue
            vmid = _int(g.get("vmid"))
            if vmid is None:
                continue
            kept.append({
                "vmid": vmid, "name": _text(g.get("name"), 64) or f"{vmid}",
                "kind": "VM" if g["type"] == "qemu" else "CT",
                "node": _text(g.get("node"), 64), "status": _text(g.get("status"), 16) or "unknown",
                "cpu": _fraction(g.get("cpu")), "cores": _int(g.get("maxcpu")),
                "mem": _int(g.get("mem")), "maxmem": _int(g.get("maxmem")),
                "uptime": _int(g.get("uptime")),
            })
        kept.sort(key=lambda g: g["vmid"])
        out["guests"] = kept[:MAX_ROWS]

    items, answered = _per_node(storage)
    if answered:
        out["answered"]["storages"] = answered
        out["storages"] = []
        for node, s in items[:MAX_ROWS]:
            if not s.get("storage"):
                continue
            total, used = _int(s.get("total")), _int(s.get("used"))
            out["storages"].append({
                "node": node, "name": str(s["storage"])[:64], "type": _text(s.get("type"), 32),
                "content": _text(s.get("content"), 128),
                "active": s.get("active") in (1, True, "1"),
                "shared": s.get("shared") in (1, True, "1"),
                "total": total, "used": used,
                "pct": round(100.0 * used / total, 1) if total and used is not None else None,
            })

    items, answered = _per_node(zfs)
    if answered:
        out["answered"]["zfs"] = answered
        out["zfs"] = []
        for node, p in items[:MAX_ROWS]:
            if not p.get("name"):
                continue
            size, alloc = _int(p.get("size")), _int(p.get("alloc"))
            out["zfs"].append({
                "node": node, "name": str(p["name"])[:64],
                "health": (_text(p.get("health"), 32) or "UNKNOWN").upper(),
                "size": size, "alloc": alloc, "free": _int(p.get("free")),
                "frag": _int(p.get("frag")),
                "pct": round(100.0 * alloc / size, 1) if size and alloc is not None else None,
            })

    items, answered = _per_node(disks)
    if answered:
        out["answered"]["disks"] = answered
        out["disks"] = []
        for node, d in items[:MAX_ROWS]:
            if not d.get("devpath") or d.get("parent"):
                continue            # a partition, if one were ever listed
            wear = d.get("wearout")
            out["disks"].append({
                "node": node, "devpath": str(d["devpath"])[:64],
                "model": _text(d.get("model"), 64), "type": _text(d.get("type"), 16),
                "size": _int(d.get("size")),
                "health": (_text(d.get("health"), 32) or "UNKNOWN").upper(),
                # Percent of life LEFT (Proxmox: 100 - "Percentage Used");
                # "N/A" when the drive does not say.
                "wearout": _int(wear) if not isinstance(wear, str) else
                           (int(wear) if wear.isdigit() else None),
                "used": _text(d.get("used"), 32),
            })
    return out


def _is_number(value: Any) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True
