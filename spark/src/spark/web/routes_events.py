"""Server-sent events, so open pages update themselves.

One long-lived connection per open tab, carrying nothing at all while the
network is quiet. The alternative -- polling -- costs a request every few
seconds per tab forever, and still shows a change up to a poll interval late.

Auth is done up front against a short-lived session rather than through the
usual `Depends(get_session)`. A dependency that yields keeps its session open
until the response finishes, and this response finishes when the browser tab
closes, which could be days. That would pin a SQLAlchemy session and a SQLite
connection per tab for no reason.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from .. import events
from ..auth import SESSION_COOKIE, resolve_proxy_user, resolve_session
from ..db import session_scope

log = logging.getLogger(__name__)

router = APIRouter()

# Long enough to be nearly free, short enough that a dead connection is noticed
# and that proxies which time out idle streams keep it open.
HEARTBEAT_SECONDS = 20


async def _authenticate(request: Request) -> bool:
    config = request.app.state.config
    async with session_scope() as session:
        if config.auth.mode == "proxy":
            return await resolve_proxy_user(session, request, config.auth) is not None
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            return False
        return await resolve_session(session, token) is not None


@router.get("/events")
async def stream_events(request: Request):
    if not await _authenticate(request):
        # 401 rather than a redirect: EventSource cannot follow one usefully,
        # and the page's script treats an error as "stop trying".
        return StreamingResponse(
            iter(()), status_code=401, media_type="text/event-stream"
        )

    async def generate():
        queue = events.subscribe()
        try:
            # Tell the client we are live, so it can clear any "reconnecting"
            # state without waiting for the first real change.
            yield "event: ready\ndata: {}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(
                        queue.get(), timeout=HEARTBEAT_SECONDS
                    )
                except asyncio.TimeoutError:
                    # A comment line. Keeps the connection warm and detects a
                    # peer that has gone away without closing.
                    yield ": keep-alive\n\n"
                    continue
                yield f"event: change\ndata: {json.dumps(event)}\n\n"
        except asyncio.CancelledError:  # pragma: no cover - normal disconnect
            raise
        finally:
            events.unsubscribe(queue)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # nginx buffers streamed responses by default, which makes events
            # arrive in silent clumps or not at all. Harmless when direct.
            "X-Accel-Buffering": "no",
        },
    )
