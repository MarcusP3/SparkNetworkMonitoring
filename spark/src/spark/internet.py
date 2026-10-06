"""The internet, checked every minute: the Dashboard's Internet card.

Several small checks rather than one ping, so the card can say *what* is
wrong, not only that something is:

  * **Reachability**: ping three public resolvers run by different
    providers (Cloudflare, Google, Quad9). The internet is down only when
    none of them answers *and* the web check fails too: one provider's
    blip, or a network that drops ICMP, is not an outage.
  * **Quality**: latency, jitter and packet loss, from the resolvers that
    answered.
  * **DNS**: one name looked up through the network's own resolver and
    through a public one, so "DNS is broken" reads differently from "the
    internet is down" -- in a browser they look the same.
  * **Web**: an HTTPS fetch of a page made for connectivity checks, which
    proves traffic really gets out.
  * **The gateway**: pinged too, when a device has the Gateway / router
    role. If it does not answer either, the problem is at home, and when a
    target already watches the gateway its alert is the one sent.

Up, degraded (reachable, but loss, DNS or the web check is off) or down.
"The internet is down" alerts after two checks in a row (Settings ->
Alerts), and again when it is back; the outage is an incident on the
Dashboard like any other. Each check is one InternetSample row, kept 30
days. The checks can be switched off on the card: SPARK then contacts none
of these hosts.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import alerts, snmp_alerts
from .checks.base import CheckSpec, describe_exception
from .checks.net import run_check
from .db import get_setting, save_setting, session_scope
from .engine.state import human_duration
from .models import (AlertIncident, Device, DeviceRole, HealthStatus, InternetSample, Target,
                     utcnow)

log = logging.getLogger(__name__)

JOB_ID = "internet"
INTERVAL_SECONDS = 60
FIRST_RUN_SECONDS = 20
SETTING = "internet"

# Three providers, so one of them failing is not "the internet".
RESOLVERS = (("Cloudflare", "1.1.1.1"), ("Google", "8.8.8.8"), ("Quad9", "9.9.9.9"))
DNS_NAME = "example.com"          # IANA's reserved name: always there
PUBLIC_DNS = "1.1.1.1"
WEB_URL = "https://www.gstatic.com/generate_204"   # answers 204, made for this
WEB_STATUS = 204

PING_COUNT = 4
PING_INTERVAL = 0.25
PING_TIMEOUT = 2.0
CHECK_TIMEOUT = 5.0

LOSS_WARN_PERCENT = 2.0           # degraded above this
DOWN_CHECKS = 2                   # down this many checks in a row: alert
ALERT_KEY = "internet"
KEEP_DAYS = 30

UP, DEGRADED, DOWN = "up", "degraded", "down"


@dataclass
class Ping:
    alive: bool
    avg_ms: float | None = None
    jitter_ms: float | None = None
    loss_percent: float = 100.0


@dataclass
class Reading:
    state: str
    reasons: list[str] = field(default_factory=list)
    latency_ms: float | None = None
    jitter_ms: float | None = None
    loss_percent: float | None = None
    pings: dict[str, float | None] = field(default_factory=dict)
    dns_local_ok: bool | None = None
    dns_public_ok: bool | None = None
    web_ok: bool | None = None
    web_ms: float | None = None
    gateway_ok: bool | None = None

    @property
    def detail(self) -> str:
        return "; ".join(self.reasons)


# --------------------------------------------------------------------------
# The checks
# --------------------------------------------------------------------------


async def ping(address: str) -> Ping:
    """Echoes to one address: average, jitter and loss. Never raises.
    Privileged ICMP first, then unprivileged, as checks.net.check_ping."""
    try:
        from icmplib import async_ping
    except ImportError:  # pragma: no cover - dependency is declared
        return Ping(alive=False)
    for privileged in (True, False):
        try:
            host = await async_ping(address, count=PING_COUNT, interval=PING_INTERVAL,
                                    timeout=PING_TIMEOUT, privileged=privileged)
        except Exception as exc:  # noqa: BLE001 - never raise out of a check
            log.debug("Internet: ping %s: %s", address, describe_exception(exc))
            continue
        if not host.is_alive:
            return Ping(alive=False)
        return Ping(alive=True, avg_ms=float(host.avg_rtt),
                    jitter_ms=float(getattr(host, "jitter", 0.0) or 0.0),
                    loss_percent=float(host.packet_loss) * 100)
    return Ping(alive=False)


def decide(pings: dict[str, Ping], *, dns_local: bool, dns_public: bool, web: bool,
           web_ms: float | None = None, gateway: Ping | None = None) -> Reading:
    """Up, degraded or down, and why, from one round of checks. Pure."""
    answered = {a: p for a, p in pings.items() if p.alive}
    reading = Reading(
        state=UP,
        pings={a: (round(p.avg_ms, 1) if p.alive and p.avg_ms is not None else None)
               for a, p in pings.items()},
        dns_local_ok=dns_local, dns_public_ok=dns_public, web_ok=web,
        web_ms=round(web_ms, 1) if web and web_ms is not None else None,
        gateway_ok=None if gateway is None else gateway.alive,
    )
    if answered:
        n = len(answered)
        reading.latency_ms = round(sum(p.avg_ms or 0 for p in answered.values()) / n, 1)
        reading.jitter_ms = round(sum(p.jitter_ms or 0 for p in answered.values()) / n, 1)
        reading.loss_percent = round(sum(p.loss_percent for p in answered.values()) / n, 1)

    if not answered and not web:
        reading.state = DOWN
        reading.reasons.append("No answer from " + ", ".join(pings) + ", and the web check failed")
        if gateway is not None and not gateway.alive:
            reading.reasons.append("the gateway did not answer either")
        return reading

    if reading.loss_percent is not None and reading.loss_percent > LOSS_WARN_PERCENT:
        reading.reasons.append(f"{reading.loss_percent:.0f}% packet loss")
    if not web:
        reading.reasons.append("the web check failed")
    if not dns_local and not dns_public:
        reading.reasons.append("DNS is not answering")
    elif not dns_local:
        reading.reasons.append("your DNS is not answering (a public resolver is)")
    elif not dns_public:
        reading.reasons.append("a public DNS resolver is not answering (yours is)")
    if not answered:
        reading.reasons.append("no resolver answered a ping (ICMP may be blocked)")
    if reading.reasons:
        reading.state = DEGRADED
    return reading


async def probe(gateway_ip: str | None = None) -> Reading:
    """Every check at once; a round takes a few seconds."""
    addresses = [a for _, a in RESOLVERS]
    tasks = [ping(a) for a in addresses]
    tasks.append(run_check("dns", CheckSpec(DNS_NAME, CHECK_TIMEOUT)))
    tasks.append(run_check("dns", CheckSpec(DNS_NAME, CHECK_TIMEOUT, {"server": PUBLIC_DNS})))
    tasks.append(run_check("http", CheckSpec(WEB_URL, CHECK_TIMEOUT,
                                             {"expect_status": WEB_STATUS})))
    if gateway_ip:
        tasks.append(ping(gateway_ip))
    results = await asyncio.gather(*tasks)
    pings = dict(zip(addresses, results[:len(addresses)]))
    dns_local, dns_public, web = results[len(addresses):len(addresses) + 3]
    gateway = results[-1] if gateway_ip else None
    return decide(pings, dns_local=dns_local.ok, dns_public=dns_public.ok, web=web.ok,
                  web_ms=web.latency_ms, gateway=gateway)


# --------------------------------------------------------------------------
# Every minute
# --------------------------------------------------------------------------


async def enabled(session: AsyncSession) -> bool:
    return bool((await get_setting(session, SETTING)).get("enabled", True))


async def set_enabled(session: AsyncSession, on: bool) -> None:
    """Switching off also closes an outage standing open: no checks, no
    alert, and nothing left claiming the internet is down."""
    settings = await get_setting(session, SETTING)
    await save_setting(session, SETTING, {**settings, "enabled": bool(on)})
    if not on:
        await snmp_alerts.forget(session, ALERT_KEY)


async def _gateway(session: AsyncSession) -> Device | None:
    return await session.scalar(
        select(Device).where(Device.role == DeviceRole.GATEWAY, Device.ignored.is_(False),
                             Device.primary_ip.is_not(None)).order_by(Device.id).limit(1))


async def run(config=None) -> Reading | None:  # type: ignore[no-untyped-def]
    """The scheduler's job. Never raises."""
    try:
        async with session_scope() as session:
            if not await enabled(session):
                return None
            gateway = await _gateway(session)
            gateway_ip = gateway.primary_ip if gateway else None
        reading = await probe(gateway_ip)
        async with session_scope() as session:
            await record(session, reading, utcnow())
        return reading
    except Exception:  # noqa: BLE001 - the scheduler must keep running
        log.exception("Internet check failed")
        return None


