"""UniFi Network over its official Integration API: HTTPS only, an API key,
read only.

What Ubiquiti's own API reference settles (developer.ui.com, the Network
API's OpenAPI documents, compared 2026-10-02 from 9.1.120 to 10.6.106), and
this module follows:

  * On the console the API is ``https://<console>/proxy/network/integration``
    and the key goes in the header ``X-API-KEY``. The key is made in UniFi
    Network, on its Integrations page.
  * Lists are paged: ``offset`` and ``limit`` (25 by default, 200 at most),
    answered as ``{offset, limit, count, totalCount, data}``.
  * The calls SPARK makes are the same in every version published, and their
    fields only grow: 10.x adds the firmware fields to the device list (9.x
    has them on the device's own page) and a device state. A radio's band is
    ``"5"`` in 9.x and ``5`` in 10.x. So this reads every field as optional,
    takes a number written either way, and keeps a state it has not seen
    before as it is written. Anything else it does not know, it ignores.

The console's certificate is self-signed unless someone has replaced it, so
it is pinned exactly as TrueNAS's and Proxmox's are (proxmox.py): nothing,
the key included, is sent until a person has trusted the fingerprint, and
then only down a connection presenting that certificate.

Read on each Test and every 5 minutes (credentials.py), GET only:

  * ``/v1/info``: the UniFi Network version (the one call that must answer);
  * ``/v1/sites``: every site;
  * per site, ``/devices`` (every adopted device and its state) and
    ``/clients`` (counted: wired, wireless, VPN, guests, and per device);
  * per device, its own page (firmware, uplink, ports, radios) and, while it
    is online, ``/statistics/latest`` (CPU, memory, uptime, uplink rate).

A call that fails leaves its part out rather than failing the check. Client
names and addresses are not kept, only how many there are.
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

from .proxmox import _response, _tls
from .truenas import fingerprint_of

log = logging.getLogger(__name__)

DEFAULT_PORT = 443
BASE = "/proxy/network/integration"
CONNECT_TIMEOUT = 10.0
CALL_TIMEOUT = 30.0
MAX_HEADERS = 64 * 1024
PAGE = 200                 # the most the API gives in one page
MAX_SITES = 20
MAX_DEVICES = 300          # across all sites; each costs two calls
MAX_CLIENTS = 5000         # counted, not kept


class UniFiError(Exception):
    """Something a person should read: what failed and, where known, why."""


class CertificateNotTrusted(UniFiError):
    """The certificate is not the pinned one (or none is pinned yet).
    Nothing was sent. `fingerprint` is what the console presented."""

    def __init__(self, fingerprint: str, *, changed: bool) -> None:
        self.fingerprint = fingerprint
        self.changed = changed
        super().__init__(
            "The UniFi console presented a different certificate from the one trusted. "
            "Nothing was sent. If the certificate was replaced on purpose, check the "
            "fingerprint and trust it again." if changed else
            "Check this certificate's fingerprint against the console's and trust it. "
            "Nothing was sent."
        )


class LoginFailed(UniFiError):
    pass


class Denied(UniFiError):
    """403: the key may not read this. Its part is left out."""


def check_key(raw: str) -> str:
    """The key as it goes in the header, or ValueError saying why. UniFi does
    not document a format, so this only refuses what cannot be a key."""
    key = (raw or "").strip()
    if key.upper().startswith("X-API-KEY:"):
        key = key[len("X-API-KEY:"):].strip()
    if not key:
        raise ValueError("Paste the API key.")
    if any(c.isspace() for c in key) or not key.isprintable() or not key.isascii():
        raise ValueError("That is not an API key: it has spaces or other characters a key "
                         "does not. Copy it again from UniFi Network's Integrations page.")
    if len(key) < 16 or len(key) > 512:
        raise ValueError("That is not an API key: it is too short or too long. Copy the "
                         "whole key from UniFi Network's Integrations page.")
    return key


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
    return f"https://{shown}{'' if port == DEFAULT_PORT else f':{port}'}{BASE}{path}"


@dataclass
class Info:
    version: str | None
    hostname: str | None
    fingerprint: str
    readings: dict = field(default_factory=dict)


def _said(body: bytes) -> str:
    """UniFi's own words from an error answer ({"message": ...}), if any."""
    try:
        message = json.loads(body or b"{}").get("message")
    except (ValueError, AttributeError):
        return ""
    if not isinstance(message, str):
        return ""
    message = " ".join(message.split())[:200]
    return f" UniFi says: {message}" if message else ""


