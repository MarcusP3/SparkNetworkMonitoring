"""Suite-wide guards.

No test may reach Discord. Webhooks in the tests are shaped like real ones
(setup refuses anything else), and the app's alert dispatcher runs on a timer
whenever a test starts the app -- so without this, a slow test could post to
discord.com with a made-up token. Tests that exercise sending pass their own
client (httpx.MockTransport) and are unaffected.
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
