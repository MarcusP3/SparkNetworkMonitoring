"""What a check is, and what it returns.

Checks are deliberately ignorant of the database. They take a `CheckSpec` --
an address, a timeout and a bag of params -- and return a `CheckOutcome`. The
state machine in `engine.state` is what turns a sequence of outcomes into a
status, an incident and (later) an alert.

Keeping them pure is what makes hysteresis testable: the interesting bugs live
in "three failures in a row means down", not in whether a socket opened.

Every check must return an outcome rather than raising. A monitoring tool whose
poller throws when the thing it polls is broken has failed at its only job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass(frozen=True)
class CheckSpec:
    """Everything a check needs, and nothing it doesn't."""

    address: str
    timeout_seconds: float = 5.0
    params: dict[str, Any] = field(default_factory=dict)

    def param(self, key: str, default: Any = None) -> Any:
        return self.params.get(key, default)


@dataclass
class CheckOutcome:
    """The result of one probe.

    Three states, not two. `degraded` is for "answered, but something is
    wrong": partial packet loss, a TLS certificate about to expire, a response
    that arrived but slowly. Collapsing that into ok/failed either cries wolf
    or hides the early warning, and the whole point of a certificate expiry
    countdown is that it warns you before it becomes an outage.
    """

    ok: bool
    degraded: bool = False
    latency_ms: float | None = None
    detail: str | None = None

    @classmethod
    def up(cls, latency_ms: float | None = None, detail: str | None = None) -> CheckOutcome:
        return cls(ok=True, latency_ms=latency_ms, detail=detail)

    @classmethod
    def warn(cls, detail: str, latency_ms: float | None = None) -> CheckOutcome:
        return cls(ok=True, degraded=True, latency_ms=latency_ms, detail=detail)

    @classmethod
    def down(cls, detail: str, latency_ms: float | None = None) -> CheckOutcome:
        return cls(ok=False, latency_ms=latency_ms, detail=detail)


CheckFn = Callable[[CheckSpec], Awaitable[CheckOutcome]]


def describe_exception(exc: BaseException) -> str:
    """A short, human-readable reason, for the detail column.

    str(exc) is empty on several of the exceptions that matter most here --
    asyncio.TimeoutError and ConnectionRefusedError among them -- so fall back
    to the class name rather than storing a blank.
    """
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__