class Client:
    """Pinned GETs to one console. A connection per call, and the
    certificate checked on each, before anything is written."""

    def __init__(self, host: str, key: str, pinned: str | None) -> None:
        self.host, self.key, self.pinned = host, key, pinned
        self.fingerprint: str | None = None

    async def _open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        name, port = split_host(self.host)
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(name, port, ssl=_tls(), limit=MAX_HEADERS),
                CONNECT_TIMEOUT)
        except TimeoutError:
            raise UniFiError(f"No answer from {self.host} within {CONNECT_TIMEOUT:.0f} seconds.") from None
        except (OSError, ssl.SSLError) as exc:
            raise UniFiError(f"Could not open the UniFi API at {url_for(self.host)}: {exc}") from None
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
        shown = f"[{name}]" if ":" in name else name
        host_header = shown if port == DEFAULT_PORT else f"{shown}:{port}"
        request = (f"GET {BASE}{path} HTTP/1.1\r\n"
                   f"Host: {host_header}\r\n"
                   f"X-API-KEY: {self.key}\r\n"
                   "Accept: application/json\r\n"
                   "User-Agent: SPARK\r\n"
                   "Connection: close\r\n\r\n")
        try:
            async with asyncio.timeout(CALL_TIMEOUT):
                writer.write(request.encode())
                await writer.drain()
                status, reason, body = await _response(reader, UniFiError)
        except TimeoutError:
            raise UniFiError(f"{path}: no answer within {CALL_TIMEOUT:.0f} seconds.") from None
        except (OSError, ssl.SSLError, asyncio.IncompleteReadError, ValueError) as exc:
            raise UniFiError(f"{path}: the connection closed ({exc}).") from None
        finally:
            writer.close()
        if status == 401:
            raise LoginFailed("UniFi refused the API key. Check it was copied whole and has "
                              "not been deleted or expired (UniFi Network → Integrations)."
                              + _said(body))
        if status == 403:
            raise Denied(f"{path}: not allowed ({reason}).{_said(body)}")
        if status == 404 and path == "/v1/info":
            raise UniFiError(
                f"There is no UniFi Network API at {url_for(self.host)}. Use the address of "
                "the UniFi console (the one UniFi Network runs on), with UniFi Network running.")
        if status == 429:
            raise UniFiError(f"{path}: UniFi asked SPARK to slow down (HTTP 429).")
        if status != 200:
            raise UniFiError(f"{path}: HTTP {status} {reason}".strip() + "." + _said(body))
        try:
            return json.loads(body or b"null")
        except ValueError:
            raise UniFiError(f"{path}: the answer was not JSON.") from None


async def _optional(client: Client, path: str) -> Any:
    try:
        return await client.get(path)
    except CertificateNotTrusted:
        raise
    except LoginFailed:
        raise
    except UniFiError as exc:
        log.info("UniFi: %s", exc)
        return None


async def _all(client: Client, path: str, most: int) -> list[dict] | None:
    """Every item of a paged list, up to `most`; None if the first page
    failed. A later page that fails ends the list where it got to."""
    items: list[dict] = []
    offset = 0
    while len(items) < most:
        sep = "&" if "?" in path else "?"
        page = await _optional(client, f"{path}{sep}offset={offset}&limit={PAGE}")
        if page is None:
            return items if offset else None
        data = page.get("data") if isinstance(page, dict) else page
        if not isinstance(data, list):
            return items if offset else None
        items.extend(d for d in data if isinstance(d, dict))
        total = _int(page.get("totalCount")) if isinstance(page, dict) else None
        offset += len(data)
        if not data or len(data) < PAGE and total is None or total is not None and offset >= total:
            break
    return items[:most]


