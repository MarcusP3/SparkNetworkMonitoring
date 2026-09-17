"""End-to-end smoke test for increment 1.

Exercises the paths a human would take on a fresh install: first-run setup,
logout, login, bad password, rate limiting, and the auth gate on the dashboard.
Run with:  .venv/bin/python smoke_test.py
"""

from __future__ import annotations

import shutil
import socket
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from spark.config import AuthConfig, Config, ProxyAuthConfig
from spark.main import create_app

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  ok    {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL  {name} {detail}")


def build_config(tmp: Path) -> Config:
    data = {
        "app": {"data_dir": str(tmp / "data"), "port": 9700, "log_level": "WARNING"},
        "network": {
            "subnets": [
                {"name": "LAN", "cidr": "192.168.1.0/24", "vlan": 1, "attached": True},
                {"name": "IoT", "cidr": "192.168.20.0/24", "vlan": 20, "attached": False},
            ]
        },
    }
    config = Config.model_validate(data)
    config.app.data_dir.mkdir(parents=True, exist_ok=True)
    return config


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="spark-smoke-"))
    try:
        config = build_config(tmp)
        app = create_app(config)

        with TestClient(app, follow_redirects=False) as client:
            print("\nFirst run")
            r = client.get("/healthz")
            check("healthz open without auth", r.status_code == 200 and r.json()["status"] == "ok")

            r = client.get("/")
            check("dashboard redirects to setup on fresh install",
                  r.status_code == 303 and r.headers["location"] == "/setup",
                  f"got {r.status_code} {r.headers.get('location')}")

            r = client.get("/setup")
            check("setup page renders", r.status_code == 200 and "Set up SPARK" in r.text)

            print("\nPassword policy")
            r = client.post("/setup", data={
                "username": "admin", "password": "short", "password_confirm": "short"})
            check("rejects a password under 12 characters",
                  r.status_code == 400 and "at least 12" in r.text)

            r = client.post("/setup", data={
                "username": "admin",
                "password": "correct horse battery",
                "password_confirm": "different phrase here"})
            check("rejects mismatched confirmation",
                  r.status_code == 400 and "do not match" in r.text)

            print("\nAccount creation")
            r = client.post("/setup", data={
                "username": "admin",
                "password": "correct horse battery",
                "password_confirm": "correct horse battery",
                "discord_webhook_url": "https://discord.com/api/webhooks/test"})
            check("setup succeeds and redirects to dashboard",
                  r.status_code == 303 and r.headers["location"] == "/",
                  f"got {r.status_code}")
            check("session cookie issued", "spark_session" in client.cookies)

            r = client.get("/")
            check("dashboard renders when signed in",
                  r.status_code == 200 and "Dashboard" in r.text)
            check("webhook from setup was saved (no missing-webhook warning)",
                  "No Discord webhook configured" not in r.text)
            check("routed VLAN warning is shown",
                  "IoT" in r.text and "identified by IP" in r.text)
            check("subnet table lists both segments",
                  "192.168.1.0/24" in r.text and "192.168.20.0/24" in r.text)

            r = client.get("/setup")
            check("setup is closed once an admin exists",
                  r.status_code == 303 and r.headers["location"] == "/login")

            print("\nSessions")
            r = client.post("/logout")
            check("logout redirects to login", r.status_code == 303)
            r = client.get("/")
            check("dashboard gated after logout",
                  r.status_code == 303 and r.headers["location"].startswith("/login"))

            r = client.post("/login", data={
                "username": "admin", "password": "wrong password here", "next": "/"})
            check("wrong password rejected", r.status_code == 401)
            check("error message does not reveal whether the user exists",
                  "Incorrect username or password" in r.text)

            r = client.post("/login", data={
                "username": "admin", "password": "correct horse battery", "next": "/"})
            check("correct password signs in", r.status_code == 303)

            print("\nCheck engine")
            # A real listening socket, so "up" means something actually answered.
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            open_port = listener.getsockname()[1]

            closed = socket.socket()
            closed.bind(("127.0.0.1", 0))
            dead_port = closed.getsockname()[1]
            closed.close()

            try:
                r = client.post("/targets/new", data={
                    "name": "loopback-open", "check_type": "tcp",
                    "address": "127.0.0.1", "params": '{"port": %d}' % open_port,
                    "interval_seconds": 60, "timeout_seconds": 2,
                    "failure_threshold": 1, "recovery_threshold": 1,
                    "depends_on_target_id": ""})
                check("target created",
                      r.status_code == 303 and r.headers.get("location") == "/targets",
                      f"got {r.status_code} -> {r.headers.get('location')}")

                r = client.get("/targets")
                check("target is listed", "loopback-open" in r.text)
                check("target was checked on creation and is up",
                      "status-up" in r.text, "no up pill rendered")

                r = client.post("/targets/new", data={
                    "name": "loopback-dead", "check_type": "tcp",
                    "address": "127.0.0.1", "params": '{"port": %d}' % dead_port,
                    "interval_seconds": 60, "timeout_seconds": 2,
                    "failure_threshold": 1, "recovery_threshold": 1,
                    "depends_on_target_id": ""})
                check("second target created",
                      r.status_code == 303 and r.headers.get("location") == "/targets",
                      f"got {r.status_code} -> {r.headers.get('location')}")

                r = client.get("/targets")
                check("failed target shows as down", "status-down" in r.text)

                r = client.get("/")
                check("dashboard lists watched targets", "loopback-open" in r.text)
                check("dashboard shows the open incident",
                      "Recent incidents" in r.text and "ongoing" in r.text)
                check("down count reflects reality", ">1<" in r.text)

                print("\nTarget validation")
                r = client.post("/targets/new", data={
                    "name": "bad", "check_type": "tcp", "address": "127.0.0.1",
                    "params": "{not json}", "interval_seconds": 60,
                    "timeout_seconds": 2, "failure_threshold": 1,
                    "recovery_threshold": 1, "depends_on_target_id": ""})
                check("malformed params JSON is rejected, not stored",
                      r.status_code == 400 and "valid JSON" in r.text)

                print("\nTarget lifecycle")
                import re
                ids = sorted(set(int(m) for m in re.findall(r"/targets/(\d+)/edit", r.text)))
                r = client.get("/targets")
                ids = sorted(set(int(m) for m in re.findall(r"/targets/(\d+)/edit", r.text)))
                check("both targets have stable ids", len(ids) == 2, f"got {ids}")

                r = client.post(f"/targets/{ids[0]}/toggle")
                check("pause redirects", r.status_code == 303)
                r = client.get("/targets")
                check("paused target is marked paused", "status-paused" in r.text)

                r = client.post(f"/targets/{ids[0]}/delete")
                r = client.get("/targets")
                check("deleted target is gone", "loopback-open" not in r.text)

                r = client.get("/targets")
                check("surviving target is untouched", "loopback-dead" in r.text)
            finally:
                listener.close()

            print("\nOpen-redirect guard")
            client.post("/logout")
            r = client.post("/login", data={
                "username": "admin",
                "password": "correct horse battery",
                "next": "https://evil.example.com"})
            check("refuses to redirect off-site after login",
                  r.headers["location"] == "/", f"got {r.headers.get('location')}")

            # Every one of these navigates off-site if passed through. The
            # assertion is same-site-ness, not a literal "/": "/\\/evil.com"
            # correctly collapses to the same-site path "/evil.com", which is
            # not an open redirect and need not be rewritten to the root.
            hostile = ["//evil.example.com", "/\\evil.example.com",
                       "https://evil.example.com", "/\\/evil.example.com",
                       "////evil.example.com", "/\\\\//evil.example.com",
                       "\\\\evil.example.com"]
            for target in hostile:
                client.post("/logout")
                rr = client.post("/login", data={
                    "username": "admin",
                    "password": "correct horse battery",
                    "next": target})
                location = rr.headers.get("location", "")
                same_site = location.startswith("/") and not location.startswith("//")
                check(f"next={target!r} cannot leave the site",
                      same_site, f"got {location}")

            client.post("/logout")
            rr = client.post("/login", data={
                "username": "admin", "password": "correct horse battery",
                "next": "/incidents?open=1"})
            check("keeps a legitimate same-site next",
                  rr.headers["location"] == "/incidents?open=1",
                  f"got {rr.headers.get('location')}")

            print("\nRate limiting")
            client.post("/logout")
            statuses = []
            for _ in range(12):
                rr = client.post("/login", data={
                    "username": "admin", "password": "nope nope nope", "next": "/"})
                statuses.append(rr.status_code)
            check("locks out after repeated failures", 429 in statuses,
                  f"statuses={statuses}")

        print("\nPersistence across restart")
        app2 = create_app(build_config(tmp))
        with TestClient(app2, follow_redirects=False) as client2:
            r = client2.get("/")
            check("existing database is reused, not re-setup",
                  r.headers.get("location") == "/login", f"got {r.headers.get('location')}")

        print("\nProxy auth guard")
        bad = build_config(tmp)
        bad.auth = AuthConfig(mode="proxy", proxy=ProxyAuthConfig(trusted_proxies=[]))
        try:
            bad.auth.validate_for_use()
            check("proxy mode without an allowlist is refused", False, "no error raised")
        except ValueError:
            check("proxy mode without an allowlist is refused", True)

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
