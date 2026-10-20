"""Where alerts go: Discord, ntfy, email and a webhook (Alerts -> Channels).

Discord was the only one, and its webhook still lives on Alerts -> Rules.
The others are set up on the Channels tab, and every channel, Discord
included, says what it gets: problems, recoveries, new devices, sign-in
events. The dispatcher (alerts.py) gives each alert one delivery per
channel that takes it and sends, retries and gives up on each on its own:
when the internet is down, Discord cannot be reached and an ntfy server on
the LAN still can.

Secrets are sealed with the vault and never shown again: ntfy's and the
webhook's URLs (a topic on ntfy.sh is readable by anyone who knows its name,
and a Home Assistant webhook id is the whole credential), their tokens, the
SMTP password. Only the host is kept in clear, to say where alerts go.

ntfy and the webhook post to whatever address they are given, on the LAN or
not -- that is what a self-hosted ntfy or a Home Assistant webhook is for,
and only the administrator can set one. Discord alone is held to Discord's
hosts (alerts.validate_webhook). Email is sent with the standard library's
smtplib in a thread; a password needs STARTTLS or TLS.
"""

from __future__ import annotations

import asyncio
import logging
import re
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from urllib.parse import urlsplit

import httpx

from .models import utcnow
from .vault import SecretUnavailable, Vault

log = logging.getLogger(__name__)

# What a channel can take, in the order the form shows them.
GROUPS = {
    "problems": "Problems",
    "recoveries": "Recoveries",
    "news": "New devices",
    "security": "Sign-in events",
}
ALL = frozenset(GROUPS)

LABELS = {"discord": "Discord", "ntfy": "ntfy", "email": "Email", "webhook": "Webhook"}
KINDS = tuple(LABELS)

# A burst larger than this is sent as one combined message. Discord allows a
# webhook about five posts in two seconds; a switch taking out twenty targets
# without dependencies set would otherwise hit it and arrive out of order.
BATCH_AT = 4
MAX_LINES = 20

HTTP_TIMEOUT = 10.0
SMTP_TIMEOUT = 20.0
SECURITY = ("starttls", "tls", "none")
MAX_RECIPIENTS = 5

_TOPIC = re.compile(r"^[-_A-Za-z0-9]{1,64}$")
_ADDRESS = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")
_HOST = re.compile(r"^[A-Za-z0-9.-]{1,253}$")


class ChannelError(ValueError):
    """Something a person typed that cannot be saved. The message is for them."""


def group_of(kind: str, tone: str) -> str:
    """Which of GROUPS an alert belongs to."""
    if kind.startswith("security_"):
        return "security"
    if kind == "new_devices":
        return "news"
    if tone == "ok":
        return "recoveries"
    return "problems"


def gets_from(form_values: list[str]) -> list[str]:
    """The ticked boxes, in GROUPS order. None ticked is allowed: the
    channel is kept and gets nothing until one is."""
    return [g for g in GROUPS if g in form_values]


# --------------------------------------------------------------------------
# What a person types
# --------------------------------------------------------------------------


def _url(raw: str, what: str) -> tuple[str, str]:
    url = (raw or "").strip()
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host:
        raise ChannelError(f"That is not a web address for {what}: it should start https:// "
                           f"(or http:// for one on your network).")
    if parts.username or parts.password:
        raise ChannelError(f"Leave the user name and password out of the {what} address; "
                           f"use the token field.")
    return url, host


def validate_ntfy(raw: str) -> tuple[str, str]:
    """An ntfy topic URL ("https://ntfy.sh/spark-alerts"): (url, host)."""
    url, host = _url(raw, "ntfy")
    segments = [s for s in urlsplit(url).path.split("/") if s]
    if len(segments) != 1 or not _TOPIC.match(segments[0]):
        raise ChannelError("Give the topic's address: the server, then one name made of "
                           "letters, digits, - and _, as in https://ntfy.sh/spark-alerts.")
    return url, host


def validate_hook(raw: str) -> tuple[str, str]:
    """A webhook URL: (url, host)."""
    return _url(raw, "the webhook")


@dataclass(frozen=True)
class EmailInput:
    host: str
    port: int
    security: str
    username: str
    sender: str
    to: list[str]


