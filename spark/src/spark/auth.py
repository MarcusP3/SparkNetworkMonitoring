"""Authentication for SPARK.

SPARK ends up holding a map of the whole network, an inventory of every service
on it, and references to credentials that reach the Docker hosts. That makes it
the most valuable single target on the LAN, which is why "it's internal, skip
the login" isn't the default here. One password is proportionate.

Two modes:

  password  - single admin account, Argon2id, an opaque session token in an
              HttpOnly cookie and stored only as its SHA-256
  proxy     - trust an identity header from an allowlisted reverse proxy
              (Authelia, Tailscale, Cloudflare Access)

Proxy mode refuses to start without a source allowlist. A trusted-header setup
that accepts the header from anywhere is spoofable by any host on the network,
which is worse than no auth because it looks like security. The allowlist is
checked against the TCP peer (`request.state.peer`, set by web/hardening.py),
never against an address a header claims.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import weakref
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Request
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .config import AuthConfig
from .models import LoginAttempt, User, UserSession, utcnow
from .proxies import is_trusted_proxy, parse_proxies

log = logging.getLogger(__name__)

SESSION_COOKIE = "spark_session"
# Over HTTPS the cookie carries the __Host- prefix, which makes the browser
# itself insist on Secure, Path=/ and no Domain -- so a cookie set over TLS
# can never be sent, or overwritten, over anything else.
SECURE_SESSION_COOKIE = "__Host-spark_session"
_hasher = PasswordHasher()

# Verified against when the username doesn't exist, so a miss costs the same
# Argon2 work as a hit. Skipping this is measurable: a wrong password on a real
# account took ~122 ms and an unknown username ~4 ms, which enumerates accounts
# regardless of the error message being identical.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(32))

MIN_PASSWORD_LENGTH = 12

# Argon2 is deliberately slow (about 120 ms and 64 MiB per attempt with the
# library's defaults). Run synchronously it stalled the event loop -- every
# check, poll and alert -- for the length of each login attempt, and a flood
# of attempts was a flood of stalls (review finding #28). It runs in a thread
# now, and at most this many at once, so a flood costs threads and memory
# that are bounded rather than the loop.
HASH_CONCURRENCY = 2
_slots: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _slot() -> asyncio.Semaphore:
    """One semaphore per event loop: an asyncio primitive binds to the loop it
    first waits on, and the test suite runs the app on many loops in turn."""
    loop = asyncio.get_running_loop()
    slot = _slots.get(loop)
    if slot is None:
        slot = _slots[loop] = asyncio.Semaphore(HASH_CONCURRENCY)
    return slot


async def _hash_in_thread(fn, *args):  # type: ignore[no-untyped-def]
    async with _slot():
        return await asyncio.to_thread(fn, *args)

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


def client_ip(request: Request, auth_config: AuthConfig | None = None) -> str:
    """The client's address, as the login lockout and session records key it.

    Already settled by the time a route runs: `web/hardening.py` replaces the
    TCP peer with the last X-Forwarded-For entry when -- and only when -- the
    peer is in `auth.proxy.trusted_proxies`, in either auth mode. Nothing here
    reads the header again, so there is exactly one place that decides whom
    to believe about addresses. The TCP peer itself is `request.state.peer`.
    """
    return request.client.host if request.client else "unknown"


def peer_ip(request: Request) -> str:
    """The address SPARK actually accepted the connection from -- never a header."""
    peer = getattr(getattr(request, "state", None), "peer", None)
    if peer:
        return str(peer)
    return request.client.host if request.client else ""


# --------------------------------------------------------------------------
# Setup and login
# --------------------------------------------------------------------------


async def setup_required(session: AsyncSession) -> bool:
    count = await session.scalar(select(func.count()).select_from(User))
    return (count or 0) == 0


async def create_admin(session: AsyncSession, username: str, password: str) -> User:
    if not await setup_required(session):
        raise AuthError("An administrator account already exists")
    if not username.strip():
        raise AuthError("Choose a username.")
    validate_password_strength(password)
    digest = await _hash_in_thread(hash_password, password)
    user = User(username=username.strip(), password_hash=digest, is_admin=True)
    session.add(user)
    try:
        await session.flush()
    except IntegrityError:
        # Someone else's insert landed between the count above and this one.
        # The partial unique index on is_admin (models.User) makes that a
        # refusal rather than a second administrator.
        await session.rollback()
        raise AuthError("An administrator account already exists") from None
    log.info("Created administrator account %r", user.username)
    return user


# --------------------------------------------------------------------------
# First-run setup code
# --------------------------------------------------------------------------

# Printed to the log when SPARK starts with no administrator, and required by
# /setup. Without it the first person to reach the port -- on the LAN before
# the owner got round to it, or from the internet on an exposed instance --
# owned the install (review finding #23). Reading the container log is
# something only the operator can do.
_SETUP_CODE_BYTES = 6


def new_setup_code() -> str:
    """Twelve hex characters in three groups, easy to read off a log line."""
    raw = secrets.token_hex(_SETUP_CODE_BYTES).upper()
    return "-".join(raw[i:i + 4] for i in range(0, len(raw), 4))


def setup_code_matches(expected: str | None, submitted: str) -> bool:
    """Compare in constant time, ignoring case, spaces and the dashes."""
    if not expected:
        return False
    normalise = lambda s: "".join(ch for ch in s.upper() if ch.isalnum())  # noqa: E731
    return secrets.compare_digest(normalise(expected), normalise(submitted or ""))


def announce_setup_code(code: str, host: str, port: int, scheme: str = "http") -> None:
    where = "localhost" if host in ("0.0.0.0", "::", "") else host
    banner = "=" * 72
    log.warning(
        "\n%s\n"
        "  SPARK has no administrator yet.\n"
        "  Open %s://%s:%s/setup and enter this setup code:\n\n"
        "      %s\n\n"
        "  It is shown only here, and changes when SPARK restarts.\n"
        "%s",
        banner, scheme, where, port, code, banner,
    )


def cookie_name(request: Request) -> str:
    """The session cookie's name for this request: prefixed over HTTPS."""
    return SECURE_SESSION_COOKIE if request.url.scheme == "https" else SESSION_COOKIE


