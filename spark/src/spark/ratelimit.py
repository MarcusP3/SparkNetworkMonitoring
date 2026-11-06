"""Per-address and per-token limits, in memory.

One process, so a dictionary of recent timestamps is enough. It forgets on
restart, which costs a guesser nothing they could use: the heartbeat's limit
is 10 tries in 10 minutes against 2^192, the agent token's against 2^256.

Written for `/hb` (web/routes_heartbeat.py) and shared since with the agents'
API (routes_api.py), which counts wrong tokens per address the same way and
tool calls per token with its own numbers. The defaults are the heartbeat's;
a limiter for something else passes its own.
"""

from __future__ import annotations

import time
from collections import deque


class Limiter:
    """Per key, in memory: calls a minute, and misses in a window.

    `allow(who)` is the gate for a request; `miss(who)` records a wrong
    credential from that key. Past `misses_per_window` misses in
    `miss_window` seconds, or `per_minute` calls in a minute, `allow` says no.
    """

    PINGS_PER_MINUTE = 30
    MISSES_PER_WINDOW = 10
    MISS_WINDOW = 600.0
    MAX_ADDRESSES = 10_000

    def __init__(self, *, per_minute: int | None = None, misses_per_window: int | None = None,
                 miss_window: float | None = None) -> None:
        self.per_minute = self.PINGS_PER_MINUTE if per_minute is None else per_minute
        self.misses_per_window = (self.MISSES_PER_WINDOW if misses_per_window is None
                                  else misses_per_window)
        self.miss_window = self.MISS_WINDOW if miss_window is None else miss_window
        self._pings: dict[str, deque[float]] = {}
        self._misses: dict[str, deque[float]] = {}

    def _within(self, book: dict[str, deque[float]], who: str, window: float,
                now: float) -> deque[float]:
        if len(book) > self.MAX_ADDRESSES:
            book.clear()          # a flood of keys: start over, not grow
        times = book.setdefault(who, deque())
        while times and now - times[0] > window:
            times.popleft()
        return times

    def allow(self, who: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if len(self._within(self._misses, who, self.miss_window, now)) >= self.misses_per_window:
            return False
        pings = self._within(self._pings, who, 60.0, now)
        if len(pings) >= self.per_minute:
            return False
        pings.append(now)
        return True

    def miss(self, who: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self._within(self._misses, who, self.miss_window, now).append(now)
