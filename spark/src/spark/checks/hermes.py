"""Hermes Agent, through its dashboard's /api/status.

Hermes (Nous Research's agent) publishes that endpoint without sign-in for
exactly this, uptime probes: its version, the gateway's state, each
messaging platform's connection, a health rollup, and memory and disk
pressure -- no session content, no secrets. So the check needs no
credential, only the address the dashboard listens on.

What the answer means for the target (`problems`):

- down: no answer, or not Hermes; the gateway not running (stopped, or
  failed to start); a messaging platform failed for good ("fatal": a token
  refused, two gateways holding one bot); the Nous sign-in ended, after
  which every turn fails while everything else looks healthy; or its
  session database corrupt.
- degraded: the rest of what Hermes itself counts against `overall` -- a
  platform retrying or disconnected, the gateway's heartbeat stale (its
  loop wedged), storage unhealthy -- and memory or disk pressure at
  critical, which Hermes leaves out of `overall` as advisory.
- up: otherwise.

The part the Hermes card shows is kept on the target (`readings`), trimmed
to it: never the paths, process id and ports Hermes adds on a loopback bind.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

from .base import CheckOutcome, CheckSpec, describe_exception

STATUS_PATH = "/api/status"
DEFAULT_PORT = 9119
# A status document is a few kilobytes; this is for the server nobody expected.
MAX_BODY = 256 * 1024
MAX_PLATFORMS = 40
MAX_TEXT = 200

HEALTHY = frozenset({"connected", "running", "ok"})
# Platforms the gateway deliberately does not run for a profile: never a problem.
OFF = frozenset({"disabled"})
NAMES = {
    "api_server": "API server", "whatsapp": "WhatsApp", "whatsapp_cloud": "WhatsApp Cloud",
    "sms": "SMS", "msgraph_webhook": "Microsoft Graph", "dingtalk": "DingTalk",
    "wecom": "WeCom", "wecom_callback": "WeCom callback", "bluebubbles": "BlueBubbles",
    "qqbot": "QQ bot",
}
GATEWAY_WORDS = {"startup_failed": "failed to start", "stopped": "is stopped"}


def status_url(address: str) -> str:
    """The status URL for a dashboard address: a URL (its path is kept, for
    a dashboard behind a proxy), or host or host:port, on 9119 by default."""
    raw = address.strip().rstrip("/")
    if "://" not in raw:
        if raw.startswith("["):
            host = raw if "]:" in raw else f"{raw}:{DEFAULT_PORT}"
        elif raw.count(":") > 1:                           # a bare IPv6 address
            host = f"[{raw}]:{DEFAULT_PORT}"
        else:
            name, sep, port = raw.rpartition(":")
            host = raw if sep and port.isdigit() and name else f"{raw}:{DEFAULT_PORT}"
        raw = f"http://{host}"
    return raw if raw.endswith(STATUS_PATH) else raw + STATUS_PATH


async def check_hermes(spec: CheckSpec) -> CheckOutcome:
    import httpx

    url = status_url(spec.address)
    verify = bool(spec.param("verify_tls", True))
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=spec.timeout_seconds, verify=verify,
                                     follow_redirects=True) as client:
            async with client.stream("GET", url, headers={"Accept": "application/json"}) as response:
                body, whole = await _read(response, MAX_BODY)
        latency = (time.monotonic() - started) * 1000
    except Exception as exc:  # noqa: BLE001
        return CheckOutcome.down(describe_exception(exc))

    if response.status_code == 400 and b"Invalid Host header" in body:
        return CheckOutcome.down(
            "Hermes refused the address: use the name or address its dashboard listens on "
            "(its --host), or its dashboard.public_url", latency_ms=latency)
    if response.status_code != 200:
        return CheckOutcome.down(f"HTTP {response.status_code} from {STATUS_PATH}", latency_ms=latency)
    if not whole:
        return CheckOutcome.down(f"not a Hermes dashboard: the answer is over {MAX_BODY // 1024} KB",
                                 latency_ms=latency)
    try:
        status = json.loads(body)
    except ValueError:
        return CheckOutcome.down("not a Hermes dashboard: the answer is not JSON", latency_ms=latency)
    if not isinstance(status, dict) or "gateway_running" not in status:
        return CheckOutcome.down("not a Hermes dashboard: there is no gateway in the answer",
                                 latency_ms=latency)
    outcome = judge(status, latency_ms=latency)
    outcome.readings = summary(status)
    return outcome


def likely(port: int, name: str | None) -> bool:
    """Worth asking whether a discovered service is Hermes: its port, or its name."""
    return port == DEFAULT_PORT or "hermes" in (name or "").lower()


async def answers_as_hermes(address: str, timeout: float = 10.0) -> bool:
    """Whether a Hermes dashboard answers there: what Watch asks before it
    makes a Hermes check rather than a TCP one."""
    return (await check_hermes(CheckSpec(address=address, timeout_seconds=timeout))).readings is not None


async def _read(response: Any, limit: int) -> tuple[bytes, bool]:
    """Up to `limit` bytes of the body, and whether that was all of it."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            return b"".join(chunks)[:limit], False
    return b"".join(chunks), True