async def record(session: AsyncSession, reading: Reading, now: datetime) -> None:
    """Store the reading and move the alert."""
    session.add(InternetSample(
        ts=now, state=reading.state, latency_ms=reading.latency_ms,
        jitter_ms=reading.jitter_ms, loss_percent=reading.loss_percent,
        pings=reading.pings, dns_local_ok=reading.dns_local_ok,
        dns_public_ok=reading.dns_public_ok, web_ok=reading.web_ok, web_ms=reading.web_ms,
        gateway_ok=reading.gateway_ok, detail=reading.detail[:1000] or None))

    rules = await snmp_alerts.load(session)
    settings = await alerts.load(session)
    on = bool(rules.get("internet_down", True))
    subject = "The internet is down"
    body = reading.detail + "."
    out = await snmp_alerts.step(
        session, ALERT_KEY, breached=reading.state == DOWN, cleared=reading.state != DOWN,
        value=None, now=now, hold=timedelta(0), min_polls=DOWN_CHECKS,
        interval=INTERVAL_SECONDS, record=snmp_alerts.Record(None, subject, body) if on else None)
    sending = settings.get("enabled", True)
    if out.fired and on and sending:
        if reading.gateway_ok is False and await _gateway_alerting(session):
            # The gateway's own target says it already: one alert, not two.
            return
        await alerts.enqueue(session, kind="internet_down", tone="bad", subject=subject, body=body,
                             dedupe_key=f"rule:{ALERT_KEY}:{out.since.isoformat()}:fire")
        await snmp_alerts._mark_notified(session, ALERT_KEY)
    elif out.cleared and out.notified and settings.get("notify_on_recovery", True) and sending:
        took = human_duration((now - out.since).total_seconds())
        await alerts.enqueue(session, kind="internet_ok", tone="ok",
                             subject="The internet is back", body=f"It was down for {took}.",
                             dedupe_key=f"rule:{ALERT_KEY}:{out.since.isoformat()}:clear")


