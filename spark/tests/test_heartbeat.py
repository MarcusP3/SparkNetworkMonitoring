"""Heartbeats: a job that calls SPARK when it has run.

Judged every minute and never probed: waiting until the first ping is due,
up while pings come within the period and grace, down once one does not, and
down at once when the job calls /fail. The ping URL holds a random token
kept only as its SHA-256; /hb takes no session, answers in a word, and is
rate-limited per address. Heartbeats belong to no device and nothing depends
on them.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from spark import db as D
from spark import tls
from spark.checks import heartbeat
from spark.engine import runner
from spark.main import create_app
from spark.models import (CheckResult, CheckType, HealthStatus, Incident, Notification, Target,
                          utcnow)
from spark.web import routes_heartbeat

from test_alerts import _config
from test_credentials import flat

PASSWORD = "correct horse battery"
DAY, HOUR = timedelta(days=1), timedelta(hours=1)
PARAMS = {"period_seconds": 86400, "grace_seconds": 3600}


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


class TestJudging:
    T = utcnow()

    def test_waiting_for_the_first_ping_is_neither(self):
        assert heartbeat.outcome(params=PARAMS, last_ping=None, since=self.T, now=self.T + DAY) is None

    def test_no_first_ping_in_time_is_down(self):
        out = heartbeat.outcome(params=PARAMS, last_ping=None, since=self.T, now=self.T + DAY + 2 * HOUR)
        assert not out.ok and out.detail == "No ping yet: expected every 1d 0h, with 1h 0m grace."

    def test_within_the_period_and_grace_is_up(self):
        out = heartbeat.outcome(params=PARAMS, last_ping=self.T, since=self.T,
                                now=self.T + DAY + 59 * timedelta(minutes=1))
        assert out.ok and out.detail == "Last ping 1d 0h ago."

    def test_past_them_is_down(self):
        out = heartbeat.outcome(params=PARAMS, last_ping=self.T, since=self.T,
                                now=self.T + DAY + 61 * timedelta(minutes=1))
        assert not out.ok and out.detail.startswith("No ping for 1d 1h: expected every 1d 0h")

    def test_a_reported_failure_stands(self):
        out = heartbeat.outcome(params={**PARAMS, "failed": True, "failed_detail": "disk full"},
                                last_ping=self.T, since=self.T, now=self.T)
        assert not out.ok and out.detail == "The job said it failed: disk full."

    def test_tokens(self):
        a, b = heartbeat.new_token(), heartbeat.new_token()
        assert a != b and len(a) == 32 and re.fullmatch(r"[A-Za-z0-9_-]+", a)
        assert heartbeat.token_hash(a) == hashlib.sha256(a.encode()).hexdigest() != heartbeat.token_hash(b)


def test_the_key_pin_is_curls():
    cert, _key = tls.make_self_signed("spark", [])
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    spki = x509.load_pem_x509_certificate(cert).public_key().public_bytes(
        Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    assert tls.public_key_pin(cert) == "sha256//" + base64.b64encode(hashlib.sha256(spki).digest()).decode()


# --------------------------------------------------------------------------
# End to end: the form, the URL, the pings, the alerts
# --------------------------------------------------------------------------


@pytest.fixture
def site(monkeypatch):
    monkeypatch.setattr(routes_heartbeat, "limiter", routes_heartbeat.Limiter())
    config = _config()
    with TestClient(create_app(config), follow_redirects=False) as client:
        client.post("/setup", data={"setup_code": client.app.state.setup_code, "username": "admin",
                                    "password": PASSWORD, "password_confirm": PASSWORD,
                                    "timezone": "UTC"})
        yield client, config


FORM = {"name": "Nightly backup", "every": "1", "every_unit": "days", "grace": "1",
        "grace_unit": "hours"}


def create(client, **over) -> str:  # type: ignore[no-untyped-def]
    page = client.post("/targets/heartbeat/new", data={**FORM, **over})
    assert page.status_code == 200, page.text
    return re.search(r"/hb/([A-Za-z0-9_-]{32})", page.text).group(1)


def target() -> Target:
    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            row = await s.scalar(select(Target).where(Target.check_type == CheckType.HEARTBEAT))
            s.expunge(row)
            return row
    return run(go())


def judged_at(monkeypatch, when) -> None:  # type: ignore[no-untyped-def]
    """The minute's evaluation, run as if it were `when`."""
    monkeypatch.setattr(runner, "utcnow", lambda: when)
    run(runner.run_target(target().id))


def subjects() -> list[str]:
    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            return [n.subject for n in (await s.execute(select(Notification).order_by(Notification.id))).scalars()
                    if not n.kind.startswith("security_")]
    return run(go())


def results() -> int:
    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            return len((await s.execute(select(CheckResult))).all())
    return run(go())


