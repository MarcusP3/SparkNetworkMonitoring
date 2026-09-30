"""Behaviour that only shows up under a real uvicorn.

The test client hands the app a scope directly; uvicorn wraps the app in its
own middleware first. Its `ProxyHeadersMiddleware` trusted 127.0.0.1 and ::1 by
default and rewrote the client address from X-Forwarded-For before SPARK saw
it, so every `TestClient` assertion about addresses passed while, in the
container, any loopback peer could pick its own address (review finding #22).
These tests start uvicorn on a loopback port and speak HTTP to it, so that
layer is under test too.
"""

from __future__ import annotations

import asyncio
import socket
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
import uvicorn

from spark import db as D
from spark.auth import create_admin
from spark.config import Config
from spark.main import create_app

PASSWORD = "correct horse battery"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _config(tmp: Path, **auth) -> Config:  # type: ignore[no-untyped-def]
    config = Config.model_validate(
        {"app": {"data_dir": str(tmp / "data"), "log_level": "WARNING"},
         "auth": auth, "network": {"subnets": []}}
    )
    config.app.data_dir.mkdir(parents=True, exist_ok=True)
    return config


def _seed_admin(config: Config) -> None:
    async def go():
        D.init_engine(config)
        await D.init_db(config)
        async with D.session_scope() as session:
            await create_admin(session, "admin", PASSWORD)
        await D.close_engine()
    asyncio.run(go())


class Live:
    """A real server on 127.0.0.1, torn down after the test."""

    def __init__(self, config: Config) -> None:
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.app = create_app(config)
        # What main.run() passes: SPARK does its own proxy-header handling.
        self.server = uvicorn.Server(uvicorn.Config(
            self.app, host="127.0.0.1", port=self.port, log_level="warning",
            proxy_headers=False, log_config=None,
        ))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> "Live":
        self.thread.start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                if httpx.get(self.base + "/healthz", timeout=0.5).status_code == 200:
                    return self
            except httpx.HTTPError:
                time.sleep(0.05)
        raise RuntimeError("uvicorn did not come up")

    def __exit__(self, *exc) -> None:  # type: ignore[no-untyped-def]
        self.server.should_exit = True
        self.thread.join(timeout=10)

    def get(self, path: str, **headers: str) -> httpx.Response:
        return httpx.get(self.base + path, headers=headers, follow_redirects=False)

    def login(self, password: str, **headers: str) -> httpx.Response:
        return httpx.post(self.base + "/login", data={"username": "admin", "password": password},
                          headers=headers, follow_redirects=False)


@pytest.fixture
def tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="spark-live-"))


class TestLockout:
    def test_a_loopback_peer_cannot_choose_its_own_address(self, tmp):
        config = _config(tmp, max_attempts=3)
        _seed_admin(config)
        with Live(config) as live:
            codes = [live.login("wrong", **{"X-Forwarded-For": f"203.0.113.{i}"}).status_code
                     for i in range(5)]
        # Every attempt came from 127.0.0.1 whatever the header said: the
        # fourth is refused. Before the fix this read [401, 401, 401, 401, 401].
        assert codes == [401, 401, 401, 429, 429]

    def test_behind_a_trusted_proxy_the_forwarded_address_is_the_bucket(self, tmp):
        config = _config(tmp, max_attempts=3, proxy={"trusted_proxies": ["127.0.0.1"]})
        _seed_admin(config)
        with Live(config) as live:
            # Two different people behind the same proxy: one locks out only
            # themselves (#27 -- one shared bucket behind an off-host proxy).
            other = [live.login("wrong", **{"X-Forwarded-For": "203.0.113.9"}).status_code
                     for _ in range(4)]
            me = live.login(PASSWORD, **{"X-Forwarded-For": "203.0.113.10"})
        assert other == [401, 401, 401, 429]
        assert me.status_code == 303 and "spark_session" in me.headers.get("set-cookie", "")

    def test_a_trusted_proxy_saying_https_makes_the_cookie_secure(self, tmp):
        config = _config(tmp, proxy={"trusted_proxies": ["127.0.0.1"]})
        _seed_admin(config)
        with Live(config) as live:
            plain = live.login(PASSWORD).headers["set-cookie"].lower()
            tls = live.login(PASSWORD, **{"X-Forwarded-Proto": "https"}).headers["set-cookie"].lower()
        assert "secure" not in plain
        assert "secure" in tls


class TestProxyMode:
    def test_a_forged_forwarded_for_does_not_make_loopback_the_proxy(self, tmp):
        # The proxy is elsewhere. A request from loopback naming the proxy in
        # X-Forwarded-For used to be let in as admin, with no password.
        config = _config(tmp, mode="proxy", proxy={"trusted_proxies": ["10.0.0.5"]})
        _seed_admin(config)
        with Live(config) as live:
            response = live.get("/settings", **{"Remote-User": "admin",
                                                "X-Forwarded-For": "10.0.0.5"})
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login")

    def test_a_proxy_on_the_same_host_works(self, tmp):
        # The layout host networking makes likely: Caddy/nginx/Traefik on the
        # VM, forwarding to 127.0.0.1:9700 and -- as every proxy does --
        # sending X-Forwarded-For. This used to be refused (#22).
        config = _config(tmp, mode="proxy", proxy={"trusted_proxies": ["127.0.0.1"]})
        _seed_admin(config)
        with Live(config) as live:
            response = live.get("/", **{"Remote-User": "admin", "X-Forwarded-For": "192.168.1.50"})
            unknown = live.get("/", **{"Remote-User": "nobody", "X-Forwarded-For": "192.168.1.50"})
        assert response.status_code == 200
        assert unknown.status_code == 303


class TestFirstRun:
    def test_only_one_administrator_can_be_created_however_many_try(self, tmp):
        # Eight claimants at once. The setup code is the same for all of them
        # -- what is under test is that check-then-insert cannot make two.
        config = _config(tmp)
        with Live(config) as live:
            assert live.get("/setup").status_code == 200
            # The page never contains the code; the operator reads it from
            # the log. Tests read it from where the log got it.
            code = live.app.state.setup_code
            assert code

            def claim(n: int) -> httpx.Response:
                return httpx.post(live.base + "/setup", data={
                    "setup_code": code, "username": f"admin{n}", "password": PASSWORD,
                    "password_confirm": PASSWORD}, follow_redirects=False)

            with ThreadPoolExecutor(max_workers=8) as pool:
                responses = list(pool.map(claim, range(8)))
            cookies = [r.headers.get("set-cookie", "") for r in responses if r.status_code == 303]
            alive = [
                httpx.get(live.base + "/", headers={"Cookie": c.split(";")[0]},
                          follow_redirects=False).status_code == 200
                for c in cookies
            ]
        # Exactly one admin row, and every cookie that was issued works.
        async def count():
            D.init_engine(config)
            async with D.session_scope() as session:
                from sqlalchemy import func, select

                from spark.models import User
                return await session.scalar(select(func.count()).select_from(User))
        assert asyncio.run(count()) == 1
        assert cookies and all(alive)