async def test(host: str, key: str, pinned: str | None) -> Info:
    """Check the key, then read everything. Raises UniFiError (or a
    subclass) if the certificate or /v1/info fails; a part it cannot read is
    left out of `readings` instead."""
    client = Client(host, key, pinned)
    info = await client.get("/v1/info")
    version = _text(info.get("applicationVersion"), 32) if isinstance(info, dict) else None
    sites = await _all(client, "/v1/sites", MAX_SITES)
    devices: dict[str, Any] = {}
    clients: dict[str, Any] = {}
    details: dict[str, Any] = {}
    stats: dict[str, Any] = {}
    budget = MAX_DEVICES
    for site in sites or []:
        sid = site.get("id")
        if not isinstance(sid, str) or not sid:
            continue
        base = f"/v1/sites/{quote(sid, safe='')}"
        devices[sid] = await _all(client, f"{base}/devices", MAX_DEVICES)
        clients[sid] = await _all(client, f"{base}/clients", MAX_CLIENTS)
        for d in (devices[sid] or [])[:budget]:
            did = d.get("id")
            if not isinstance(did, str) or not did:
                continue
            budget -= 1
            path = f"{base}/devices/{quote(did, safe='')}"
            details[did] = await _optional(client, path)
            if str(d.get("state") or "").upper() == "ONLINE":
                stats[did] = await _optional(client, f"{path}/statistics/latest")
    readings = parse(version=version, sites=sites, devices=devices, clients=clients,
                     details=details, stats=stats)
    names = [s["name"] for s in readings.get("sites") or []]
    return Info(version=version, hostname=", ".join(names)[:128] or None,
                fingerprint=client.fingerprint or "", readings=readings)


# --------------------------------------------------------------------------
# The answers, cut down
# --------------------------------------------------------------------------


def _text(value: Any, limit: int = 128) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    text = " ".join(str(value).split())
    return text[:limit] or None


def _int(value: Any) -> int | None:
    """A whole number, given as a number or as text."""
    number = _number(value)
    return int(number) if number is not None and number >= 0 else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _pct(value: Any) -> float | None:
    number = _number(value)
    return round(min(100.0, max(0.0, number)), 1) if number is not None else None


def band(value: Any) -> str | None:
    """A radio's band as text, "2.4", "5", "6": 9.x writes "5", 10.x 5."""
    number = _number(value)
    return f"{number:g}" if number is not None and number > 0 else None


def _mac(value: Any) -> str | None:
    text = _text(value, 32)
    return text.lower().replace("-", ":") if text else None


def parse(*, version: str | None, sites: Any, devices: dict, clients: dict,
          details: dict, stats: dict) -> dict:
    """A part is None when UniFi did not answer it, empty when it had none.
    `answered` names the sites each per-site part came from."""
    out: dict[str, Any] = {"version": version, "sites": None, "devices": None,
                           "clients": None, "answered": {}}
    names: dict[str, str] = {}
    if isinstance(sites, list):
        out["sites"] = []
        for s in sites:
            if isinstance(s, dict) and isinstance(s.get("id"), str):
                name = _text(s.get("name"), 64) or _text(s.get("internalReference"), 64) or "site"
                names[s["id"]] = name
                out["sites"].append({"id": s["id"][:64], "name": name})

    answered = [sid for sid, d in devices.items() if isinstance(d, list)]
    if answered:
        out["answered"]["devices"] = answered
        out["devices"] = []
        for sid in answered:
            for d in devices[sid]:
                did = d.get("id") if isinstance(d, dict) else None
                if not isinstance(did, str) or not did:
                    continue
                full = details.get(did) if isinstance(details.get(did), dict) else {}
                st = stats.get(did) if isinstance(stats.get(did), dict) else {}
                out["devices"].append(_device(sid, names.get(sid), d, full, st))
            if len(out["devices"]) >= MAX_DEVICES:
                break
        out["devices"].sort(key=lambda d: (d["site_name"] or "", (d["name"] or "").lower()))

    answered = [sid for sid, c in clients.items() if isinstance(c, list)]
    if answered:
        out["answered"]["clients"] = answered
        counts = {"total": 0, "wired": 0, "wireless": 0, "vpn": 0, "teleport": 0,
                  "other": 0, "guest": 0}
        per: dict[str, int] = {}
        for sid in answered:
            for c in clients[sid]:
                if not isinstance(c, dict):
                    continue
                counts["total"] += 1
                kind = str(c.get("type") or "").lower()
                counts[kind if kind in ("wired", "wireless", "vpn", "teleport") else "other"] += 1
                access = c.get("access") if isinstance(c.get("access"), dict) else {}
                if str(access.get("type") or "").upper() == "GUEST":
                    counts["guest"] += 1
                up = c.get("uplinkDeviceId")
                if isinstance(up, str) and up:
                    per[up[:64]] = per.get(up[:64], 0) + 1
        out["clients"] = {**counts, "by_device": per}
    return out


