"""The HTTP check against a real local server: it never keeps more than
`MAX_BODY` of a response (review finding #30).

A monitored host that starts serving something enormous -- or is compromised
and made to -- used to be read whole into memory by `response.text`. Now the
body is streamed and cut at 1 MiB, and `expect_body` matches within that.
"""

from __future__ import annotations

import asyncio
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from spark.checks.base import CheckSpec
from spark.checks.net import MAX_BODY, check_http

PHRASE = b"all systems nominal"


class _Handler(BaseHTTPRequestHandler):
    """/small: a page with the phrase. /huge-early and /huge-late: eight
    times the cap, with the phrase at the start or the very end. /never: a
    body that keeps coming until the client stops reading."""

    def log_message(self, *_args) -> None:  # type: ignore[no-untyped-def]
        pass

    def do_GET(self) -> None:  # noqa: N802 - http.server's name
        if self.path == "/small":
            body = b"<p>" + PHRASE + b"</p>"
            self._send(body)
        elif self.path in ("/huge-early", "/huge-late"):
            filler = b"x" * (8 * MAX_BODY)
            body = PHRASE + filler if self.path == "/huge-early" else filler + PHRASE
            self._send(body)
        elif self.path == "/never":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            chunk = b"y" * 65536
            try:
                for _ in range(4096):        # 256 MiB if anyone read it all
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self.send_error(404)

    def _send(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


@pytest.fixture(scope="module")
def server():  # type: ignore[no-untyped-def]
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()


def _check(url: str, **params):  # type: ignore[no-untyped-def]
    return asyncio.run(check_http(CheckSpec(address=url, timeout_seconds=10, params=params)))


class TestBodyCap:
    def test_a_normal_page_matches(self, server):
        outcome = _check(server + "/small", expect_body=PHRASE.decode())
        assert outcome.ok and not outcome.degraded, outcome.detail

    def test_the_phrase_within_the_first_megabyte_is_found(self, server):
        outcome = _check(server + "/huge-early", expect_body=PHRASE.decode())
        assert outcome.ok, outcome.detail

    def test_the_phrase_beyond_the_cap_is_not_read_for(self, server):
        # Documented behaviour: expect_body looks at the first MAX_BODY only.
        outcome = _check(server + "/huge-late", expect_body=PHRASE.decode())
        assert not outcome.ok
        assert "did not contain" in (outcome.detail or "")

    def test_an_endless_body_is_cut_not_swallowed(self, server):
        # Finishes in bounded time and memory instead of reading 256 MiB.
        outcome = _check(server + "/never", expect_body="nothing here")
        assert not outcome.ok
        assert "did not contain" in (outcome.detail or "")

    def test_without_expect_body_the_body_is_not_read_at_all(self, server):
        outcome = _check(server + "/never")
        assert outcome.ok, outcome.detail
        assert outcome.detail and outcome.detail.startswith("HTTP 200")
