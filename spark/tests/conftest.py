"""Suite-wide guards.

No test may reach Discord, or any other channel's server. Webhooks in the
tests are shaped like real ones (setup refuses anything else), and the app's
alert dispatcher runs on a timer whenever a test starts the app -- so without
this, a slow test could post to discord.com with a made-up token. Tests that
exercise sending pass their own client (httpx.MockTransport) or their own
SMTP fake, and are unaffected.
"""

from __future__ import annotations

import httpx
import pytest


def _refuse(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError(f"no network in tests: {request.url.host}", request=request)


@pytest.fixture(autouse=True)
def _no_discord(monkeypatch):
    from spark import alerts

    monkeypatch.setattr(
        alerts, "_new_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(_refuse)),
    )


def _no_smtp(host, port, security, timeout):  # type: ignore[no-untyped-def]
    raise ConnectionRefusedError(f"no network in tests: {host}")


@pytest.fixture(autouse=True)
def _no_mail(monkeypatch):
    """Nor a mail server: email goes through channels._smtp_connect, which
    tests that send email replace with a fake of their own."""
    from spark import channels

    monkeypatch.setattr(channels, "_smtp_connect", _no_smtp)