def _device(sid: str, site_name: str | None, d: dict, full: dict, st: dict) -> dict:
    def pick(name: str) -> Any:
        """The device's own page first: it has more, and in 9.x the only
        firmware fields."""
        value = full.get(name)
        return d.get(name) if value is None else value

    interfaces = full.get("interfaces") if isinstance(full.get("interfaces"), dict) else {}
    ports = [p for p in interfaces.get("ports") or [] if isinstance(p, dict)]
    radios = [r for r in interfaces.get("radios") or [] if isinstance(r, dict)]
    uplink = full.get("uplink") if isinstance(full.get("uplink"), dict) else {}
    st_up = st.get("uplink") if isinstance(st.get("uplink"), dict) else {}
    features = d.get("features")
    if isinstance(features, dict):          # the device's own page: an object
        features = list(features)
    roles = [str(f) for f in features if isinstance(f, str)] if isinstance(features, list) else []
    if not roles and isinstance(full.get("features"), dict):
        roles = [str(f) for f in full["features"]]
    updatable = pick("firmwareUpdatable")
    bands = sorted({b for r in radios if (b := band(r.get("frequencyGHz")))}, key=float)
    return {
        "site": sid[:64], "site_name": site_name,
        "id": str(d["id"])[:64],
        "name": _text(pick("name"), 64) or _text(pick("model"), 64) or "device",
        "model": _text(pick("model"), 64),
        "mac": _mac(pick("macAddress")),
        "ip": _text(pick("ipAddress"), 64),
        "state": (_text(pick("state"), 32) or "UNKNOWN").upper(),
        "firmware": _text(pick("firmwareVersion"), 64),
        "updatable": updatable if isinstance(updatable, bool) else None,
        "roles": roles[:6],
        "uplink": _text(uplink.get("deviceId"), 64),
        "ports_up": sum(1 for p in ports if str(p.get("state") or "").upper() == "UP")
                    if ports else None,
        "ports": len(ports) or None,
        "bands": bands,
        "uptime": _int(st.get("uptimeSec")),
        "cpu": _pct(st.get("cpuUtilizationPct")),
        "mem": _pct(st.get("memoryUtilizationPct")),
        "load": _number(st.get("loadAverage1Min")),
        "tx_bps": _int(st_up.get("txRateBps")),
        "rx_bps": _int(st_up.get("rxRateBps")),
    }


# --------------------------------------------------------------------------
# Identity: the MACs UniFi knows
# --------------------------------------------------------------------------

_MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


