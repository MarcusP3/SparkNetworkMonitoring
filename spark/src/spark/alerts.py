"""Alerting: deciding what is worth a message, and getting it to Discord.

Two halves, joined by the `notification` table (an outbox):

  * **Deciding** happens where the change happens, in the same transaction:
    the check runner when a target goes down or recovers, the SNMP poller when
    a device stops or starts answering, the sweep when new devices appear. A
    decision is a row. It cannot be lost to a crash between the state change
    and the send, and it cannot be sent for a change that rolled back.

  * **Sending** is a scheduled job that takes what is due and posts it,
    holding no database connection while Discord answers. Failures back off
    and retry; Discord's rate limit is honoured; a burst is sent as one
    message rather than twenty; during quiet hours messages are held and go
    out afterwards as a single digest.

What is deliberately *not* alerted:

  * a target whose failure is explained by one it depends on being down --
    the switch goes, you get one message, not thirty (the incident is still
    recorded, flagged, as before);
  * a recovery whose outage was never alerted, which would read as news about
    something nobody was told had broken;
  * DEGRADED, which is the early warning on the page, not a page for you;
  * an SNMP device that has never answered -- that is a configuration
    problem, shown on the SNMP card, not an outage;
  * an SNMP silence on a device that has a target already reporting it down;
  * the first sweep's devices, which are all "new" and none of them news.
"""

from __future__ import annotations

import logging
import time as _time
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_setting, save_setting, session_scope
from .models import (
    HealthStatus,
    Notification,
    NotificationStatus,
    Target,
    utcnow,
)
from .vault import SecretUnavailable, Vault, vault_for

log = logging.getLogger(__name__)

SETTING = "alerting"
JOB_ID = "alerts:dispatch"
DISPATCH_SECONDS = 15

# Retries, then give up. Minutes rather than seconds after the first: a
# webhook that is failing is usually failing for a reason that takes a while.
BACKOFF_SECONDS = (30, 120, 600, 1800)
MAX_ATTEMPTS = len(BACKOFF_SECONDS) + 1

# A burst larger than this is sent as one combined message. Discord allows a
# webhook about five posts in two seconds; a switch taking out twenty targets
# without dependencies set would otherwise hit it and arrive out of order.
BATCH_AT = 4
MAX_LINES = 20

# The brand's status colours, as Discord embed integers.
TONES = {"bad": 0xF87171, "ok": 0x34D399, "info": 0x8E9BB0}

WEBHOOK_HOSTS = {"discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com"}


class AlertError(ValueError):
    """Something a person typed that cannot be saved. The message is for them."""


# --------------------------------------------------------------------------
# Settings and the webhook
# --------------------------------------------------------------------------


def validate_webhook(raw: str) -> str:
    """A Discord webhook URL, or AlertError saying what is wrong with it.

    Only Discord's own hosts, over https. The URL is fetched by the server,
    so accepting any host would make this form a way to have SPARK send
    requests to arbitrary places on the LAN.
    """
    url = (raw or "").strip()
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or host not in WEBHOOK_HOSTS:
        raise AlertError(
            "That is not a Discord webhook URL. In Discord: Server Settings → "
            "Integrations → Webhooks → Copy Webhook URL."
        )
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) < 4 or segments[:2] != ["api", "webhooks"] or not segments[2].isdigit():
        raise AlertError("That Discord URL is not a webhook — it should contain /api/webhooks/.")
    if parts.username or parts.password or parts.port not in (None, 443):
        raise AlertError("That is not a Discord webhook URL.")
    return f"https://{host}{parts.path}"


async def load(session: AsyncSession) -> dict:
    return await get_setting(session, SETTING)


def webhook_url(settings: dict, vault: Vault) -> str | None:
    sealed = settings.get("discord_webhook_sealed")
    if not sealed:
        return None
    try:
        return vault.open(sealed)
    except SecretUnavailable:
        return None


async def set_webhook(session: AsyncSession, vault: Vault, raw: str | None) -> None:
    """Seal and store a webhook, or remove it with None."""
    settings = await load(session)
    settings.pop("discord_webhook_url", None)
    settings["discord_webhook_sealed"] = vault.seal(validate_webhook(raw)) if raw else ""
    await save_setting(session, SETTING, settings)


