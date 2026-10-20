"""Alerting: deciding what is worth a message, and getting it to Discord,
ntfy, email or a webhook (channels.py).

Two halves, joined by the `notification` table (an outbox):

  * **Deciding** happens where the change happens, in the same transaction:
    the check runner when a target goes down or recovers, the SNMP poller when
    a device stops or starts answering, the sweep when new devices appear. A
    decision is a row. It cannot be lost to a crash between the state change
    and the send, and it cannot be sent for a change that rolled back.

  * **Sending** is a scheduled job that takes what is due and posts it,
    holding no database connection while a channel answers. Each alert gets
    one delivery per channel that takes it (models.Delivery), and each is
    sent on its own: failures back off and retry, a rate limit is honoured
    per channel, a burst is sent as one message rather than twenty, and
    during quiet hours messages are held and go out afterwards as a single
    digest.

What is deliberately *not* alerted:

  * anything on the mute list (Settings -> Alerts): a muted device, or a
    muted target, is still checked and shown, but sends nothing;
  * anything under a maintenance window (maintenance.py), likewise -- until
    the window ends, when what is still broken is sent;

  * a target whose failure is explained by one it depends on being down, or
    by a device above it on the service map being down -- the switch goes,
    you get one message, not thirty (the incident is still recorded,
    flagged, as before);
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
from datetime import datetime, time, timedelta, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import channels, prefs
from .channels import SendResult
from .db import get_setting, save_setting, session_scope
from .models import (
    AlertMute,
    Delivery,
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


async def is_muted(session: AsyncSession, *, device_id: int | None = None,
                   target_id: int | None = None) -> bool:
    """On the mute list: the device itself, or the target itself.

    A target on a muted device is muted too -- muting a device means "never
    tell me about this box", whichever part of SPARK noticed.
    """
    clauses = []
    if device_id is not None:
        clauses.append(AlertMute.device_id == device_id)
    if target_id is not None:
        clauses.append(AlertMute.target_id == target_id)
    if not clauses:
        return False
    from sqlalchemy import or_

    return await session.scalar(select(AlertMute.id).where(or_(*clauses)).limit(1)) is not None


async def is_quiet(session: AsyncSession, *, device_id: int | None = None,
                   target_id: int | None = None, when: datetime | None = None) -> bool:
    """Not to be sent about now: on the mute list, or under a maintenance
    window (maintenance.py) at `when`. What every alert asks; the mute list
    alone is `is_muted`, which is what the device page's button shows."""
    if await is_muted(session, device_id=device_id, target_id=target_id):
        return True
    from .maintenance import covering

    return await covering(session, device_id, when) is not None


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
    if await is_quiet(session, device_id=target.device_id, target_id=target.id, when=now):
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
    from .snmp_alerts import Record, close_incidents, open_incident

    incident = f"snmpdown:{row_id}"
    if answered:
        await close_incidents(session, now, key=incident)
    if previous_ok is None:
        return  # never answered: a configuration problem, not an outage
    settings = await load(session)
    if not settings.get("notify_on_snmp", True):
        return
    from .suppressions import for_device

    if (await for_device(session, device_id)).off("snmp_down"):
        return  # suppressed for this device: fully quiet
    # Muted, or alerts switched off: the outage is still an incident on the
    # dashboard, only no message is sent.
    sending = settings.get("enabled", True) and not await is_quiet(session, device_id=device_id,
                                                                   when=now)
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
        # Something above it on the service map is down: that explains it.
        from .hierarchy import upstream_is_down

        if await upstream_is_down(session, device_id):
            return
        from .engine.state import human_duration

        subject = f"{device_name} stopped answering SNMP"
        body = (f"`{address or '—'}` — no answer for {human_duration(silent_for)}."
                + (f"\n{error}" if error else ""))
        await open_incident(session, incident, Record(device_id, subject, body), previous_ok)
        if sending:
            await enqueue(session, kind="snmp_down", tone="bad", subject=subject, body=body,
                          dedupe_key=f"{key}:down")
    elif was_failing and await _queued(session, f"{key}:down"):
        if not sending or not settings.get("notify_on_recovery", True):
            return
        from .engine.state import human_duration

        await enqueue(
            session, kind="snmp_up", tone="ok",
            subject=f"{device_name} is answering SNMP again",
            body=f"`{address or '—'}` — silent for {human_duration((now - previous_ok).total_seconds())}.",
            dedupe_key=f"{key}:up",
        )


