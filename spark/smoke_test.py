"""End-to-end smoke test for increment 1.

Exercises the paths a human would take on a fresh install: first-run setup,
logout, login, bad password, rate limiting, and the auth gate on the dashboard.
Run with:  .venv/bin/python smoke_test.py
"""

from __future__ import annotations

import re
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

                print("\nDefaults when the tuning box is left unticked")
                # A disabled fieldset submits none of its inputs, so this posts
                # exactly what the browser would post with the box unticked.
                r = client.post("/targets/new", data={
                    "name": "defaults-probe", "check_type": "tcp",
                    "address": "127.0.0.1", "params": '{"port": %d}' % open_port,
                    "depends_on_target_id": ""})
                check("target created with no tuning fields at all",
                      r.status_code == 303 and r.headers.get("location") == "/targets",
                      f"got {r.status_code} -> {r.headers.get('location')}")

                r = client.get("/targets")
                check("unticked target polls on the 15s default",
                      ">15s<" in r.text.replace(" ", "").replace("\n", ""),
                      "interval is not 15s")

                # Pin the id to this target's own row. The list is ordered by
                # name, so "the last edit link" is whatever sorts last, which
                # is not necessarily the one just created.
                r = client.get("/targets")
                match = re.search(r"defaults-probe.*?/targets/(\d+)/edit", r.text, re.S)
                check("found the probe's own row", match is not None)
                probe_id = int(match.group(1))
                r = client.get(f"/targets/{probe_id}/edit")
                check("a default target opens with tuning collapsed",
                      'id="tuning"' in r.text and "disabled" in r.text.split('id="tuning"')[1][:40],
                      "tuning fieldset is not disabled")
                # The greyed-out fields are themselves the statement of the
                # defaults, so assert on those rather than on prose.
                check("collapsed fields show the real defaults",
                      'value="15"' in r.text and 'value="3.0"' in r.text
                      and r.text.count('value="4"') >= 2,
                      "default values are not rendered in the fieldset")

                r = client.post(f"/targets/{probe_id}/edit", data={
                    "name": "defaults-probe", "check_type": "tcp",
                    "address": "127.0.0.1", "params": '{"port": %d}' % open_port,
                    "depends_on_target_id": "",
                    "interval_seconds": 300, "timeout_seconds": 9,
                    "failure_threshold": 2, "recovery_threshold": 7})
                check("tuned values are saved", r.status_code == 303)
                r = client.get(f"/targets/{probe_id}/edit")
                # Editing a tuned target with the box shut would silently reset
                # it, because disabled fields are not submitted. It must open.
                opened = 'id="tuning"' in r.text and \
                         "disabled" not in r.text.split('id="tuning"')[1][:40]
                check("a tuned target reopens with the box already ticked", opened,
                      "tuning fieldset stayed disabled - editing would reset it")
                check("checkbox itself is ticked", 'id="tune"' in r.text
                      and "checked" in r.text.split('id="tune"')[1][:60])

                client.post(f"/targets/{probe_id}/delete")

                print("\nDevices")
                r = client.get("/devices")
                check("devices page renders", r.status_code == 200 and "Devices" in r.text)
                check("honest empty state before any sweep",
                      "Nothing discovered yet" in r.text)
                # It used to blame NET_RAW on any empty list, which is a guess.
                # Before a sweep has run the honest answer is that none has.
                check("an empty list before any sweep says exactly that",
                      "No sweep has run yet" in r.text)
                check("it does not guess at a cause it has no evidence for",
                      "ICMP is unavailable inside the container" not in r.text)

                # A device the sweep would have created, without needing a network.
                import asyncio as _asyncio
                from spark import db as _db
                from spark.discovery.store import record as _record
                from spark.discovery.sweep import Observation as _Obs

                async def _seed():
                    async with _db.session_scope() as ses:
                        await _record(ses, _Obs(ip="10.1.10.77", mac="b8:27:eb:01:02:03",
                                                hostname="pi.lan", vendor="Raspberry Pi",
                                                subnet="LAN"))
                _asyncio.get_event_loop().run_until_complete(_seed()) \
                    if False else _asyncio.run(_seed())

                r = client.get("/devices")
                check("a discovered device is listed", "10.1.10.77" in r.text)
                check("its vendor came from the MAC prefix", "Raspberry Pi" in r.text)
                check("it is offered for watching", "/watch" in r.text)

                dev_id = int(re.search(r"/devices/(\d+)/watch", r.text).group(1))
                r = client.post(f"/devices/{dev_id}/watch")
                check("watching a device creates a target",
                      r.status_code == 303 and r.headers.get("location") == "/targets",
                      f"got {r.status_code} -> {r.headers.get('location')}")
                r = client.get("/targets")
                check("the new target is watching that address", "10.1.10.77" in r.text)
                r = client.get("/devices")
                check("the device now reads as watched", "watched" in r.text)

                r = client.post(f"/devices/{dev_id}/watch")
                check("watching twice does not make a second target",
                      client.get("/targets").text.count("10.1.10.77") == 1)

                # Tidy up: later sections count targets, and leaving this one
                # behind makes their arithmetic wrong rather than their logic.
                rows = client.get("/targets").text
                watched_id = int(re.search(
                    r"10\.1\.10\.77.*?/targets/(\d+)/edit", rows, re.S).group(1))
                client.post(f"/targets/{watched_id}/delete")
                check("cleanup left the other targets alone",
                      client.get("/targets").text.count("10.1.10.77") == 0)

                print("\nLive updates")
                r = client.get("/targets")
                check("targets page has a live region", 'id="live"' in r.text)
                check("rows carry identity and status for the swap",
                      'data-target-id=' in r.text and 'data-status=' in r.text)
                check("EventSource is wired up", "new EventSource('/events')" in r.text)
                r = client.get("/")
                check("dashboard has a live region too",
                      'id="live"' in r.text and 'data-target-id=' in r.text)

                print("\nTarget validation")
                r = client.post("/targets/new", data={
                    "name": "bad", "check_type": "tcp", "address": "127.0.0.1",
                    "params": "{not json}", "interval_seconds": 60,
                    "timeout_seconds": 2, "failure_threshold": 1,
                    "recovery_threshold": 1, "depends_on_target_id": ""})
                check("malformed params JSON is rejected, not stored",
                      r.status_code == 400 and "valid JSON" in r.text)

                print("\nTarget lifecycle")
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

        print("\nEvent stream is not public")
        with TestClient(create_app(build_config(tmp)), follow_redirects=False) as anon:
            r = anon.get("/events")
            check("/events rejects an unauthenticated client",
                  r.status_code == 401, f"got {r.status_code}")

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
