"""Agent tokens: the credential an AI agent presents instead of the password.

An agent -- a local model behind Hermes Agent, LM Studio, Claude Code or
Claude Desktop -- reaches SPARK two ways, both through a token made here:
the JSON API under `/api/v1/` (web/routes_api.py, which the `spark-monitor-mcp`
bridge calls) and, for an agent that drives a browser, a sign-in on `/login`
that makes a session tagged with the token (web/routes_auth.py). The token is
what the password is not: named, so the audit says who; scoped, so a bad call
is bounded before it is made; expiring; revocable on its own, without signing
you out.

Storage follows sessions and heartbeat URLs: the token is random, shown once
when it is made, and only its SHA-256 is kept, so a copy of the database
cannot call the API or sign an agent in.

Two scopes. `read` sees what the pages show and changes nothing. `operate`
adds the reversible actions that are buttons on the pages -- acknowledge,
mute, check now, pause, a maintenance window, scan now -- and nothing that
touches credentials, settings, the account or where SPARK sends packets.
Those have no scope at all: an agent gets no closer to them than a browser
without a session does.

Everything an agent is told was told to SPARK by the network first: names,
SNMP descriptions, syslog lines. `clean` is the one door that text leaves
through: bounded, with the control and invisible characters that carry
hidden instructions removed. Marking it as data is the bridge's job, where
text meets a model; stopping a model from obeying it is nobody's job here,
and docs/guide.md says so.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import unicodedata
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import limits
from .models import AgentAction, AgentScope, AgentToken, UserSession, utcnow
from .ratelimit import Limiter

TOKEN_PREFIX = "spark_"
# 256 bits; token_urlsafe(32) is 43 characters, so a token is 49.
_TOKEN_BYTES = 32
TOKEN_PATTERN = re.compile(r"^spark_[A-Za-z0-9_-]{43}$")

# How stale last_used_at may get before it is rewritten: a chatty agent must
# not open a write transaction on every call (auth.LAST_SEEN_RESOLUTION says why).
LAST_USED_RESOLUTION = 60.0

# Expiry choices offered when a token is made, in days; None is never.
EXPIRY_CHOICES: tuple[int | None, ...] = (30, 90, 365, None)
DEFAULT_EXPIRY_DAYS = 90

# Tool calls a token may make in a minute. Misses (wrong tokens) are counted
# per address by the route, with the heartbeat's numbers.
calls = Limiter(per_minute=limits.AGENT_CALLS_PER_MINUTE)


class AgentError(ValueError):
    pass


# --------------------------------------------------------------------------
# Tokens
# --------------------------------------------------------------------------


def new_token() -> str:
    """`spark_` and 43 URL-safe characters. The prefix lets the sign-in form
    tell a token from a password, and lets a secret scanner find one that
    leaked into a repository or a chat log."""
    return TOKEN_PREFIX + secrets.token_urlsafe(_TOKEN_BYTES)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def looks_like_token(text: str | None) -> bool:
    return bool(text) and TOKEN_PATTERN.match(text.strip()) is not None  # type: ignore[arg-type]


def allows(token: AgentToken, scope: AgentScope) -> bool:
    """Whether `token` may do what `scope` covers. `operate` includes `read`."""
    if scope == AgentScope.READ:
        return token.scope in (AgentScope.READ, AgentScope.OPERATE)
    return token.scope == AgentScope.OPERATE


def _validate_name(name: str) -> str:
    name = " ".join((name or "").split())
    if not name:
        raise AgentError("Give the token a name: the audit and the alerts use it.")
    if len(name) > limits.NAME:
        raise AgentError(f"A token's name is at most {limits.NAME} characters.")
    return name


async def mint(session: AsyncSession, *, name: str, scope: AgentScope | str,
               expires_in_days: int | None = DEFAULT_EXPIRY_DAYS,
               ip: str | None = None) -> tuple[AgentToken, str]:
    """Make a token. Returns the row and the token itself, the one time it
    exists in plaintext; the caller shows it and forgets it.

    A security event in the same transaction: a new credential for the
    network map is something the owner should hear about, and if the owner
    did not make it, the person who did is holding it.
    """
    from . import alerts

    name = _validate_name(name)
    scope = AgentScope(scope)
    if expires_in_days is not None and expires_in_days not in EXPIRY_CHOICES:
        raise AgentError("Expiry is 30, 90 or 365 days, or never.")
    token = new_token()
    row = AgentToken(
        name=name, token_hash=token_hash(token), scope=scope, created_ip=ip,
        expires_at=(utcnow() + timedelta(days=expires_in_days)
                    if expires_in_days is not None else None),
    )
    session.add(row)
    await session.flush()
    where = f" from {ip}" if ip else ""
    await alerts.on_security_event(
        session, kind="security_agent_token", tone="info",
        subject=f"Agent token {name!r} ({scope}) created{where}",
        body=("A new agent token can read the network map"
              + (" and act on alerts and targets" if scope == AgentScope.OPERATE else "")
              + ". If that was not you, revoke it under Settings → Agents."),
    )
    return row, token


async def verify(session: AsyncSession, token: str | None, *, ip: str | None = None
                 ) -> AgentToken | None:
    """The token's row if it is good: known, not revoked, not expired.

    Records where and when it was last used, at most once a minute, and
    queues an event the first time it is used from a different address than
    the last -- the shape of a sign-in from a new address. Returns None for
    anything else, without saying which.
    """
    if not looks_like_token(token):
        return None
    row = await session.scalar(
        select(AgentToken).where(AgentToken.token_hash == token_hash(token.strip())))  # type: ignore[union-attr]
    if row is None or row.revoked:
        return None
    now = utcnow()
    if row.expires_at is not None and row.expires_at < now:
        return None
    if ip and row.last_used_ip and ip != row.last_used_ip:
        await _note_new_address(session, row, ip)
    if (row.last_used_at is None
            or (now - row.last_used_at).total_seconds() > LAST_USED_RESOLUTION
            or (ip and ip != row.last_used_ip)):
        row.last_used_at = now
        if ip:
            row.last_used_ip = ip
    return row


async def _note_new_address(session: AsyncSession, row: AgentToken, ip: str) -> None:
    """Once per token, address and day, however many calls follow."""
    from . import alerts

    day = utcnow().strftime("%Y%m%d")
    await alerts.on_security_event(
        session, kind="security_agent_address", tone="info",
        subject=f"Agent token {row.name!r} used from a new address: {ip}",
        body=(f"It was last used from `{row.last_used_ip}`. If the agent did not move, "
              "someone else has the token: revoke it under Settings → Agents."),
        dedupe_key=f"security:agent:{row.id}:{ip}:{day}",
    )


async def revoke(session: AsyncSession, token_id: int, *, ip: str | None = None
                 ) -> AgentToken | None:
    """Stop a token for good, and end every session signed in with it.

    The row stays, revoked, so the audit keeps the name. Idempotent: revoking
    a revoked token does nothing and says so with None.
    """
    from . import alerts

    row = await session.get(AgentToken, token_id)
    if row is None or row.revoked:
        return None
    row.revoked = True
    await session.execute(delete(UserSession).where(UserSession.agent_token_id == row.id))
    where = f" from {ip}" if ip else ""
    await alerts.on_security_event(
        session, kind="security_agent_token", tone="info",
        subject=f"Agent token {row.name!r} revoked{where}",
        body="Its sessions were ended and its calls are refused from now on.",
    )
    return row


async def list_tokens(session: AsyncSession) -> list[AgentToken]:
    """Every token, live ones first, newest first within each."""
    rows = (await session.execute(
        select(AgentToken).order_by(AgentToken.revoked, AgentToken.created_at.desc())
    )).scalars().all()
    return list(rows)


def is_live(token: AgentToken) -> bool:
    return not token.revoked and (token.expires_at is None or token.expires_at >= utcnow())


# --------------------------------------------------------------------------
# What an agent did
# --------------------------------------------------------------------------


async def record(session: AsyncSession, token: AgentToken, *, route: str, summary: str,
                 ok: bool, ip: str | None = None) -> AgentAction:
    """One audit row, in the caller's transaction, for an `operate` call or a
    refusal. Reads are not recorded: a write per read would make `read` the
    thing it exists not to be."""
    row = AgentAction(token_id=token.id, ip=ip, route=route[:64],
                      summary=clean(summary, limit=512), ok=ok)
    session.add(row)
    return row


async def recent_actions(session: AsyncSession, limit: int = 50) -> list[tuple[AgentAction, AgentToken]]:
    rows = (await session.execute(
        select(AgentAction, AgentToken)
        .join(AgentToken, AgentToken.id == AgentAction.token_id)
        .order_by(AgentAction.at.desc(), AgentAction.id.desc()).limit(limit)
    )).all()
    return [(action, token) for action, token in rows]


# --------------------------------------------------------------------------
# Device-written text
# --------------------------------------------------------------------------

# Characters that carry nothing a person reads and everything a hidden
# instruction needs: controls (Cc), format characters (Cf -- zero-width
# joiners, bidi overrides, the Unicode tag block that smuggles ASCII into a
# string invisibly), surrogates (Cs), private use (Co) and unassigned (Cn).
_DROP = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})
_SPACE = frozenset({"Zs", "Zl", "Zp"})


def clean(text: object, limit: int = limits.AGENT_TEXT) -> str:
    """Device-written text as a single bounded line of printable characters.

    Newlines and tabs become spaces (so one syslog line cannot pretend to be
    a transcript), invisible characters are dropped, and anything past
    `limit` is cut with an ellipsis. The words are left alone: this is not a
    filter for what a device may say, only for how it may say it.
    """
    if text is None:
        return ""
    out: list[str] = []
    for ch in str(text):
        category = unicodedata.category(ch)
        if ch in "\t\n\r\f\v" or category in _SPACE:
            out.append(" ")
        elif category in _DROP:
            continue
        else:
            out.append(ch)
    line = " ".join("".join(out).split())
    if len(line) > limit:
        line = line[: max(0, limit - 1)].rstrip() + "…"
    return line
