"""Heartbeats: a job that says it ran, rather than a thing SPARK probes.

A backup, a cron task, a certificate renewal: nothing SPARK can poll will
tell you it has stopped running. Instead the job ends by calling its ping URL
(web/routes_heartbeat.py), and this decides what silence means: up while the
last ping is within the period plus the grace, down once it is not. A job
that calls the URL's /fail instead is down until it next succeeds.

Like every check, pure: what it needs is passed in, and it never raises.
Evaluated every minute by the runner; a ping moves the state at once.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta

from ..engine.state import human_duration
from .base import CheckOutcome

# What the form offers, and the bounds on what the server accepts.
UNITS = {"minutes": 60, "hours": 3600, "days": 86400}
MIN_PERIOD = timedelta(minutes=1)
MAX_PERIOD = timedelta(days=31)
MAX_GRACE = timedelta(days=7)
EVALUATE_SECONDS = 60


def new_token() -> str:
    """The secret in a ping URL: 32 URL-safe characters, 192 bits."""
    return secrets.token_urlsafe(24)


def token_hash(token: str) -> str:
    """What is stored instead of the token. A plain hash is enough: the
    token is random, not a password someone chose, so there is nothing to
    guess a dictionary of."""
    return hashlib.sha256(token.encode()).hexdigest()


def period_of(params: dict) -> timedelta:
    return timedelta(seconds=int(params.get("period_seconds") or 86400))


def grace_of(params: dict) -> timedelta:
    return timedelta(seconds=int(params.get("grace_seconds") or 3600))


def outcome(*, params: dict, last_ping: datetime | None, since: datetime,
            now: datetime) -> CheckOutcome | None:
    """What silence means now. None while a new heartbeat waits for its
    first ping and is not yet late: not up, since it has never been heard
    from, and not down either."""
    period, grace = period_of(params), grace_of(params)
    expected = f"expected every {human_duration(period.total_seconds())}, " \
               f"with {human_duration(grace.total_seconds())} grace"
    if params.get("failed"):
        why = params.get("failed_detail") or "it reported a failure"
        return CheckOutcome.down(f"The job said it failed: {why}.")
    if last_ping is None:
        if now - since <= period + grace:
            return None
        return CheckOutcome.down(f"No ping yet: {expected}.")
    quiet = now - last_ping
    if quiet <= period + grace:
        return CheckOutcome.up(detail=f"Last ping {human_duration(quiet.total_seconds())} ago.")
    return CheckOutcome.down(f"No ping for {human_duration(quiet.total_seconds())}: {expected}.")