async def seal_plaintext_webhook(session: AsyncSession, vault: Vault) -> bool:
    """Move a webhook stored in plaintext by an earlier version into the vault.

    Not a numbered migration: migrations run before the vault exists, and this
    needs the key. Idempotent -- it runs at every start and does nothing once
    the plaintext key is gone. Returns whether it changed anything.
    """
    settings = await load(session)
    if "discord_webhook_url" not in settings:
        return False
    plain = (settings.pop("discord_webhook_url") or "").strip()
    if plain and not settings.get("discord_webhook_sealed"):
        # Sealed as stored, not re-validated: refusing it now would silently
        # drop a webhook that has been working.
        settings["discord_webhook_sealed"] = vault.seal(plain)
    await save_setting(session, SETTING, settings)
    return True


def zone(settings: dict) -> ZoneInfo | timezone:
    try:
        return ZoneInfo(settings.get("timezone") or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def _hhmm(value: str | None) -> time | None:
    try:
        hours, minutes = (value or "").split(":")
        return time(int(hours), int(minutes))
    except ValueError:
        return None


def in_quiet_hours(settings: dict, now: datetime) -> bool:
    """Whether `now` falls in the quiet window, in the saved time zone.

    A window that ends earlier than it starts runs overnight (22:00-07:00).
    Start equal to end, or either unset, means no quiet hours.
    """
    start, end = _hhmm(settings.get("quiet_hours_start")), _hhmm(settings.get("quiet_hours_end"))
    if start is None or end is None or start == end:
        return False
    local = now.astimezone(zone(settings)).time().replace(tzinfo=None)
    if start < end:
        return start <= local < end
    return local >= start or local < end


# --------------------------------------------------------------------------
# Deciding
# --------------------------------------------------------------------------


async def enqueue(
    session: AsyncSession,
    *,
    kind: str,
    subject: str,
    body: str | None = None,
    tone: str = "info",
    incident_id: int | None = None,
    dedupe_key: str | None = None,
) -> Notification | None:
    """Queue one alert in the caller's transaction. None if already queued."""
    if dedupe_key is not None:
        exists = await session.scalar(
            select(Notification.id).where(Notification.dedupe_key == dedupe_key)
        )
        if exists is not None:
            return None
    row = Notification(
        kind=kind, subject=subject[:512], body=body, tone=tone,
        incident_id=incident_id, dedupe_key=dedupe_key,
        status=NotificationStatus.PENDING, next_attempt_at=utcnow(),
    )
    session.add(row)
    return row


async def _queued(session: AsyncSession, key: str) -> bool:
    return await session.scalar(
        select(Notification.id).where(Notification.dedupe_key == key)
    ) is not None


async def on_target_transition(session: AsyncSession, target: Target, transition,  # type: ignore[no-untyped-def]
                               detail: str | None = None) -> None:
    """A target went down or came back. Called in the check's transaction."""
    settings = await load(session)
    if not settings.get("enabled", True):
        return
    now = utcnow()
    if target.muted_until is not None and target.muted_until > now:
        return

    if transition.incident_opened is not None and not transition.suppressed_by_dependency:
        await session.flush()  # the incident's id, for the key
        incident = transition.incident_opened
        lines = [f"`{target.address}`"]
        if detail:
            lines.append(detail)
        lines.append(f"Down after {target.consecutive_failures} failed checks in a row.")
        await enqueue(
            session, kind="down", tone="bad",
            subject=f"{target.name} is down",
            body="\n".join(lines),
            incident_id=incident.id,
            dedupe_key=f"incident:{incident.id}:down",
        )
    elif transition.recovered and transition.incident_closed is not None:
        incident = transition.incident_closed
        if incident.suppressed_by_dependency or not settings.get("notify_on_recovery", True):
            return
        if not await _queued(session, f"incident:{incident.id}:down"):
            return
        from .engine.state import human_duration

        await enqueue(
            session, kind="recovered", tone="ok",
            subject=f"{target.name} is back up",
            body=f"`{target.address}` — down for {human_duration(incident.duration_seconds)}.",
            incident_id=incident.id,
            dedupe_key=f"incident:{incident.id}:up",
        )


def snmp_silence_threshold(interval: int) -> float:
    """How long a device may go unanswered before it is an outage.

    Three polls, and never less than three minutes: one lost UDP datagram is
    not news, and at a 30-second interval three polls is only ninety seconds.
    """
    return max(3 * interval, 180)


async def on_snmp_poll(session: AsyncSession, *, row_id: int, device_id: int,
                       device_name: str, address: str | None, answered: bool,
                       previous_ok: datetime | None, was_failing: bool,
                       now: datetime, interval: int, error: str | None) -> None:
    """A poll finished. Called in the poll's transaction, before it is recorded.

    Keyed by when the device last answered, which stays the same for the whole
    outage: that is what makes "already alerted" a lookup rather than a column.
    """
    if previous_ok is None:
        return  # never answered: a configuration problem, not an outage
    settings = await load(session)
    if not settings.get("enabled", True) or not settings.get("notify_on_snmp", True):
        return
    key = f"snmp:{row_id}:{previous_ok.isoformat()}"

    if not answered:
        silent_for = (now - previous_ok).total_seconds()
        if silent_for < snmp_silence_threshold(interval):
            return
        # A target already down for this device has said it; one message, not two.
        covered = await session.scalar(
            select(Target.id).where(Target.device_id == device_id,
                                    Target.status == HealthStatus.DOWN).limit(1)
        )
        if covered is not None:
            return
        from .engine.state import human_duration

        await enqueue(
            session, kind="snmp_down", tone="bad",
            subject=f"{device_name} stopped answering SNMP",
            body=(f"`{address or '—'}` — no answer for {human_duration(silent_for)}."
                  + (f"\n{error}" if error else "")),
            dedupe_key=f"{key}:down",
        )
    elif was_failing and await _queued(session, f"{key}:down"):
        if not settings.get("notify_on_recovery", True):
            return
        from .engine.state import human_duration

        await enqueue(
            session, kind="snmp_up", tone="ok",
            subject=f"{device_name} is answering SNMP again",
            body=f"`{address or '—'}` — silent for {human_duration((now - previous_ok).total_seconds())}.",
            dedupe_key=f"{key}:up",
        )


async def on_new_devices(session: AsyncSession, devices: list, *, first_sweep: bool) -> None:  # type: ignore[type-arg]
    """New devices from a sweep, in one message. The first sweep is a baseline."""
    if first_sweep or not devices:
        return
    settings = await load(session)
    if not settings.get("enabled", True) or not settings.get("notify_on_new_device", True):
        return
    lines = []
    for device in devices[:15]:
        bits = [f"**{device.display_name}**", f"`{device.primary_ip or '—'}`"]
        if device.vendor:
            bits.append(device.vendor)
        lines.append(" · ".join(bits))
    if len(devices) > 15:
        lines.append(f"…and {len(devices) - 15} more.")
    count = len(devices)
    await enqueue(
        session, kind="new_devices", tone="info",
        subject=f"{count} new device{'s' if count != 1 else ''} on the network",
        body="\n".join(lines),
    )


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------


@dataclass
class _Item:
    id: int
    kind: str
    subject: str
    body: str | None
    tone: str
    created_at: datetime
    attempts: int


@dataclass
class SendResult:
    ok: bool
    error: str | None = None
    retry_after: float | None = None
    permanent: bool = False


def _embed(title: str, body: str | None, tone: str, when: datetime) -> dict:
    embed = {"title": title[:256], "color": TONES.get(tone, TONES["info"]),
             "timestamp": when.astimezone(timezone.utc).isoformat()}
    if body:
        embed["description"] = body[:4000]
    return embed


def payload(embeds: list[dict]) -> dict:
    return {
        "username": "SPARK",
        "embeds": embeds[:10],
        # Never let a device name like "@everyone" ping a server.
        "allowed_mentions": {"parse": []},
    }


def _new_client() -> httpx.AsyncClient:
    """The client used when the caller does not supply one.

    A module attribute so the test suite and smoke test can replace it with
    one that never reaches the internet: their fake webhooks are shaped like
    real ones, and a real POST to Discord from a test run is not acceptable.
    """
    return httpx.AsyncClient(timeout=10.0)


async def post(url: str, body: dict, *, client: httpx.AsyncClient | None = None) -> SendResult:
    """POST one message. Classifies the answer; never raises."""
    own = client is None
    client = client or _new_client()
    try:
        response = await client.post(url, json=body)
    except httpx.HTTPError as exc:
        return SendResult(False, f"{type(exc).__name__}: {exc}".rstrip(": "))
    finally:
        if own:
            await client.aclose()
    if response.status_code < 300:
        return SendResult(True)
    detail = ""
    try:
        data = response.json()
        detail = data.get("message", "") if isinstance(data, dict) else ""
    except ValueError:
        pass
    text = f"Discord answered {response.status_code}{': ' + detail if detail else ''}"
    if response.status_code == 429:
        retry = None
        try:
            retry = float(response.json().get("retry_after"))
        except (ValueError, TypeError, AttributeError):
            retry = float(response.headers.get("retry-after", 5) or 5)
        return SendResult(False, text, retry_after=retry)
    # 4xx other than 429 will not get better by trying again: the webhook was
    # deleted (404), or the URL is wrong (401/403).
    return SendResult(False, text, permanent=400 <= response.status_code < 500)


def _messages(items: list[_Item], held: list[_Item], tz) -> list[tuple[list[int], dict]]:  # type: ignore[no-untyped-def]
    """Group what is due into Discord messages: (row ids, payload) pairs."""
    out: list[tuple[list[int], dict]] = []
    if held:
        lines = [
            f"`{h.created_at.astimezone(tz).strftime('%H:%M')}` {h.subject}"
            for h in held[:MAX_LINES]
        ]
        if len(held) > MAX_LINES:
            lines.append(f"…and {len(held) - MAX_LINES} more.")
        tone = "bad" if any(h.tone == "bad" for h in held) else "info"
        out.append(([h.id for h in held], payload([_embed(
            f"While quiet hours were on: {len(held)} alert{'s' if len(held) != 1 else ''}",
            "\n".join(lines), tone, utcnow())])))
    if len(items) >= BATCH_AT:
        lines = [f"**{i.subject}**" + (f" — {i.body.splitlines()[0]}" if i.body else "")
                 for i in items[:MAX_LINES]]
        if len(items) > MAX_LINES:
            lines.append(f"…and {len(items) - MAX_LINES} more.")
        tone = "bad" if any(i.tone == "bad" for i in items) else "info"
        out.append(([i.id for i in items], payload([_embed(
            f"{len(items)} alerts", "\n".join(lines), tone, utcnow())])))
    else:
        for i in items:
            out.append(([i.id], payload([_embed(i.subject, i.body, i.tone, i.created_at)])))
    return out


def _item(row: Notification) -> _Item:
    return _Item(row.id, row.kind, row.subject, row.body, row.tone, row.created_at, row.attempts)


async def dispatch(config, *, client: httpx.AsyncClient | None = None) -> int:  # type: ignore[no-untyped-def]
    """Send what is due. The scheduler's job. Returns how many rows it settled."""
    try:
        return await _dispatch(config, client)
    except Exception:  # noqa: BLE001 - the scheduler must keep running
        log.exception("Alert dispatch failed")
        return 0


async def _dispatch(config, client) -> int:  # type: ignore[no-untyped-def]
    now = utcnow()
    # ---- decide what goes out, then let go of the database ----
    async with session_scope() as session:
        settings = await load(session)
        due = list((await session.execute(
            select(Notification)
            .where(Notification.status == NotificationStatus.PENDING,
                   Notification.next_attempt_at <= now)
            .order_by(Notification.id).limit(50)
        )).scalars())
        quiet = in_quiet_hours(settings, now)
        held = [] if quiet else list((await session.execute(
            select(Notification).where(Notification.status == NotificationStatus.HELD)
            .order_by(Notification.id).limit(200)
        )).scalars())
        if not due and not held:
            return 0

        url = webhook_url(settings, vault_for(config))
        reason = None
        if not settings.get("enabled", True):
            reason = "Alerting was switched off."
        elif url is None:
            reason = "No Discord webhook is configured."
        if reason:
            for row in due + held:
                row.status = NotificationStatus.DROPPED
                row.error = reason
            return len(due) + len(held)

        if quiet:
            for row in due:
                row.status = NotificationStatus.HELD
            return len(due)

        items, held_items = [_item(r) for r in due], [_item(r) for r in held]
        tz = zone(settings)

    # ---- send, holding nothing ----
    results: list[tuple[list[int], SendResult]] = []
    for ids, body in _messages(items, held_items, tz):
        result = await post(url, body, client=client)  # type: ignore[arg-type]
        results.append((ids, result))
        if result.retry_after is not None:
            break  # rate limited: everything after this waits too

    # ---- record ----
    sent_at = utcnow()
    touched = {i for ids, _ in results for i in ids}
    async with session_scope() as session:
        rows = {r.id: r for r in (await session.execute(
            select(Notification).where(Notification.id.in_(touched | {i.id for i in items}))
        )).scalars()}
        for ids, result in results:
            for row_id in ids:
                row = rows.get(row_id)
                if row is None:
                    continue
                row.attempts += 1
                if result.ok:
                    row.status, row.sent_at, row.error = NotificationStatus.SENT, sent_at, None
                    continue
                row.error = result.error
                if result.permanent or row.attempts >= MAX_ATTEMPTS:
                    row.status = NotificationStatus.FAILED
                else:
                    # A held row whose digest failed retries on its own.
                    row.status = NotificationStatus.PENDING
                    wait = result.retry_after if result.retry_after is not None \
                        else BACKOFF_SECONDS[min(row.attempts - 1, len(BACKOFF_SECONDS) - 1)]
                    row.next_attempt_at = sent_at + timedelta(seconds=wait)
        # Items never attempted because a 429 stopped the loop wait for it too.
        if results and results[-1][1].retry_after is not None:
            later = sent_at + timedelta(seconds=results[-1][1].retry_after or 5)
            for item in items:
                if item.id not in touched and item.id in rows:
                    rows[item.id].next_attempt_at = later
    return len(touched)


async def send_test(session: AsyncSession, vault: Vault) -> SendResult:
    """Send a test message now, and log it. For the Settings button.

    Commits before the network call, like SNMP Test: the request is not left
    holding its transaction while Discord answers.
    """
    settings = await load(session)
    url = webhook_url(settings, vault)
    if url is None:
        return SendResult(False, "Save a Discord webhook first.")
    await session.commit()
    started = _time.monotonic()
    result = await post(url, payload([_embed(
        "SPARK test alert",
        "If you can read this, alerts will reach this channel.",
        "info", utcnow())]))
    log.info("Test alert: %s in %.1fs", "sent" if result.ok else result.error,
             _time.monotonic() - started)
    session.add(Notification(
        kind="test", subject="Test alert", tone="info",
        status=NotificationStatus.SENT if result.ok else NotificationStatus.FAILED,
        attempts=1, sent_at=utcnow() if result.ok else None, error=result.error,
    ))
    return result


async def recent(session: AsyncSession, limit: int = 15) -> list[Notification]:
    return list((await session.execute(
        select(Notification).order_by(Notification.id.desc()).limit(limit)
    )).scalars())


async def counts(session: AsyncSession) -> dict[str, int]:
    rows = await session.execute(
        select(Notification.status, func.count()).group_by(Notification.status)
    )
    return {str(status): n for status, n in rows.all()}


__all__ = [
    "AlertError", "dispatch", "enqueue", "in_quiet_hours", "on_new_devices",
    "on_snmp_poll", "on_target_transition", "post", "seal_plaintext_webhook",
    "send_test", "set_webhook", "validate_webhook", "webhook_url",
]