async def fill_in_macs(session, readings: dict) -> list:  # type: ignore[no-untyped-def]
    """Give MAC-less devices the MAC UniFi reports at their address.

    Across a router SPARK sees no MACs, so a UniFi device on a routed VLAN
    is known to SPARK by IP alone. UniFi knows its MAC. The rule is the one
    identity.fill_in_macs uses for a router's ARP table: only when exactly
    one MAC-less device is at that address, no device has that MAC yet, and
    the address is not one an SNMP-polled device says is its own (that one
    is a duplicate awaiting a merge, which a MAC would block). Not a merge:
    the device gains the identity a sweep on its own subnet would have
    given it."""
    from sqlalchemy import select

    from . import identity
    from .discovery import oui
    from .models import Device

    by_ip: dict[str, str | None] = {}
    for d in readings.get("devices") or []:
        ip, mac = d.get("ip"), d.get("mac")
        if not ip or not mac or not _MAC.match(mac):
            continue
        # Two UniFi devices at one address: not sure which, so neither.
        by_ip[ip] = None if ip in by_ip and by_ip[ip] != mac else mac
    if not by_ip:
        return []
    own = (await identity.reports(session)).own
    devices = list((await session.execute(select(Device).order_by(Device.id))).scalars())
    in_use = {d.mac for d in devices if d.mac}
    wanting: dict[str, list] = {}
    for device in devices:
        if device.mac or not device.primary_ip or device.primary_ip in own:
            continue
        mac = by_ip.get(device.primary_ip)
        if mac:
            wanting.setdefault(mac, []).append(device)
    filled = []
    for mac, candidates in wanting.items():
        if len(candidates) == 1 and mac not in in_use:
            device = candidates[0]
            device.mac = mac
            device.vendor = device.vendor or oui.lookup(mac)
            filled.append(device)
            log.info("%s: MAC %s from UniFi", device.primary_ip, mac)
    await session.flush()
    return filled


# --------------------------------------------------------------------------
# The device page
# --------------------------------------------------------------------------

ONLINE = "ONLINE"
# Passing states: being set up, updated or adopted. Not a fault.
BUSY = ("UPDATING", "GETTING_READY", "ADOPTING", "PENDING_ADOPTION", "DELETING")

ROLE_WORDS = {"gateway": "gateway", "switching": "switch", "accessPoint": "access point"}


def state_pill(state: str) -> str:
    if state == ONLINE:
        return "ok"
    if state in BUSY:
        return "neutral"
    return "bad"


def view(row, *, macs: dict[str, int] | None = None,  # type: ignore[no-untyped-def]
         ips: dict[str, int] | None = None) -> dict | None:
    """What the UniFi card shows from this credential, or None. Each UniFi
    device links to its SPARK page: by MAC, or else by address (`ips`) for
    a device SPARK knows by IP alone."""
    if row.kind != "unifi":
        return None
    readings = row.readings or {}
    if readings.get("devices") is None and readings.get("clients") is None:
        return None
    from datetime import datetime

    from .charts import fmt_bps
    from .engine.state import human_duration

    macs, ips = macs or {}, ips or {}
    devices = readings.get("devices") or []
    named = {d["id"]: d["name"] for d in devices}
    clients = readings.get("clients") or {}
    per = clients.get("by_device") or {}
    rows = []
    for d in devices:
        online = d.get("state") == ONLINE
        rows.append({
            **d,
            "pill": state_pill(d.get("state") or ""),
            "state_words": (d.get("state") or "unknown").replace("_", " ").lower(),
            "role_words": ", ".join(ROLE_WORDS.get(r, r) for r in d.get("roles") or []),
            "via": named.get(d.get("uplink")) if d.get("uplink") else None,
            "spark_id": macs.get(d.get("mac") or "") or ips.get(d.get("ip") or ""),
            "clients": per.get(d["id"]),
            "up": human_duration(d["uptime"]) if online and d.get("uptime") else "—",
            "rate": (f"↓ {fmt_bps(d['rx_bps'])} · ↑ {fmt_bps(d['tx_bps'])}"
                     if online and d.get("rx_bps") is not None and d.get("tx_bps") is not None
                     else None),
        })
    try:
        read_at = datetime.fromisoformat(readings["read_at"]) if readings.get("read_at") else None
    except (TypeError, ValueError):
        read_at = None
    sites = {d.get("site") for d in devices}
    return {
        "read_at": read_at,
        "version": readings.get("version") or (row.last_info or {}).get("version"),
        "devices": rows,
        "online": sum(1 for d in rows if d.get("state") == ONLINE),
        "problems": sum(1 for d in rows if d["pill"] == "bad"),
        "updates": sum(1 for d in rows if d.get("updatable")),
        "many_sites": len(sites) > 1,
        "clients": clients if readings.get("clients") is not None else None,
    }
