"""In-process publish/subscribe for live page updates.

The scheduler, the web app and the browser connections all live in the same
process and the same event loop, so "push a change to open pages" needs nothing
more than a set of queues. No Redis, no message broker -- the same reasoning
that keeps the rest of SPARK in one container.

Deliberately lossy. A subscriber that stops draining its queue (a laptop asleep
with a tab open, a connection half-closed) gets its oldest events dropped rather
than growing without bound. Events are a nudge to re-read the page, not a
transaction log: dropping one costs a stale row until the next change, and the
alternative is a monitoring tool that leaks memory because someone shut a lid.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)

# Small on purpose. If a client is this far behind it is not reading anyway.
QUEUE_SIZE = 16

_subscribers: set[asyncio.Queue[dict[str, Any]]] = set()


def subscribe() -> asyncio.Queue[dict[str, Any]]:
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=QUEUE_SIZE)
    _subscribers.add(queue)
    return queue


def unsubscribe(queue: asyncio.Queue[dict[str, Any]]) -> None:
    _subscribers.discard(queue)


def subscriber_count() -> int:
    return len(_subscribers)


def publish(event: dict[str, Any]) -> None:
    """Fan an event out to every open page. Never raises, never blocks.

    Called from the check runner, which must not fail because a browser tab is
    misbehaving.
    """
    for queue in list(_subscribers):
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            # Drop the oldest and retry once; if that fails, skip this client.
            try:
                queue.get_nowait()
                queue.put_nowait(event)
            except Exception:  # noqa: BLE001
                log.debug("Dropping event for a subscriber that is not draining")
        except Exception:  # noqa: BLE001 - a bad subscriber is not our problem
            log.debug("Failed to queue event for a subscriber", exc_info=True)