class TestLife:
    def test_made_shown_once_and_waiting(self, site):
        client, config = site
        page = flat(client.post("/targets/heartbeat/new", data=FORM).text)
        token = re.search(r"/hb/([A-Za-z0-9_-]{32})", page).group(1)
        assert "Copy the URL now." in page and f"http://testserver/hb/{token}/fail" in page
        assert "curl -fsS -m 10 --retry 3 http://testserver/hb/" in page
        t = target()
        assert t.heartbeat_hash == heartbeat.token_hash(token) and t.address == "heartbeat"
        assert (t.interval_seconds, t.failure_threshold, t.recovery_threshold) == (60, 1, 1)
        assert t.params == PARAMS and t.device_id is None
        listing = flat(client.get("/targets").text)
        assert "Waiting for the first ping." in listing and "pinged by the job" in listing
        assert f'href="/targets/{t.id}/heartbeat"' in listing
        assert token not in listing, "the URL is never shown again"
        raw = b"".join(Path(str(config.app.db_path) + x).read_bytes()
                       for x in ("", "-wal") if Path(str(config.app.db_path) + x).exists())
        assert token.encode() not in raw

    def test_judging_before_the_first_is_due_records_nothing(self, site, monkeypatch):
        client, _ = site
        create(client)
        judged_at(monkeypatch, target().created_at + 12 * HOUR)
        assert results() == 0 and target().status == HealthStatus.UNKNOWN

    def test_late_down_then_a_ping_brings_it_back(self, site, monkeypatch):
        client, _ = site
        token = create(client)
        judged_at(monkeypatch, target().created_at + DAY + 2 * HOUR)
        assert target().status == HealthStatus.DOWN
        assert subjects() == ["Nightly backup has not checked in"]
        answer = client.get(f"/hb/{token}")
        assert answer.status_code == 200 and answer.text == "ok\n"
        assert answer.headers["content-type"].startswith("text/plain")
        assert target().status == HealthStatus.UP and target().last_ping_at is not None
        assert subjects() == ["Nightly backup has not checked in", "Nightly backup checked in again"]

    def test_a_ping_before_it_is_due_is_up_at_once(self, site):
        client, _ = site
        token = create(client)
        assert client.post(f"/hb/{token}").status_code == 200
        assert client.head(f"/hb/{token}").status_code == 200
        assert target().status == HealthStatus.UP and subjects() == []

    def test_fail_says_why_and_the_next_success_clears_it(self, site):
        client, _ = site
        token = create(client)
        client.get(f"/hb/{token}")
        answer = client.post(f"/hb/{token}/fail", content=b"exit 3: disk full\nand more")
        assert answer.status_code == 200
        assert target().status == HealthStatus.DOWN
        assert subjects() == ["Nightly backup reported a failure"]
        assert "The job said it failed: exit 3: disk full." in flat(client.get("/targets").text)
        client.get(f"/hb/{token}")
        assert target().status == HealthStatus.UP and "failed" not in target().params
        assert subjects()[-1] == "Nightly backup checked in again"

    def test_paused_takes_the_ping_and_changes_nothing(self, site):
        client, _ = site
        token = create(client)
        client.post(f"/targets/{target().id}/toggle")
        answer = client.get(f"/hb/{token}")
        assert answer.text == "ok, but this heartbeat is paused\n"
        assert target().last_ping_at is not None and target().status != HealthStatus.UP

    def test_a_new_url_and_the_old_one_stops(self, site):
        client, _ = site
        old = create(client)
        page = client.post(f"/targets/{target().id}/heartbeat/url").text
        new = re.search(r"/hb/([A-Za-z0-9_-]{32})", page).group(1)
        assert new != old
        assert client.get(f"/hb/{old}").status_code == 404
        assert client.get(f"/hb/{new}").status_code == 200

    def test_edit_keeps_the_url_and_a_standing_failure(self, site):
        client, _ = site
        token = create(client)
        client.get(f"/hb/{token}/fail")
        ident = target().id
        page = flat(client.get(f"/targets/{ident}/edit", follow_redirects=True).text)
        assert "Make a new ping URL" in page and 'value="Nightly backup"' in page
        answer = client.post(f"/targets/{ident}/heartbeat", data={**FORM, "name": "Backup", "every": "6",
                                                                 "every_unit": "hours"})
        assert answer.status_code == 303
        t = target()
        assert t.name == "Backup" and t.params["period_seconds"] == 6 * 3600 and t.params["failed"]
        assert client.get(f"/hb/{token}").status_code == 200

    @pytest.mark.parametrize("over, message", [
        ({"name": "  "}, "Give the heartbeat a name"),
        ({"every": "0"}, "every minute at most"),
        ({"every": "32", "every_unit": "days"}, "every 31 days at least"),
        ({"every_unit": "weeks"}, "whole number of minutes, hours or days"),
        ({"grace": "8", "grace_unit": "days"}, "a minute to 7 days"),
        ({"grace": "x"}, "whole number"),
    ])
    def test_refused_says_why(self, site, over, message):
        client, _ = site
        answer = client.post("/targets/heartbeat/new", data={**FORM, **over})
        assert answer.status_code == 400 and message in answer.text
        async def count():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                return len((await s.execute(select(Target))).all())
        assert run(count()) == 0