# --------------------------------------------------------------------------
# What the answer means
# --------------------------------------------------------------------------


def _text(value: Any, limit: int = MAX_TEXT) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    text = " ".join(str(value).split())
    return text[:limit] or None


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return None


def _label(key: str) -> str:
    """telegram -> Telegram; a profile's own, alpha:discord -> Discord (alpha)."""
    profile, _, name = key.rpartition(":")
    words = NAMES.get(name) or name.replace("_", " ").title()
    return f"{words} ({profile})" if profile else words


def platforms(status: dict) -> list[dict]:
    """Each messaging platform: its label, state and the error it gave."""
    found = status.get("gateway_platforms")
    if not isinstance(found, dict):
        return []
    rows = []
    for key, entry in sorted(found.items(), key=lambda kv: str(kv[0]))[:MAX_PLATFORMS]:
        if not isinstance(entry, dict):
            continue
        state = (_text(entry.get("state") or entry.get("status"), 24) or "unknown").lower()
        rows.append({"key": _text(key, 64), "label": _label(str(key))[:80], "state": state,
                     "error": _text(entry.get("error_message") or entry.get("error_code"))})
    return rows


def problems(status: dict) -> tuple[list[str], list[str]]:
    """(why it is down, why it is degraded), most telling first."""
    from ..engine.state import human_duration

    down: list[str] = []
    warn: list[str] = []
    running = status.get("gateway_running") is True
    state = _text(status.get("gateway_state"), 24)
    if not running:
        why = _text(status.get("gateway_exit_reason"))
        words = GATEWAY_WORDS.get(state or "", "is not running")
        down.append(f"the gateway {words}" + (f": {why}" if why else ""))
    elif state in ("degraded", "starting", "draining"):
        warn.append(f"the gateway is {state}")
    stale = status.get("gateway_heartbeat_stale_s")
    if running and isinstance(stale, (int, float)) and not isinstance(stale, bool) and stale > 0:
        warn.append(f"the gateway has not checked in for {human_duration(stale)}")
    for p in platforms(status):
        error = f": {p['error']}" if p["error"] else ""
        if p["state"] == "fatal":
            down.append(f"{p['label']} failed{error}")
        elif p["state"] not in HEALTHY | OFF:
            warn.append(f"{p['label']} is {p['state']}{error}")
    if status.get("nous_session_valid") == "terminal":
        down.append("its Nous sign-in has ended: every turn fails until it signs in again")
    components = status.get("components") if isinstance(status.get("components"), dict) else {}
    storage = components.get("storage") if isinstance(components.get("storage"), dict) else {}
    if storage.get("reason") == "corrupt":
        down.append("its session database is corrupt")
    elif storage.get("status") not in (None, "ok"):
        warn.append("its storage is unhealthy")
    dashboard = components.get("dashboard") if isinstance(components.get("dashboard"), dict) else {}
    if dashboard.get("status") not in (None, "ok"):
        warn.append("its dashboard reports a problem")
    for key in ("memory", "disk"):
        block = status.get(key) if isinstance(status.get(key), dict) else {}
        if block.get("pressure") == "critical":
            free = _int(block.get("system_available_mb") if key == "memory" else block.get("free_mb"))
            warn.append(f"{key} is critical" + (f" ({free} MB free)" if free is not None else ""))
    if not down and not warn and status.get("overall") == "degraded":
        warn.append("Hermes reports itself degraded")
    return down, warn


def _joined(reasons: list[str]) -> str:
    text = "; ".join(reasons[:3])
    if len(reasons) > 3:
        text += f"; and {len(reasons) - 3} more"
    return text[:500]


