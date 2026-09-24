"""Authentication for SPARK.

SPARK ends up holding a map of the whole network, an inventory of every service
on it, and references to credentials that reach the Docker hosts. That makes it
the most valuable single target on the LAN, which is why "it's internal, skip
the login" isn't the default here. One password is proportionate.

Two modes:

  password  - single admin account, Argon2id, signed session cookie
  proxy     - trust an identity header from an allowlisted reverse proxy
              (Authelia, Tailscale, Cloudflare Access)

Proxy mode refuses to start without a source allowlist. A trusted-header setup
that accepts the header from anywhere is spoofable by any host on the network,
which is worse than no auth because it looks like security.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import secrets
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Request
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import AuthConfig
from .models import LoginAttempt, User, UserSession, utcnow

log = logging.getLogger(__name__)

SESSION_COOKIE = "spark_session"
_hasher = PasswordHasher()

# Verified against when the username doesn't exist, so a miss costs the same
# Argon2 work as a hit. Skipping this is measurable: a wrong password on a real
# account took ~122 ms and an unknown username ~4 ms, which enumerates accounts
# regardless of the error message being identical.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(32))

MIN_PASSWORD_LENGTH = 12

# How stale a session's last-seen timestamp may get before it is rewritten.
LAST_SEEN_RESOLUTION = 60.0


class AuthError(Exception):
    pass


class RateLimited(AuthError):
    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__("Too many failed attempts")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        _hasher.verify(password_hash, password)
        return True
    except (VerifyMismatchError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def validate_password_strength(password: str) -> None:
    """Deliberately minimal: length only.

    Composition rules ("must contain a symbol") push people toward
    Passw0rd! and away from passphrases. Length is the property that matters.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters. "
            "A short phrase you'll remember beats a short jumble you won't."
        )


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def client_ip(request: Request, auth_config: AuthConfig) -> str:
    """The client's address, honouring X-Forwarded-For only behind a trusted proxy."""
    direct = request.client.host if request.client else "unknown"
    if auth_config.mode != "proxy":
        return direct
    if not _is_trusted_proxy(direct, auth_config):
        return direct
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        # The *last* entry, not the first. A proxy appends the address it saw
        # to whatever the client already sent, so the first entry is the
        # client's to write -- which made the per-IP login limit a header
        # away from useless -- and the last is the one the trusted proxy
        # itself added.
        return forwarded.split(",")[-1].strip() or direct
    return direct


def _is_trusted_proxy(ip: str, auth_config: AuthConfig) -> bool:
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for entry in auth_config.proxy.trusted_proxies:
        if address in ipaddress.ip_network(entry, strict=False):
            return True
    return False


# --------------------------------------------------------------------------
# Setup and login
# --------------------------------------------------------------------------


async def setup_required(session: AsyncSession) -> bool:
    count = await session.scalar(select(func.count()).select_from(User))
    return (count or 0) == 0


async def create_admin(session: AsyncSession, username: str, password: str) -> User:
    if not await setup_required(session):
        raise AuthError("An administrator account already exists")
    validate_password_strength(password)
    user = User(username=username.strip(), password_hash=hash_password(password), is_admin=True)
    session.add(user)
    await session.flush()
    log.info("Created administrator account %r", user.username)
    return user


async def _check_rate_limit(session: AsyncSession, ip: str, config: AuthConfig) -> None:
    window_start = utcnow() - timedelta(minutes=config.lockout_minutes)
    failures = await session.scalar(
        select(func.count())
        .select_from(LoginAttempt)
        .where(LoginAttempt.ip == ip, LoginAttempt.ts >= window_start, LoginAttempt.ok.is_(False))
    )
    if (failures or 0) >= config.max_attempts:
        raise RateLimited(config.lockout_minutes * 60)