class TestPingRoute:
    def test_wrong_tokens_all_read_the_same(self, site):
        client, _ = site
        create(client)
        for token in ("short", "x" * 32, "a" * 64, "bad%20chars-not-a-token-at-all"):
            answer = client.get(f"/hb/{token}")
            assert answer.status_code == 404 and answer.text == "not found\n"

    def test_ten_wrong_tokens_and_that_address_waits(self, site):
        client, _ = site
        token = create(client)
        for _ in range(routes_heartbeat.Limiter.MISSES_PER_WINDOW):
            client.get("/hb/" + "z" * 32)
        answer = client.get(f"/hb/{token}")
        assert answer.status_code == 429 and answer.headers["retry-after"] == "60"

    def test_thirty_pings_a_minute(self, site):
        client, _ = site
        token = create(client)
        codes = [client.get(f"/hb/{token}").status_code for _ in range(31)]
        assert codes.count(200) == 30 and codes[-1] == 429

    def test_no_session_needed_and_none_given(self, site):
        client, _ = site
        token = create(client)
        client.cookies.clear()
        answer = client.get(f"/hb/{token}")
        assert answer.status_code == 200 and "set-cookie" not in answer.headers

    def test_the_limiter_forgets(self):
        limiter = routes_heartbeat.Limiter()
        for i in range(10):
            limiter.miss("10.0.0.9", now=float(i))
        assert not limiter.allow("10.0.0.9", now=10.0)
        assert limiter.allow("10.0.0.9", now=10.0 + routes_heartbeat.Limiter.MISS_WINDOW)


class TestKeptApart:
    def test_not_in_the_target_form_nor_dependable(self, site):
        client, _ = site
        create(client)
        page = client.get("/targets/new").text
        assert '<option value="heartbeat"' not in page and "Nightly backup" not in page
        answer = client.post("/targets/new", data={"name": "x", "check_type": "heartbeat",
                                                   "address": "x"})
        assert answer.status_code == 400 and "Add heartbeat" in answer.text

    def test_the_generic_edit_will_not_take_one(self, site):
        client, _ = site
        create(client)
        answer = client.post(f"/targets/{target().id}/edit", data={"name": "x", "check_type": "ping",
                                                                   "address": "10.0.0.1"})
        assert answer.status_code == 400
        assert target().check_type == CheckType.HEARTBEAT

    def test_a_device_named_heartbeat_does_not_claim_it(self, site):
        client, _ = site
        from spark.models import Device

        async def device():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                s.add(Device(mac="aa:00:00:00:00:09", primary_ip="10.0.0.9", hostname="heartbeat",
                             last_seen=utcnow()))
        run(device())
        create(client)
        assert 'class="target-link"' not in client.get("/targets").text

    def test_the_targets_filter_offers_it(self, site):
        client, _ = site
        create(client)
        assert '<option value="heartbeat">HEARTBEAT</option>' in client.get("/targets").text

    def test_signed_out_cannot_make_one(self):
        config = _config()
        with TestClient(create_app(config), follow_redirects=False) as client:
            assert client.post("/targets/heartbeat/new", data=FORM).status_code in (302, 303, 307)


def test_migration_26_from_a_version_25_database():
    from sqlalchemy import inspect

    async def shape(wind_back: bool):  # type: ignore[no-untyped-def]
        config = _config()
        D.init_engine(config)
        await D.init_db(config)
        if wind_back:
            async with D.session_scope() as s:
                await s.execute(text("DROP INDEX ix_target_heartbeat_hash"))
                await s.execute(text("ALTER TABLE target DROP COLUMN heartbeat_hash"))
                await s.execute(text("ALTER TABLE target DROP COLUMN last_ping_at"))
                await D._set_version(s, 25)
            await D.close_engine()
            D.init_engine(config)
            await D.init_db(config)
        async with D.session_scope() as s:
            connection = await s.connection()
            shape = await connection.run_sync(lambda sync: (
                sorted(c["name"] for c in inspect(sync).get_columns("target")),
                sorted((i["name"], bool(i["unique"])) for i in inspect(sync).get_indexes("target"))))
            version = await D._get_version(s)
        await D.close_engine()
        return shape, version

    migrated, fresh = run(shape(True)), run(shape(False))
    assert migrated == fresh
    assert ("ix_target_heartbeat_hash", True) in fresh[0][1]
    assert fresh[1] == D.CURRENT_VERSION >= 26