def _healthy(status: dict) -> str:
    version = _text(status.get("version"), 32)
    shown = [p for p in platforms(status) if p["state"] not in OFF]
    sessions = _int(status.get("active_sessions"))
    parts = ["gateway running"]
    if shown:
        connected = sum(1 for p in shown if p["state"] in HEALTHY)
        parts.append(f"{connected} of {len(shown)} platform{'s' if len(shown) != 1 else ''} connected")
    if sessions is not None:
        parts.append(f"{sessions} active session{'s' if sessions != 1 else ''}")
    return (f"Hermes {version}: " if version else "Hermes: ") + ", ".join(parts)


def judge(status: dict, latency_ms: float | None = None) -> CheckOutcome:
    down, warn = problems(status)
    if down:
        return CheckOutcome.down(_joined(down + warn), latency_ms=latency_ms)
    if warn:
        return CheckOutcome.warn(_joined(warn), latency_ms=latency_ms)
    return CheckOutcome.up(latency_ms=latency_ms, detail=_healthy(status))


def summary(status: dict, now: datetime | None = None) -> dict:
    """What the card shows, and nothing else the answer carried."""
    down, warn = problems(status)
    memory = status.get("memory") if isinstance(status.get("memory"), dict) else {}
    disk = status.get("disk") if isinstance(status.get("disk"), dict) else {}
    components = status.get("components") if isinstance(status.get("components"), dict) else {}
    used = disk.get("used_percent")
    return {
        "read_at": (now or datetime.now(timezone.utc)).isoformat(),
        "version": _text(status.get("version"), 32),
        "release_date": _text(status.get("release_date"), 32),
        "overall": _text(status.get("overall"), 16),
        "gateway": {"running": status.get("gateway_running") is True,
                    "state": _text(status.get("gateway_state"), 24),
                    "exit_reason": _text(status.get("gateway_exit_reason"))},
        "sessions": _int(status.get("active_sessions")),
        "agents": _int(status.get("active_agents")),
        "platforms": platforms(status),
        "storage": _text((components.get("storage") or {}).get("status")
                         if isinstance(components.get("storage"), dict) else None, 16),
        "memory": {"pressure": _text(memory.get("pressure"), 16),
                   "available_mb": _int(memory.get("system_available_mb")),
                   "total_mb": _int(memory.get("system_total_mb"))},
        "disk": {"pressure": _text(disk.get("pressure"), 16),
                 "free_mb": _int(disk.get("free_mb")), "total_mb": _int(disk.get("total_mb")),
                 "used_percent": round(float(used), 1) if isinstance(used, (int, float))
                 and not isinstance(used, bool) else None},
        "nous": _text(status.get("nous_session_valid"), 16),
        "config": {"current": _int(status.get("config_version")),
                   "latest": _int(status.get("latest_config_version"))},
        "signed_in_only": status.get("auth_required") is True,
        "down": down, "warn": warn,
    }


# --------------------------------------------------------------------------
# The card
# --------------------------------------------------------------------------


def _pill(state: str | None) -> str:
    if state in HEALTHY:
        return "ok"
    if state in OFF or state in (None, "unknown"):
        return "neutral"
    return "bad" if state in ("fatal", "critical", "corrupt", "startup_failed", "stopped") else "warn"


def view(readings: dict | None) -> dict | None:
    """The Hermes card from a target's last readings, or None before the first."""
    if not readings or not readings.get("read_at"):
        return None
    gateway = readings.get("gateway") or {}
    state = gateway.get("state") or ("running" if gateway.get("running") else "stopped")
    nous = readings.get("nous")
    config = readings.get("config") or {}
    current, latest = config.get("current"), config.get("latest")
    try:
        read_at = datetime.fromisoformat(readings["read_at"])
    except (TypeError, ValueError):
        read_at = None
    return {
        **readings,
        "read_at": read_at,
        "gateway_state": state,
        "gateway_pill": _pill(state if gateway.get("running") else "stopped"),
        "platforms": [dict(p, pill=_pill(p.get("state"))) for p in readings.get("platforms") or []],
        "storage_pill": _pill(readings.get("storage")),
        "memory_pill": _pill((readings.get("memory") or {}).get("pressure")),
        "disk_pill": _pill((readings.get("disk") or {}).get("pressure")),
        "nous_words": {"valid": "signed in", "terminal": "ended"}.get(nous or ""),
        "nous_pill": {"valid": "ok", "terminal": "bad"}.get(nous or "", "neutral"),
        "config_behind": current is not None and latest is not None and current < latest,
    }
