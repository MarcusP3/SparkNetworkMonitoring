"""Browser-side defences: response headers and a same-origin check on writes.

Two things a server-rendered app should do for the browser that FastAPI does
not do by itself, kept in one raw ASGI middleware so they cost nothing on the
`/events` stream (Starlette's `BaseHTTPMiddleware` buffers and wraps streaming
responses; a plain ASGI callable does not).

**Headers.** A Content-Security-Policy that allows only SPARK's own origin,
with a per-request nonce for the handful of inline scripts in the templates,
so an injected `<script>` -- should autoescaping ever be bypassed -- does not
run. `frame-ancestors 'none'` stops the dashboard being framed for
clickjacking, `form-action 'self'` stops a form being pointed elsewhere, and
`Cache-Control: no-store` keeps a page full of the network's inventory out of
a shared machine's back button.

**Same-origin writes.** Every state change in SPARK is a POST from one of its
own pages. The session cookie is `SameSite=Lax`, which already stops a
cross-site form post from carrying it, but a second, independent check is
cheap: browsers send `Origin` on every cross-site POST, so a POST whose
`Origin` (or, failing that, `Referer`) names another host is refused. A
request with neither header -- curl, a test client, an old browser on a
same-site navigation -- is allowed, because a cookie-carrying cross-site
request from a modern browser always has one of them.

Only `Host` is compared. Behind a reverse proxy that rewrites the `Host`
header the check would refuse every POST; proxies preserve it by default and
the README says to keep it that way.
"""

from __future__ import annotations

import secrets
from urllib.parse import urlsplit

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'nonce-{nonce}'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "object-src 'none'"
)

_STATIC_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
)

_FORBIDDEN = (
    b"This request came from another site and was refused. "
    b"SPARK only accepts changes from its own pages.\n"
)


def _header(headers: list[tuple[bytes, bytes]], name: bytes) -> str | None:
    for key, value in headers:
        if key == name:
            return value.decode("latin-1")
    return None


def _request_origin(headers: list[tuple[bytes, bytes]]) -> str | None:
    """The host the browser says the request came from, or None if it did not say.

    `Origin` is authoritative when present. `null` is what a browser sends
    from a sandboxed frame or a `data:` page, which is never one of ours.
    """
    origin = _header(headers, b"origin")
    if origin is not None:
        if origin.strip().lower() == "null":
            return "null"
        return (urlsplit(origin).netloc or "null").lower()
    referer = _header(headers, b"referer")
    if referer:
        return (urlsplit(referer).netloc or "null").lower()
    return None


def same_origin(headers: list[tuple[bytes, bytes]]) -> bool:
    """Whether a write may proceed. Pure, so it can be tested without a server."""
    origin = _request_origin(headers)
    if origin is None:
        return True
    host = (_header(headers, b"host") or "").lower()
    return bool(host) and origin == host


class Hardening:
    """ASGI middleware: refuse cross-site writes, stamp every response."""

    def __init__(self, app) -> None:  # type: ignore[no-untyped-def]
        self.app = app

    async def __call__(self, scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = scope.get("headers", [])
        if scope["method"] in UNSAFE_METHODS and not same_origin(headers):
            await send(
                {
                    "type": "http.response.start",
                    "status": 403,
                    "headers": [
                        (b"content-type", b"text/plain; charset=utf-8"),
                        (b"content-length", str(len(_FORBIDDEN)).encode()),
                        *_STATIC_HEADERS,
                    ],
                }
            )
            await send({"type": "http.response.body", "body": _FORBIDDEN})
            return

        nonce = secrets.token_urlsafe(16)
        # `request.state` reads from here, so templates can emit the nonce.
        scope.setdefault("state", {})["csp_nonce"] = nonce
        csp = _CSP.format(nonce=nonce).encode("ascii")

        async def send_with_headers(message) -> None:  # type: ignore[no-untyped-def]
            if message["type"] == "http.response.start":
                out = list(message.get("headers", []))
                present = {key for key, _ in out}
                extra: list[tuple[bytes, bytes]] = [
                    (b"content-security-policy", csp),
                    *_STATIC_HEADERS,
                ]
                content_type = _header(out, b"content-type") or ""
                # Pages, not assets: the stylesheet and fonts are versioned by
                # content hash and are exactly what a browser should keep.
                if content_type.startswith("text/html"):
                    extra.append((b"cache-control", b"no-store"))
                out.extend((key, value) for key, value in extra if key not in present)
                message = {**message, "headers": out}
            await send(message)

        await self.app(scope, receive, send_with_headers)