def session_token(request: Request) -> str | None:
    """The session token the browser sent, under either name.

    Both are read so a session started over one scheme survives a switch
    to the other -- an instance turning TLS on, or a proxy going in front --
    for as long as the browser keeps sending the old cookie.
    """
    return request.cookies.get(SECURE_SESSION_COOKIE) or request.cookies.get(SESSION_COOKIE)


async def record_attempt(session: AsyncSession, ip: str, ok: bool) -> None:
    """A guess at the setup code counts toward the same lockout as a login."""
    session.add(LoginAttempt(ip=ip, ok=ok))


async def note_lockout(session: AsyncSession, ip: str, config: AuthConfig, what: str) -> None:
    """Tell the owner the lockout tripped for `ip` -- once per lockout window,
    however many refused attempts follow."""
    from . import alerts

    window = int(utcnow().timestamp() // (config.lockout_minutes * 60))
    await alerts.on_security_event(
        session, kind="security_lockout",
        subject=f"{what}: {config.max_attempts} failed attempts from {ip}",
        body=(f"`{ip}` is locked out for {config.lockout_minutes} minutes. If that was not "
              "you mistyping, something on the network is guessing."),
        dedupe_key=f"security:lockout:{ip}:{window}",
    )


async def note_sign_in(session: AsyncSession, user: User, ip: str | None,
                       user_agent: str | None) -> None:
    """Tell the owner about a sign-in from an address no session has come from
    before. The very first session of the account is not news; the second
    one from somewhere new is.

    Called before the new session row is added, so "before" means before.
    """
    from . import alerts

    if not ip:
        return
    any_before = await session.scalar(
        select(UserSession.id).where(UserSession.user_id == user.id).limit(1))
    if any_before is None:
        return
    seen = await session.scalar(
        select(UserSession.id).where(UserSession.user_id == user.id,
                                     UserSession.ip == ip).limit(1))
    if seen is not None:
        return
    await alerts.on_security_event(
        session, kind="security_new_address", tone="info",
        subject=f"Signed in from a new address: {ip}",
        body=f"`{ip}` — {(user_agent or 'unknown browser')[:120]}. If that was not you, "
             "change the password under Preferences → Account: it signs out every session.",
    )


async def note_password_changed(session: AsyncSession, how: str, ip: str | None = None) -> None:
    from . import alerts

    where = f" from {ip}" if ip else ""
    await alerts.on_security_event(
        session, kind="security_password", tone="info",
        subject=f"The password was {how}{where}",
        body="Every session has been signed out. If this was not you, the old password no "
             "longer works and the person who did it is signed in: on the machine SPARK runs "
             "on, `docker compose exec spark spark-reset-password` takes it back.",
    )


async def check_rate_limit(session: AsyncSession, ip: str, config: AuthConfig) -> None:
    await _check_rate_limit(session, ip, config)


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
        ok = await _hash_in_thread(verify_password, user.password_hash, password)
    else:
        # Spend the same work on a missing user as on a wrong password.
        await _hash_in_thread(verify_password, _DUMMY_HASH, password)
        ok = False

    session.add(LoginAttempt(ip=ip, ok=ok))

    if not ok or user is None:
        # Same message either way; distinguishing them tells an attacker which
        # usernames exist.
        raise AuthError("Incorrect username or password")

    if user.password_hash and needs_rehash(user.password_hash):
        user.password_hash = await _hash_in_thread(hash_password, password)

    user.last_login_at = utcnow()
    return user


async def change_password(
    session: AsyncSession, user: User, current: str, new: str
) -> None:
    """Set a new password after checking the current one.

    Every session is revoked, this one included -- a changed password is the
    moment to end anything signed in from somewhere else -- so the caller
    must issue a fresh cookie for the person who just did it.
    """
    if not user.password_hash or not await _hash_in_thread(verify_password,
                                                            user.password_hash, current):
        raise AuthError("Current password is incorrect")
    validate_password_strength(new)
    user.password_hash = await _hash_in_thread(hash_password, new)
    await revoke_all_sessions(session, user.id)


async def set_password(session: AsyncSession, user: User, new: str) -> None:
    """Replace the password without knowing the old one: `spark-reset-password`,
    run on the machine SPARK runs on. Every session is revoked."""
    validate_password_strength(new)
    user.password_hash = await _hash_in_thread(hash_password, new)
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


async def _live(
    session: AsyncSession, token: str, idle: timedelta | None
) -> tuple[UserSession, User] | None:
    """The session row and its user, if the token is still good."""
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
    now = utcnow()
    if record.expires_at < now:
        return None
    # Idle timeout. last_seen_at is written at most once a minute (below), so
    # this ends a session up to a minute early, never late.
    if idle is not None and now - record.last_seen_at > idle:
        return None
    return record, user


async def resolve_session(
    session: AsyncSession,
    token: str,
    *,
    idle: timedelta | None = None,
    touch: bool = True,
) -> User | None:
    """The signed-in user, or None.

    `idle` is the inactivity timeout (prefs.get_idle_minutes); None skips it.
    `touch=False` for requests the browser makes by itself -- the live
    refresh, the event stream, the timeout check -- so a tab left open does
    not count as someone using it and never times out.
    """
    live = await _live(session, token, idle)
    if live is None:
        return None
    record, user = live

    # Only write when it is actually stale. Updating this on every request
    # means every authenticated page view opens a write transaction and holds
    # SQLite's single writer slot for the life of the request -- which is what
    # made a background sweep collide with the request that started it. A
    # minute of resolution is plenty for an idle-session timestamp.
    if touch and (utcnow() - record.last_seen_at).total_seconds() > LAST_SEEN_RESOLUTION:
        record.last_seen_at = utcnow()
    return user


async def seconds_left(session: AsyncSession, token: str, idle: timedelta) -> int:
    """Seconds until this session times out; 0 if it already has. No touch."""
    live = await _live(session, token, idle)
    if live is None:
        return 0
    record, _ = live
    now = utcnow()
    left = min(record.last_seen_at + idle, record.expires_at) - now
    return max(0, int(left.total_seconds()))


async def end_idle_sessions(session: AsyncSession, idle: timedelta) -> int:
    """Delete sessions unused for longer than `idle`. Returns how many.

    Run when the timeout is changed. The timeout is checked against
    last_seen_at at request time, so without this a session that had timed
    out under 30 minutes would come back to life when the timeout went up to
    an hour.
    """
    result = await session.execute(
        delete(UserSession).where(UserSession.last_seen_at < utcnow() - idle)
    )
    return result.rowcount or 0


async def revoke_session(session: AsyncSession, token: str) -> None:
    record = await session.scalar(
        select(UserSession).where(UserSession.token_hash == _token_hash(token))
    )
    if record is not None:
        record.revoked = True


async def revoke_all_sessions(session: AsyncSession, user_id: int) -> None:
    await session.execute(delete(UserSession).where(UserSession.user_id == user_id))


async def revoke_other_sessions(session: AsyncSession, user_id: int, keep_token: str) -> int:
    """"Sign out everywhere else": every session but the one in hand. Returns how many."""
    result = await session.execute(
        delete(UserSession).where(UserSession.user_id == user_id,
                                  UserSession.token_hash != _token_hash(keep_token))
    )
    return result.rowcount or 0


async def list_sessions(session: AsyncSession, user_id: int, current_token: str | None,
                        idle: timedelta | None = None):
    """Live sessions for the Preferences page: (row, is_this_one), newest use first.

    Live means what _live means: not revoked, not past its expiry, and --
    with `idle` -- used within the inactivity timeout. A session that timed
    out is refused on its next request but its row stays until the nightly
    purge, so without the last test every time-out showed here as a
    session still signed in.
    """
    now = utcnow()
    query = (select(UserSession)
             .where(UserSession.user_id == user_id, UserSession.revoked.is_(False),
                    UserSession.expires_at >= now))
    if idle is not None:
        query = query.where(UserSession.last_seen_at >= now - idle)
    rows = (await session.execute(query.order_by(UserSession.last_seen_at.desc()))).scalars().all()
    current = _token_hash(current_token) if current_token else None
    return [(row, row.token_hash == current) for row in rows]


async def purge_expired(session: AsyncSession, idle: timedelta | None = None) -> None:
    """Delete sessions past their expiry -- and, with `idle`, the ones that
    timed out (no use within the inactivity timeout), which can never be
    used again -- and login attempts older than a week."""
    await session.execute(delete(UserSession).where(UserSession.expires_at < utcnow()))
    if idle is not None:
        await end_idle_sessions(session, idle)
    await session.execute(
        delete(LoginAttempt).where(LoginAttempt.ts < utcnow() - timedelta(days=7))
    )


# --------------------------------------------------------------------------
# Proxy mode
# --------------------------------------------------------------------------


async def resolve_proxy_user(
    session: AsyncSession, request: Request, config: AuthConfig
) -> User | None:
    # The TCP peer, never the address a header claims. `request.client` may
    # already be the forwarded address (hardening.py rewrites it behind a
    # trusted proxy), which is exactly the thing that must not be consulted
    # here: whoever sent X-Forwarded-For chose it.
    peer = peer_ip(request)
    if not is_trusted_proxy(peer, parse_proxies(config.proxy.trusted_proxies)):
        log.warning(
            "Identity header presented from %s, which is not in auth.proxy.trusted_proxies",
            peer,
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