def validate_email(*, host: str, port: str, security: str, username: str, sender: str,
                   to: str, password_given: bool, password_saved: bool) -> EmailInput:
    host = (host or "").strip().lower()
    if not host or not _HOST.match(host):
        raise ChannelError("Give the mail server's name, as in smtp.example.com.")
    try:
        number = int((port or "").strip())
    except ValueError:
        raise ChannelError("The port must be a number: 587 for STARTTLS, 465 for TLS.") from None
    if not 1 <= number <= 65535:
        raise ChannelError("The port must be between 1 and 65535.")
    if security not in SECURITY:
        raise ChannelError("Choose STARTTLS, TLS or none.")
    username = (username or "").strip()
    if (password_given or (password_saved and username)) and security == "none":
        raise ChannelError("A password is never sent unencrypted: choose STARTTLS or TLS.")
    sender = (sender or "").strip()
    if not _ADDRESS.match(sender):
        raise ChannelError("The From address does not look like an email address.")
    recipients = [a.strip() for a in re.split(r"[,;\s]+", to or "") if a.strip()]
    if not recipients:
        raise ChannelError("Give at least one address to send to.")
    if len(recipients) > MAX_RECIPIENTS:
        raise ChannelError(f"At most {MAX_RECIPIENTS} addresses.")
    bad = next((a for a in recipients if not _ADDRESS.match(a)), None)
    if bad:
        raise ChannelError(f"“{bad}” does not look like an email address.")
    return EmailInput(host=host, port=number, security=security, username=username,
                      sender=sender, to=recipients)


# --------------------------------------------------------------------------
# What is saved
# --------------------------------------------------------------------------


def saved(settings: dict, key: str) -> dict:
    """A channel's saved settings, or {} if it is not set up. Discord's
    webhook stays where it always was; only what it gets is new."""
    if key == "discord":
        if not settings.get("discord_webhook_sealed"):
            return {}
        return {"gets": settings.get("discord_gets", sorted(ALL))}
    return dict(settings.get(key) or {})


def set_up(settings: dict) -> list[str]:
    """Which channels are set up, in KINDS order."""
    return [k for k in KINDS if saved(settings, k)]


def seal_ntfy(settings: dict, vault: Vault, *, url: str, token: str, gets: list[str]) -> None:
    """Blank url or token keeps the saved one: the fields are never filled in."""
    current = dict(settings.get("ntfy") or {})
    if url.strip():
        clean, host = validate_ntfy(url)
        current.update(url_sealed=vault.seal(clean), host=host)
    elif not current.get("url_sealed"):
        raise ChannelError("Give the ntfy topic's address.")
    if token.strip():
        current["token_sealed"] = vault.seal(token.strip())
    current["gets"] = gets
    settings["ntfy"] = current


def seal_hook(settings: dict, vault: Vault, *, url: str, token: str, gets: list[str]) -> None:
    current = dict(settings.get("webhook") or {})
    if url.strip():
        clean, host = validate_hook(url)
        current.update(url_sealed=vault.seal(clean), host=host)
    elif not current.get("url_sealed"):
        raise ChannelError("Give the webhook's address.")
    if token.strip():
        current["token_sealed"] = vault.seal(token.strip())
    current["gets"] = gets
    settings["webhook"] = current


def seal_email(settings: dict, vault: Vault, form: EmailInput, *, password: str,
               gets: list[str]) -> None:
    current = dict(settings.get("email") or {})
    current.update(host=form.host, port=form.port, security=form.security,
                   username=form.username, sender=form.sender, to=form.to, gets=gets)
    if password:
        current["password_sealed"] = vault.seal(password)
    if not form.username:
        current.pop("password_sealed", None)
    settings["email"] = current


def set_gets(settings: dict, key: str, gets: list[str]) -> None:
    if key == "discord":
        settings["discord_gets"] = gets
    elif settings.get(key):
        settings[key] = {**settings[key], "gets": gets}


def remove(settings: dict, key: str) -> None:
    if key in ("ntfy", "email", "webhook"):
        settings.pop(key, None)


# --------------------------------------------------------------------------
# Ready to send
# --------------------------------------------------------------------------


@dataclass
class Channel:
    """One channel, its secrets opened, for a dispatch."""

    key: str
    gets: frozenset[str]
    target: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        return LABELS[self.key]

    @property
    def combines(self) -> bool:
        """Bursts and quiet-hours digests as one message. A webhook is read
        by a program, which wants one alert per post."""
        return self.key != "webhook"

    def wants(self, kind: str, tone: str) -> bool:
        return group_of(kind, tone) in self.gets


def _open(vault: Vault, sealed: str | None) -> str | None:
    if not sealed:
        return None
    try:
        return vault.open(sealed)
    except SecretUnavailable:
        return None


