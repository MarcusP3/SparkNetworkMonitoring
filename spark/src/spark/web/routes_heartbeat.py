"""Heartbeat pings: /hb/<token>, which a job calls when it has run.

The only routes in SPARK that take no session. The token in the path is the
credential: random, 192 bits, compared by its SHA-256 against the one stored
hash, so the URL is never kept and a database copy cannot be used to ping.
GET, POST or HEAD all count (curl, wget, a PowerShell Invoke-WebRequest, an
uptime tool that only does HEAD). /fail is the job saying it ran and failed;
a POST body there is kept as the reason, its first line, 200 characters.

Answers are a word of plain text and nothing more: "ok", or "not found" for
a token that is not one -- the same whatever was wrong with it. Each address
gets 30 pings a minute and 10 wrong tokens in ten minutes, then 429.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import alerts, events
from ..checks import heartbeat
from ..engine.state import apply_outcome
from ..models import CheckType, Target, utcnow
from .deps import get_session
from ..ratelimit import Limiter

router = APIRouter()

_TOKEN = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
FAILED_DETAIL = 200


limiter = Limiter()


async def _ping(request: Request, session: AsyncSession, token: str, *, failed: bool):  # type: ignore[no-untyped-def]
    who = request.client.host if request.client else "unknown"
    if not limiter.allow(who):
        return PlainTextResponse("too many requests\n", status_code=429,
                                 headers={"Retry-After": "60"})
    target = None
    if _TOKEN.match(token):
        target = await session.scalar(select(Target).where(
            Target.check_type == CheckType.HEARTBEAT,
            Target.heartbeat_hash == heartbeat.token_hash(token)))
    if target is None:
        limiter.miss(who)
        return PlainTextResponse("not found\n", status_code=404)

    now = utcnow()
    params = dict(target.params or {})
    if failed:
        reason = ""
        if request.method == "POST":
            body = (await request.body())[:4096].decode("utf-8", "replace").strip()
            reason = body.splitlines()[0][:FAILED_DETAIL] if body else ""
        params.update(failed=True, failed_detail=reason or None)
    else:
        params.pop("failed", None)
        params.pop("failed_detail", None)
    target.params = params
    target.last_ping_at = now
    if not target.enabled:
        await session.commit()
        return PlainTextResponse("ok, but this heartbeat is paused\n")

    outcome = heartbeat.outcome(params=params, last_ping=now, since=target.created_at, now=now)
    transition = await apply_outcome(session, target, outcome)  # type: ignore[arg-type]
    if transition.changed:
        await alerts.on_target_transition(session, target, transition, outcome.detail)  # type: ignore[union-attr]
    await session.commit()
    events.publish({"kind": "status", "target_id": target.id, "from": str(transition.previous),
                    "to": str(transition.current)} if transition.changed
                   else {"kind": "heartbeat", "target_id": target.id})
    return PlainTextResponse("ok\n")


@router.api_route("/hb/{token}", methods=["GET", "POST", "HEAD"], include_in_schema=False)
async def ping(request: Request, token: str, session: AsyncSession = Depends(get_session)):
    """The job ran."""
    return await _ping(request, session, token, failed=False)


@router.api_route("/hb/{token}/fail", methods=["GET", "POST"], include_in_schema=False)
async def ping_failed(request: Request, token: str, session: AsyncSession = Depends(get_session)):
    """The job ran, and failed."""
    return await _ping(request, session, token, failed=True)