async def authenticate(
    session: AsyncSession, username: str, password: str, ip: str, config: AuthConfig
) -> User:
    await _check_rate_limit(session, ip, config)

    user = await session.scalar(select(User).where(User.username == username.strip()))
    if user is not None and user.password_hash:
        ok = verify_password(user.password_hash, password)
    else:
        # Spend the same work on a missing user as on a wrong password.
        verify_password(_DUMMY_HASH, password)
        ok = False

    session.add(LoginAttempt(ip=ip, ok=ok))

    if not ok or user is None:
        # Same message either way; distinguishing them tells an attacker which
        # usernames exist.
        raise AuthError("Incorrect username or password")

    if user.password_hash and needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)

    user.last_login_at = utcnow()
    return user


async def change_password(
    session: AsyncSession, user: User, current: str, new: str
) -> None:
    if not user.password_hash or not verify_password(user.password_hash, current):
        raise AuthError("Current password is incorrect")
    validate_password_strength(new)
    user.password_hash = hash_password(new)
    # Every session is now stale, this one included. The caller is responsible
    # for issuing a fresh cookie; previously the comment claimed "every other"
    # while the code revoked all of them, including the one in hand.
    await revoke_all_sessions(session, user.id)


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------


async def create_session(
    session: AsyncSession,
    user: User,
    config: AuthConfig,
    user_agent: str | None = None,
    ip: str | None = None,
) -> str:
    token = secrets.token_urlsafe(32)
    session.add(
        UserSession(
            user_id=user.id,
            token_hash=_token_hash(token),
            expires_at=utcnow() + timedelta(days=config.session_days),
            user_agent=(user_agent or "")[:255] or None,
            ip=ip,
        )
    )
    return token


async def resolve_session(session: AsyncSession, token: str) -> User | None:
    # One round trip, not two: the session row and its user together. This
    # runs on every authenticated request, so it is the query that matters.
    row = (
        await session.execute(
            select(UserSession, User)
            .join(User, User.id == UserSession.user_id)
            .where(UserSession.token_hash == _token_hash(token))
        )
    ).first()
    if row is None:
        return None
    record, user = row
    if record.revoked:
        return None

    if record.expires_at < utcnow():
        return None

    # Only write when it is actually stale. Updating this on every request
    # means every authenticated page view opens a write transaction and holds
    # SQLite's single writer slot for the life of the request -- which is what
    # made a background sweep collide with the request that started it. A
    # minute of resolution is plenty for an idle-session timestamp.
    if (utcnow() - record.last_seen_at).total_seconds() > LAST_SEEN_RESOLUTION:
        record.last_seen_at = utcnow()
    return user


async def revoke_session(session: AsyncSession, token: str) -> None:
    record = await session.scalar(
        select(UserSession).where(UserSession.token_hash == _token_hash(token))
    )
    if record is not None:
        record.revoked = True


async def revoke_all_sessions(session: AsyncSession, user_id: int) -> None:
    await session.execute(delete(UserSession).where(UserSession.user_id == user_id))


async def purge_expired(session: AsyncSession) -> None:
    await session.execute(delete(UserSession).where(UserSession.expires_at < utcnow()))
    await session.execute(
        delete(LoginAttempt).where(LoginAttempt.ts < utcnow() - timedelta(days=7))
    )


# --------------------------------------------------------------------------
# Proxy mode
# --------------------------------------------------------------------------


async def resolve_proxy_user(
    session: AsyncSession, request: Request, config: AuthConfig
) -> User | None:
    direct = request.client.host if request.client else ""
    if not _is_trusted_proxy(direct, config):
        log.warning(
            "Identity header presented from %s, which is not in auth.proxy.trusted_proxies",
            direct,
        )
        return None
    username = request.headers.get(config.proxy.header.lower())
    if not username:
        return None
    user = await session.scalar(select(User).where(User.username == username.strip()))
    if user is None:
        # Trust the proxy's authentication, but only for accounts that exist
        # here. Auto-provisioning would let a misconfigured proxy mint admins.
        log.warning("Proxy asserted unknown user %r", username)
    return user