def ready(settings: dict, vault: Vault, key: str) -> Channel | None:
    """One channel ready to send, or None if it is not set up (or its
    secret cannot be opened: a database restored without its secret.key)."""
    conf = saved(settings, key)
    if not conf:
        return None
    gets = frozenset(conf.get("gets", sorted(ALL)))
    if key == "discord":
        url = _open(vault, settings.get("discord_webhook_sealed"))
        return Channel(key, gets, {"url": url}) if url else None
    if key in ("ntfy", "webhook"):
        url = _open(vault, conf.get("url_sealed"))
        if url is None:
            return None
        return Channel(key, gets, {"url": url, "token": _open(vault, conf.get("token_sealed"))})
    if key == "email":
        password = _open(vault, conf.get("password_sealed"))
        if conf.get("username") and conf.get("password_sealed") and password is None:
            return None
        return Channel(key, gets, {**{k: v for k, v in conf.items() if k != "password_sealed"},
                                   "password": password})
    return None


def configured(settings: dict, vault: Vault) -> list[Channel]:
    return [c for k in KINDS if (c := ready(settings, vault, k)) is not None]


# --------------------------------------------------------------------------
# Messages
# --------------------------------------------------------------------------


@dataclass
class Item:
    """One delivery of one alert, as the dispatcher hands it over."""

    id: int                    # the delivery's id
    kind: str
    subject: str
    body: str | None
    tone: str
    created_at: datetime
    attempts: int
    incident_id: int | None = None


@dataclass
class Message:
    title: str
    body: str | None
    tone: str
    when: datetime
    items: list[Item]
    held: bool = False


def messages(items: list[Item], held: list[Item], tz, *, combines: bool) -> list[Message]:  # type: ignore[no-untyped-def]
    """Group what is due into messages. Combining channels get one digest for
    what quiet hours held and one message for a burst; a webhook gets each
    alert on its own."""
    out: list[Message] = []
    if not combines:
        out += [Message(h.subject, h.body, h.tone, h.created_at, [h], held=True) for h in held]
        out += [Message(i.subject, i.body, i.tone, i.created_at, [i]) for i in items]
        return out
    if held:
        lines = [f"`{h.created_at.astimezone(tz).strftime('%H:%M')}` {h.subject}"
                 for h in held[:MAX_LINES]]
        if len(held) > MAX_LINES:
            lines.append(f"…and {len(held) - MAX_LINES} more.")
        tone = "bad" if any(h.tone == "bad" for h in held) else "info"
        out.append(Message(
            f"While quiet hours were on: {len(held)} alert{'s' if len(held) != 1 else ''}",
            "\n".join(lines), tone, utcnow(), list(held), held=True))
    if len(items) >= BATCH_AT:
        lines = [f"**{i.subject}**" + (f" — {i.body.splitlines()[0]}" if i.body else "")
                 for i in items[:MAX_LINES]]
        if len(items) > MAX_LINES:
            lines.append(f"…and {len(items) - MAX_LINES} more.")
        tone = "bad" if any(i.tone == "bad" for i in items) else "info"
        out.append(Message(f"{len(items)} alerts", "\n".join(lines), tone, utcnow(), list(items)))
    else:
        out += [Message(i.subject, i.body, i.tone, i.created_at, [i]) for i in items]
    return out


def plain(text: str | None) -> str:
    """Discord's markdown taken out, for email."""
    return re.sub(r"\*\*|`", "", text or "")


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------


@dataclass
class SendResult:
    ok: bool
    error: str | None = None
    retry_after: float | None = None
    permanent: bool = False


def _answer(response: httpx.Response, who: str) -> SendResult:
    """What a server's answer means: sent, try later, or give up."""
    if response.status_code < 300:
        return SendResult(True)
    text = f"{who} answered {response.status_code}"
    if response.status_code == 429:
        try:
            retry = float(response.headers.get("retry-after", 30) or 30)
        except ValueError:
            retry = 30.0
        return SendResult(False, text, retry_after=retry)
    # 4xx other than these will not get better by trying again: a wrong
    # address, a topic or webhook that is gone, a token refused.
    return SendResult(False, text, permanent=400 <= response.status_code < 500
                      and response.status_code not in (408, 425))


async def _post(client: httpx.AsyncClient | None, url: str, who: str, **kwargs) -> SendResult:  # type: ignore[no-untyped-def]
    from . import alerts

    own = client is None
    client = client or alerts._new_client()
    try:
        response = await client.post(url, timeout=HTTP_TIMEOUT, **kwargs)
    except httpx.HTTPError as exc:
        return SendResult(False, f"{type(exc).__name__}: {exc}".rstrip(": "))
    finally:
        if own:
            await client.aclose()
    return _answer(response, who)


NTFY_PRIORITY = {"bad": 4, "ok": 3, "info": 2}
NTFY_TAGS = {"bad": ["warning"], "ok": ["white_check_mark"], "info": ["information_source"]}