async def _gateway_alerting(session: AsyncSession) -> bool:
    """A target watching the gateway is down, so its alert covers this."""
    gateway = await _gateway(session)
    if gateway is None:
        return False
    return bool(await session.scalar(select(Target.id).where(
        Target.device_id == gateway.id, Target.enabled.is_(True),
        Target.status == HealthStatus.DOWN).limit(1)))


async def prune(session: AsyncSession, now: datetime) -> int:
    """Samples past KEEP_DAYS, for the nightly retention run."""
    from sqlalchemy import delete
    result = await session.execute(
        delete(InternetSample).where(InternetSample.ts < now - timedelta(days=KEEP_DAYS)))
    return result.rowcount or 0


# --------------------------------------------------------------------------
# The card
# --------------------------------------------------------------------------

STATE_WORDS = {UP: ("ok dot", "Online"), DEGRADED: ("warn dot", "Degraded"),
               DOWN: ("bad dot", "Down")}


async def _uptime(session: AsyncSession, since: datetime) -> float | None:
    """The share of checks since `since` that were not down, in percent."""
    total = await session.scalar(select(func.count(InternetSample.id))
                                 .where(InternetSample.ts >= since)) or 0
    if not total:
        return None
    down = await session.scalar(select(func.count(InternetSample.id)).where(
        InternetSample.ts >= since, InternetSample.state == DOWN)) or 0
    return round(100.0 * (total - down) / total, 2)


async def view(session: AsyncSession, now: datetime | None = None) -> dict:
    """What the Dashboard's Internet card shows."""
    from .charts import trend

    now = now or utcnow()
    on = await enabled(session)
    last = await session.scalar(select(InternetSample).order_by(InternetSample.ts.desc()).limit(1))
    day = list((await session.execute(
        select(InternetSample.latency_ms, InternetSample.state)
        .where(InternetSample.ts >= now - timedelta(hours=24)).order_by(InternetSample.ts)
    )).all())
    # One point per ~15 minutes is plenty for a strip this size.
    step = max(1, len(day) // 96)
    series = [r.latency_ms for r in day[::step]]
    outages = list((await session.execute(
        select(AlertIncident).where(AlertIncident.key == ALERT_KEY)
        .order_by(AlertIncident.opened_at.desc()).limit(5))).scalars())
    pill, words = STATE_WORDS.get(last.state, ("neutral", "Checking…")) if last else (
        "neutral", "Checking…")
    return {
        "enabled": on,
        "last": last,
        "pill": pill if on else "neutral",
        "words": words if on else "Off",
        "resolvers": [(name, address, (last.pings or {}).get(address) if last else None)
                      for name, address in RESOLVERS],
        "trend": trend(series, last.state if last else "idle") if series else None,
        "uptime_day": await _uptime(session, now - timedelta(hours=24)),
        "uptime_week": await _uptime(session, now - timedelta(days=7)),
        "outages": [{"opened_at": o.opened_at, "closed_at": o.closed_at,
                     "took": human_duration(((o.closed_at or now) - o.opened_at).total_seconds())}
                    for o in outages],
        "web_url": WEB_URL, "dns_name": DNS_NAME, "public_dns": PUBLIC_DNS,
        "stale": bool(on and last and (now - last.ts).total_seconds() > 5 * INTERVAL_SECONDS),
    }