async def on_security_event(session: AsyncSession, *, kind: str, subject: str, body: str,
                            tone: str = "bad", dedupe_key: str | None = None) -> None:
    """Something happened to the sign-in that the owner would want to know.

    Detective control for the account: the lockout tripping (someone is
    guessing), a sign-in from an address that has never held a session
    before (was that you?), a password change or reset (if not you, it is
    too late for the old one), and first-run setup completing (on a fresh
    install, that it was you). Called in the transaction that recorded the
    event; the dispatcher sends. Off under Settings -> Alerts.
    """
    settings = await load(session)
    if not settings.get("enabled", True) or not settings.get("notify_on_security", True):
        return
    await enqueue(session, kind=kind, tone=tone, subject=subject[:512], body=body,
                  dedupe_key=dedupe_key)


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
    Every channel that posts over HTTP (channels.py) takes it from here.
    """
    return httpx.AsyncClient(timeout=10.0)


async def post(url: str, body: dict, *, client: httpx.AsyncClient | None = None) -> SendResult:
    """POST one message to Discord. Classifies the answer; never raises."""
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


OFF_REASON = "Alerting was switched off."
NO_CHANNEL_REASON = "No Discord webhook or other channel is set up."
NOT_TAKEN_REASON = "No channel takes this kind of alert."
GONE_REASON = "That channel was removed."


def _item(delivery: Delivery, row: Notification) -> channels.Item:
    return channels.Item(delivery.id, row.kind, row.subject, row.body, row.tone, row.created_at,
                         delivery.attempts, row.incident_id)


async def dispatch(config, *, client: httpx.AsyncClient | None = None) -> int:  # type: ignore[no-untyped-def]
    """Send what is due. The scheduler's job. Returns how many deliveries it
    settled (or alerts it dropped before any channel)."""
    try:
        return await _dispatch(config, client)
    except Exception:  # noqa: BLE001 - the scheduler must keep running
        log.exception("Alert dispatch failed")
        return 0


async def _route(session: AsyncSession, settings: dict,
                 active: dict[str, channels.Channel]) -> int:
    """Give each alert not yet routed one delivery per channel that takes
    it. An alert from before channels existed carries its own state over
    to its Discord delivery, retries and all."""
    new = (await session.execute(
        select(Notification).where(
            Notification.status.in_([NotificationStatus.PENDING, NotificationStatus.HELD]),
            ~exists().where(Delivery.notification_id == Notification.id))
        .order_by(Notification.id).limit(200)
    )).scalars().all()
    reason = (OFF_REASON if not settings.get("enabled", True)
              else NO_CHANNEL_REASON if not active else None)
    dropped = 0
    for row in new:
        takers = [] if reason else [c for c in active.values() if c.wants(row.kind, row.tone)]
        if not takers:
            row.status, row.error = NotificationStatus.DROPPED, reason or NOT_TAKEN_REASON
            dropped += 1
            continue
        for channel in takers:
            session.add(Delivery(notification_id=row.id, channel=channel.key, status=row.status,
                                 attempts=row.attempts, next_attempt_at=row.next_attempt_at))
    await session.flush()
    return dropped


async def _summarise(session: AsyncSession, notification_ids: set[int]) -> None:
    """Bring each alert's own status in line with its deliveries: sent once
    any channel has it, else waiting, held, failed or dropped."""
    if not notification_ids:
        return
    by_row: dict[int, list[Delivery]] = {}
    for d in (await session.execute(
            select(Delivery).where(Delivery.notification_id.in_(notification_ids)))).scalars():
        by_row.setdefault(d.notification_id, []).append(d)
    rows = (await session.execute(
        select(Notification).where(Notification.id.in_(by_row)))).scalars()
    for row in rows:
        ds = by_row[row.id]
        statuses = {d.status for d in ds}
        for status in (NotificationStatus.SENT, NotificationStatus.PENDING, NotificationStatus.HELD,
                       NotificationStatus.FAILED, NotificationStatus.DROPPED):
            if status in statuses:
                row.status = status
                break
        row.attempts = max(d.attempts for d in ds)
        sent = [d.sent_at for d in ds if d.sent_at is not None]
        row.sent_at = min(sent) if sent else None
        several = len(ds) > 1
        errors = [f"{channels.LABELS.get(d.channel, d.channel)}: {d.error}" if several else d.error
                  for d in ds if d.error and d.status != NotificationStatus.SENT]
        row.error = "; ".join(errors) or None
        waiting = [d.next_attempt_at for d in ds if d.status == NotificationStatus.PENDING]
        if waiting:
            row.next_attempt_at = min(waiting)


async def _dispatch(config, client) -> int:  # type: ignore[no-untyped-def]
    now = utcnow()
    vault = vault_for(config)
    # ---- route and decide what goes out, then let go of the database ----
    async with session_scope() as session:
        settings = await load(session)
        # Quiet hours are kept in the zone chosen under Preferences.
        settings["timezone"] = await prefs.get_timezone(session)
        active = {c.key: c for c in channels.configured(settings, vault)}
        settled = await _route(session, settings, active)

        open_rows = (await session.execute(
            select(Delivery, Notification)
            .join(Notification, Notification.id == Delivery.notification_id)
            .where(Delivery.status.in_([NotificationStatus.PENDING, NotificationStatus.HELD]))
            .order_by(Delivery.id).limit(1000)
        )).all()
        changed: set[int] = set()
        quiet = in_quiet_hours(settings, now)
        enabled = settings.get("enabled", True)
        plan: list[tuple[channels.Channel, list[channels.Item], list[channels.Item]]] = []
        for key in channels.KINDS:
            mine = [(d, n) for d, n in open_rows if d.channel == key]
            due = [(d, n) for d, n in mine
                   if d.status == NotificationStatus.PENDING and d.next_attempt_at <= now][:50]
            held = [(d, n) for d, n in mine if d.status == NotificationStatus.HELD][:200]
            if not due and not held:
                continue
            channel = active.get(key)
            if not enabled or channel is None:
                for d, n in due + held:
                    d.status, d.error = NotificationStatus.DROPPED, OFF_REASON if not enabled else GONE_REASON
                    changed.add(n.id)
                settled += len(due) + len(held)
                continue
            if quiet:
                for d, n in due:
                    d.status = NotificationStatus.HELD
                    changed.add(n.id)
                settled += len(due)
                continue
            plan.append((channel, [_item(d, n) for d, n in due], [_item(d, n) for d, n in held]))
        await _summarise(session, changed)
        if not plan:
            return settled
        tz = zone(settings)

    # ---- send, holding nothing ----
    results: list[tuple[str, list[int], SendResult]] = []
    for channel, items, held in plan:
        for message in channels.messages(items, held, tz, combines=channel.combines):
            result = await channels.send(channel, message, client=client, tz=tz)
            results.append((channel.key, [i.id for i in message.items], result))
            if result.retry_after is not None:
                break  # rate limited: everything after this on this channel waits too

    # ---- record ----
    sent_at = utcnow()
    touched = {i for _, ids, _ in results for i in ids}
    planned = {i.id for _, items, _ in plan for i in items}
    async with session_scope() as session:
        rows = {d.id: d for d in (await session.execute(
            select(Delivery).where(Delivery.id.in_(touched | planned)))).scalars()}
        for _, ids, result in results:
            for delivery_id in ids:
                d = rows.get(delivery_id)
                if d is None:
                    continue
                d.attempts += 1
                if result.ok:
                    d.status, d.sent_at, d.error = NotificationStatus.SENT, sent_at, None
                    continue
                d.error = result.error
                if result.permanent or d.attempts >= MAX_ATTEMPTS:
                    d.status = NotificationStatus.FAILED
                else:
                    # A held delivery whose digest failed retries on its own.
                    d.status = NotificationStatus.PENDING
                    wait = result.retry_after if result.retry_after is not None \
                        else BACKOFF_SECONDS[min(d.attempts - 1, len(BACKOFF_SECONDS) - 1)]
                    d.next_attempt_at = sent_at + timedelta(seconds=wait)
        # Deliveries never attempted because a 429 stopped their channel wait for it too.
        for key, _, result in results:
            if result.retry_after is None:
                continue
            later = sent_at + timedelta(seconds=result.retry_after or 5)
            for channel, items, _ in plan:
                if channel.key != key:
                    continue
                for item in items:
                    if item.id not in touched and item.id in rows:
                        rows[item.id].next_attempt_at = later
        await _summarise(session, {d.notification_id for d in rows.values()})
    return settled + len(touched)


async def send_test(session: AsyncSession, vault: Vault, key: str = "discord") -> SendResult:
    """Send a test message on one channel now, and log it. For the Test
    buttons.

    Commits before the network call, like SNMP Test: the request is not left
    holding its transaction while the channel answers.
    """
    settings = await load(session)
    channel = channels.ready(settings, vault, key)
    if channel is None:
        return SendResult(False, "Save a Discord webhook first." if key == "discord"
                          else f"Set up {channels.LABELS[key]} first.")
    await session.commit()
    started = _time.monotonic()
    result = await channels.send(channel, channels.Message(
        "SPARK test alert", "If you can read this, alerts will reach you here.",
        "info", utcnow(), [channels.Item(0, "test", "SPARK test alert", None, "info", utcnow(), 0)]),
        tz=zone({"timezone": await prefs.get_timezone(session)}))
    log.info("Test alert to %s: %s in %.1fs", channel.label, "sent" if result.ok else result.error,
             _time.monotonic() - started)
    status = NotificationStatus.SENT if result.ok else NotificationStatus.FAILED
    row = Notification(kind="test", subject=f"Test alert to {channel.label}", tone="info",
                       status=status, attempts=1, sent_at=utcnow() if result.ok else None,
                       error=result.error)
    session.add(row)
    await session.flush()
    session.add(Delivery(notification_id=row.id, channel=key, status=status, attempts=1,
                         sent_at=row.sent_at, error=result.error))
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
    "is_muted", "is_quiet", "on_snmp_poll", "on_target_transition", "post", "seal_plaintext_webhook",
    "send_test", "set_webhook", "validate_webhook", "webhook_url", "SendResult",
]