async def _ntfy(channel: Channel, message: Message, client) -> SendResult:  # type: ignore[no-untyped-def]
    """JSON publishing: the title and body may hold any text, which headers
    may not."""
    parts = urlsplit(channel.target["url"])
    topic = parts.path.strip("/")
    root = f"{parts.scheme}://{parts.netloc}/"
    headers = {"Authorization": f"Bearer {channel.target['token']}"} if channel.target.get("token") else {}
    return await _post(client, root, "ntfy", headers=headers, json={
        "topic": topic,
        "title": message.title[:250],
        "message": (message.body or message.title)[:4000],
        "priority": NTFY_PRIORITY.get(message.tone, 3),
        "tags": NTFY_TAGS.get(message.tone, []),
        "markdown": True,
    })


async def _webhook(channel: Channel, message: Message, client) -> SendResult:  # type: ignore[no-untyped-def]
    item = message.items[0]
    headers = {"Authorization": f"Bearer {channel.target['token']}"} if channel.target.get("token") else {}
    return await _post(client, channel.target["url"], "The webhook", headers=headers, json={
        "source": "SPARK",
        "kind": item.kind,
        "subject": item.subject,
        "body": item.body,
        "tone": item.tone,
        "at": item.created_at.astimezone(timezone.utc).isoformat(),
        "incident_id": item.incident_id,
        "held_for_quiet_hours": message.held,
    })


def _smtp_connect(host: str, port: int, security: str, timeout: float) -> smtplib.SMTP:
    """A connection, encrypted as asked. A module attribute so the tests can
    put a fake in its place: no test talks to a mail server."""
    context = ssl.create_default_context()
    if security == "tls":
        return smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
    smtp = smtplib.SMTP(host, port, timeout=timeout)
    if security == "starttls":
        smtp.starttls(context=context)
    return smtp


def _send_mail(target: dict, mail: EmailMessage) -> SendResult:
    try:
        smtp = _smtp_connect(target["host"], int(target["port"]), target["security"], SMTP_TIMEOUT)
        try:
            if target.get("username"):
                smtp.login(target["username"], target.get("password") or "")
            smtp.send_message(mail)
        finally:
            try:
                smtp.quit()
            except (smtplib.SMTPException, OSError):
                pass
    except smtplib.SMTPAuthenticationError:
        return SendResult(False, "The mail server refused the user name or password.", permanent=True)
    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused) as exc:
        return SendResult(False, f"The mail server refused an address: {exc}", permanent=True)
    except ssl.SSLCertVerificationError as exc:
        return SendResult(False, f"The mail server's certificate was not trusted: {exc.verify_message}",
                          permanent=True)
    except smtplib.SMTPNotSupportedError:
        return SendResult(False, "The mail server does not offer STARTTLS: try TLS on port 465.",
                          permanent=True)
    except (smtplib.SMTPException, OSError) as exc:
        return SendResult(False, f"{type(exc).__name__}: {exc}".rstrip(": "))
    return SendResult(True)


def _mail(target: dict, message: Message, tz) -> EmailMessage:  # type: ignore[no-untyped-def]
    mail = EmailMessage()
    mail["Subject"] = f"SPARK: {plain(message.title)}"[:250]
    mail["From"] = target["sender"]
    mail["To"] = ", ".join(target["to"])
    mail["Date"] = formatdate(localtime=False)
    mail["Message-ID"] = make_msgid(domain=target["sender"].rsplit("@", 1)[-1])
    when = message.when.astimezone(tz).strftime("%a %b %-d, %H:%M %Z")
    mail.set_content(f"{plain(message.body or message.title)}\n\n— SPARK, {when}\n")
    return mail


async def _email(channel: Channel, message: Message, tz) -> SendResult:  # type: ignore[no-untyped-def]
    return await asyncio.to_thread(_send_mail, channel.target, _mail(channel.target, message, tz))


async def send(channel: Channel, message: Message, *, client: httpx.AsyncClient | None = None,
               tz=timezone.utc) -> SendResult:  # type: ignore[no-untyped-def]
    """Send one message on one channel. Classifies the answer; never raises."""
    from . import alerts

    try:
        if channel.key == "discord":
            result = await alerts.post(channel.target["url"], alerts.payload([alerts._embed(
                message.title, message.body, message.tone, message.when)]), client=client)
            return SendResult(result.ok, result.error, result.retry_after, result.permanent)
        if channel.key == "ntfy":
            return await _ntfy(channel, message, client)
        if channel.key == "webhook":
            return await _webhook(channel, message, client)
        if channel.key == "email":
            return await _email(channel, message, tz)
    except Exception as exc:  # noqa: BLE001 - one bad channel must not stop the rest
        log.warning("Sending to %s failed unexpectedly: %s", channel.label, exc)
        return SendResult(False, f"{type(exc).__name__}: {exc}")
    return SendResult(False, f"Unknown channel {channel.key!r}", permanent=True)
