"""Channels: Discord, ntfy, email and a webhook, each with its own copy.

Every alert gets one delivery per channel that takes it, sent, retried and
given up on by itself; the alert's own row is a summary (sent once any
channel has it). Each channel says what it gets. Bursts and quiet-hours
digests are one message on the channels that combine; the webhook gets one
post per alert. Secrets are sealed and never shown. Nothing here reaches a
network: HTTP goes through a MockTransport and email through a fake SMTP.
"""

from __future__ import annotations

import asyncio
import json
import smtplib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from spark import alerts, channels
from spark import db as D
from spark.main import create_app
from spark.models import Delivery, Notification, NotificationStatus, utcnow
from spark.vault import vault_for

from test_alerts import HOOK, _config, _set, at, db, queue, rows  # noqa: F401
from test_credentials import flat

PASSWORD = "correct horse battery"
NTFY = "https://ntfy.example.lan/spark-alerts-7f3k"
WEBHOOK = "http://homeassistant.example.lan:8123/api/webhook/spark-0b2c"


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


class Servers:
    """Discord, ntfy and a webhook receiver, answering as each test asks."""

    def __init__(self, **answers: list[int]):
        self.answers = {k: list(v) for k, v in answers.items()}
        self.posts: dict[str, list[tuple[httpx.Request, dict]]] = {
            "discord": [], "ntfy": [], "webhook": []}

    def _which(self, request: httpx.Request) -> str:
        host = request.url.host
        return "discord" if host == "discord.com" else "ntfy" if host == "ntfy.example.lan" else "webhook"

    async def handler(self, request: httpx.Request) -> httpx.Response:
        which = self._which(request)
        self.posts[which].append((request, json.loads(request.content)))
        queue_ = self.answers.get(which) or []
        status = queue_.pop(0) if queue_ else 200
        if status == 429:
            return httpx.Response(429, headers={"retry-after": "9"}, json={"retry_after": 9})
        return httpx.Response(status)

    def dispatch(self, config) -> int:  # type: ignore[no-untyped-def]
        async def go():  # type: ignore[no-untyped-def]
            async with httpx.AsyncClient(transport=httpx.MockTransport(self.handler)) as client:
                return await alerts.dispatch(config, client=client)
        return run(go())


def setting(**changes) -> None:  # type: ignore[no-untyped-def]
    run(_set(**changes))


def add_ntfy(config, *, gets=None, token="") -> None:  # type: ignore[no-untyped-def]
    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            settings = await alerts.load(s)
            channels.seal_ntfy(settings, vault_for(config), url=NTFY, token=token,
                               gets=gets if gets is not None else list(channels.GROUPS))
            await D.save_setting(s, alerts.SETTING, settings)
    run(go())


def add_hook(config, *, gets=None, token="") -> None:  # type: ignore[no-untyped-def]
    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            settings = await alerts.load(s)
            channels.seal_hook(settings, vault_for(config), url=WEBHOOK, token=token,
                               gets=gets if gets is not None else list(channels.GROUPS))
            await D.save_setting(s, alerts.SETTING, settings)
    run(go())


def add_email(config, *, security="starttls", username="spark", password="mail-pass") -> None:  # type: ignore[no-untyped-def]
    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            settings = await alerts.load(s)
            form = channels.validate_email(host="smtp.example.com", port="587", security=security,
                                           username=username, sender="spark@example.com",
                                           to="me@example.com, you@example.com",
                                           password_given=bool(password), password_saved=False)
            channels.seal_email(settings, vault_for(config), form, password=password,
                                gets=list(channels.GROUPS))
            await D.save_setting(s, alerts.SETTING, settings)
    run(go())


def deliveries() -> list[Delivery]:
    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            found = list((await s.execute(select(Delivery).order_by(Delivery.id))).scalars())
            for d in found:
                s.expunge(d)
            return found
    return run(go())


def by_channel() -> dict[str, Delivery]:
    return {d.channel: d for d in deliveries()}


class FakeSMTP:
    """What channels._smtp_connect returns in these tests: records mail."""

    sent: list = []
    logins: list = []
    fail_login = False
    refuse_connect = False

    def __init__(self, host, port, security, timeout):  # type: ignore[no-untyped-def]
        if FakeSMTP.refuse_connect:
            raise ConnectionRefusedError("connection refused")
        self.where = (host, port, security)

    def login(self, user, password):  # type: ignore[no-untyped-def]
        if FakeSMTP.fail_login:
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 bad credentials")
        FakeSMTP.logins.append((user, password))

    def send_message(self, mail):  # type: ignore[no-untyped-def]
        FakeSMTP.sent.append((self.where, mail))

    def quit(self):  # type: ignore[no-untyped-def]
        pass


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.sent, FakeSMTP.logins = [], []
    FakeSMTP.fail_login = FakeSMTP.refuse_connect = False
    monkeypatch.setattr(channels, "_smtp_connect", FakeSMTP)
    return FakeSMTP


# --------------------------------------------------------------------------
# What a person types
# --------------------------------------------------------------------------


class TestValidation:
    def test_ntfy(self):
        assert channels.validate_ntfy(" https://ntfy.sh/spark-a_1 ") == ("https://ntfy.sh/spark-a_1", "ntfy.sh")
        assert channels.validate_ntfy("http://10.0.0.9:8080/alerts")[1] == "10.0.0.9"
        for bad in ("ntfy.sh/alerts", "https://ntfy.sh/", "https://ntfy.sh/a/b",
                    "https://ntfy.sh/bad topic", "ftp://ntfy.sh/x", "https://u:p@ntfy.sh/x"):
            with pytest.raises(channels.ChannelError):
                channels.validate_ntfy(bad)

    def test_webhook(self):
        assert channels.validate_hook(WEBHOOK)[1] == "homeassistant.example.lan"
        for bad in ("homeassistant:8123/api", "javascript:alert(1)", "https://user:pw@example.com/x"):
            with pytest.raises(channels.ChannelError):
                channels.validate_hook(bad)

    def _email(self, **over):  # type: ignore[no-untyped-def]
        args = dict(host="smtp.example.com", port="587", security="starttls", username="me",
                    sender="spark@example.com", to="a@example.com", password_given=True,
                    password_saved=False)
        return channels.validate_email(**{**args, **over})

    def test_email(self):
        form = self._email(to="a@example.com; b@example.com  c@example.com")
        assert form.to == ["a@example.com", "b@example.com", "c@example.com"] and form.port == 587

    @pytest.mark.parametrize("over, message", [
        ({"host": ""}, "mail server's name"),
        ({"port": "smtp"}, "must be a number"),
        ({"port": "70000"}, "between 1 and 65535"),
        ({"security": "ssl3"}, "STARTTLS, TLS or none"),
        ({"security": "none"}, "never sent unencrypted"),
        ({"sender": "spark"}, "From address"),
        ({"to": ""}, "at least one"),
        ({"to": ",".join(f"u{i}@example.com" for i in range(6))}, "At most 5"),
        ({"to": "a@example.com, nobody"}, "“nobody” does not look like"),
    ])
    def test_email_refused(self, over, message):
        with pytest.raises(channels.ChannelError, match=message):
            self._email(**over)

    def test_a_relay_with_no_password_may_be_plain(self):
        assert self._email(security="none", username="", password_given=False).security == "none"

    def test_groups(self):
        assert channels.group_of("security_lockout", "bad") == "security"
        assert channels.group_of("new_devices", "info") == "news"
        assert channels.group_of("recovered", "ok") == "recoveries"
        assert channels.group_of("down", "bad") == "problems"
        assert channels.group_of("maintenance_left", "bad") == "problems"


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------


class TestEveryChannel:
    def test_one_alert_one_copy_each(self, db, smtp):
        add_ntfy(db, token="tk_secret")
        add_hook(db, token="hook-token")
        add_email(db)
        queue(1)
        servers = Servers()
        assert servers.dispatch(db) == 4
        assert {d.channel: d.status for d in deliveries()} == {
            c: NotificationStatus.SENT for c in ("discord", "ntfy", "webhook", "email")}
        [n] = rows()
        assert n.status == NotificationStatus.SENT and n.error is None

        [(_, discord)] = servers.posts["discord"]
        assert discord["embeds"][0]["title"] == "thing-0 is down"
        [(request, ntfy)] = servers.posts["ntfy"]
        assert str(request.url) == "https://ntfy.example.lan/"
        assert request.headers["authorization"] == "Bearer tk_secret"
        assert ntfy == {"topic": "spark-alerts-7f3k", "title": "thing-0 is down",
                        "message": "`10.0.0.1`", "priority": 4, "tags": ["warning"],
                        "markdown": True}
        [(request, hook)] = servers.posts["webhook"]
        assert request.headers["authorization"] == "Bearer hook-token"
        assert hook["source"] == "SPARK" and hook["kind"] == "down" and hook["tone"] == "bad"
        assert hook["subject"] == "thing-0 is down" and hook["held_for_quiet_hours"] is False

        [((host, port, security), mail)] = smtp.sent
        assert (host, port, security) == ("smtp.example.com", 587, "starttls")
        assert smtp.logins == [("spark", "mail-pass")]
        assert mail["Subject"] == "SPARK: thing-0 is down"
        assert mail["To"] == "me@example.com, you@example.com"
        assert "10.0.0.1" in mail.get_content() and "`" not in mail.get_content()

    def test_one_failing_does_not_hold_the_others(self, db):
        add_ntfy(db)
        queue(1)
        servers = Servers(discord=[500])
        servers.dispatch(db)
        got = by_channel()
        assert got["ntfy"].status == NotificationStatus.SENT
        assert got["discord"].status == NotificationStatus.PENDING and got["discord"].attempts == 1
        [n] = rows()
        assert n.status == NotificationStatus.SENT, "it reached you"
        assert n.error == "Discord: Discord answered 500"
        servers.dispatch(db)
        assert len(servers.posts["ntfy"]) == 1 and len(servers.posts["discord"]) == 1, "not due yet"

    def test_a_refused_token_is_given_up_at_once(self, db):
        add_ntfy(db)
        queue(1)
        Servers(ntfy=[403]).dispatch(db)
        assert by_channel()["ntfy"].status == NotificationStatus.FAILED
        assert by_channel()["ntfy"].error == "ntfy answered 403"

    def test_a_rate_limit_holds_only_its_channel(self, db):
        add_ntfy(db)
        queue(2)
        servers = Servers(ntfy=[429])
        servers.dispatch(db)
        assert len(servers.posts["ntfy"]) == 1 and len(servers.posts["discord"]) == 2
        waiting = [d for d in deliveries() if d.channel == "ntfy"]
        assert all(d.status == NotificationStatus.PENDING for d in waiting)
        assert all(7 < (d.next_attempt_at - utcnow()).total_seconds() <= 9 for d in waiting)

    def test_what_each_channel_gets(self, db):
        add_ntfy(db, gets=["problems"])
        add_hook(db, gets=["recoveries", "security"])
        queue(1, kind="recovered", tone="ok")
        queue(1, kind="security_lockout", tone="bad")
        queue(1, kind="down", tone="bad")
        servers = Servers()
        servers.dispatch(db)
        assert len(servers.posts["discord"]) == 3, "Discord gets everything unless told"
        assert [p["title"] for _, p in servers.posts["ntfy"]] == ["thing-0 is down"]
        assert [p["kind"] for _, p in servers.posts["webhook"]] == ["recovered", "security_lockout"]

    def test_discord_can_be_told_too(self, db):
        add_ntfy(db)
        setting(discord_gets=["security"])
        queue(1)
        servers = Servers()
        servers.dispatch(db)
        assert servers.posts["discord"] == [] and len(servers.posts["ntfy"]) == 1

    def test_taken_by_no_channel_is_dropped_saying_so(self, db):
        setting(discord_gets=[])
        queue(1)
        Servers().dispatch(db)
        [n] = rows()
        assert n.status == NotificationStatus.DROPPED and n.error == "No channel takes this kind of alert."

    def test_a_burst_is_one_message_except_to_the_webhook(self, db):
        add_ntfy(db)
        add_hook(db)
        queue(6)
        servers = Servers()
        servers.dispatch(db)
        [(_, ntfy)] = servers.posts["ntfy"]
        assert ntfy["title"] == "6 alerts" and ntfy["message"].count("is down") == 6
        assert len(servers.posts["webhook"]) == 6, "a program wants each one"
        assert len(servers.posts["discord"]) == 1

    def test_quiet_hours_hold_on_every_channel(self, db, monkeypatch):
        add_ntfy(db)
        add_hook(db)
        setting(quiet_hours_start="00:00", quiet_hours_end="23:59", timezone="UTC")
        monkeypatch.setattr(alerts, "utcnow", lambda: at(3))
        queue(2)
        servers = Servers()
        servers.dispatch(db)
        assert all(not p for p in servers.posts.values())
        assert {d.status for d in deliveries()} == {NotificationStatus.HELD}
        assert {n.status for n in rows()} == {NotificationStatus.HELD}
        monkeypatch.setattr(alerts, "utcnow", lambda: datetime(2026, 9, 25, 23, 59, 30,
                                                               tzinfo=timezone.utc))
        servers.dispatch(db)
        assert servers.posts["ntfy"][0][1]["title"] == "While quiet hours were on: 2 alerts"
        assert [p["held_for_quiet_hours"] for _, p in servers.posts["webhook"]] == [True, True]
        assert {n.status for n in rows()} == {NotificationStatus.SENT}

    def test_email_that_cannot_sign_in_is_given_up(self, db, smtp):
        add_email(db)
        smtp.fail_login = True
        queue(1)
        Servers().dispatch(db)
        assert by_channel()["email"].status == NotificationStatus.FAILED
        assert "refused the user name or password" in by_channel()["email"].error

    def test_email_that_cannot_connect_tries_again(self, db, smtp):
        add_email(db)
        smtp.refuse_connect = True
        queue(1)
        Servers().dispatch(db)
        assert by_channel()["email"].status == NotificationStatus.PENDING
        assert "ConnectionRefusedError" in by_channel()["email"].error

    def test_removing_a_channel_drops_what_waits_for_it(self, db):
        add_ntfy(db)
        queue(1)
        Servers(ntfy=[500]).dispatch(db)

        async def remove():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                settings = await alerts.load(s)
                channels.remove(settings, "ntfy")
                await D.save_setting(s, alerts.SETTING, settings)
                for d in (await s.execute(select(Delivery))).scalars():
                    d.next_attempt_at = utcnow() - timedelta(seconds=1)
        run(remove())
        Servers().dispatch(db)
        assert by_channel()["ntfy"].status == NotificationStatus.DROPPED
        assert by_channel()["ntfy"].error == "That channel was removed."

    def test_an_alert_from_before_channels_carries_its_retries_over(self, db):
        async def old():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                s.add(Notification(kind="down", subject="old one is down", tone="bad",
                                   status=NotificationStatus.PENDING, attempts=2,
                                   next_attempt_at=utcnow() - timedelta(seconds=1)))
        run(old())
        Servers().dispatch(db)
        [d] = deliveries()
        assert d.channel == "discord" and d.attempts == 3 and d.status == NotificationStatus.SENT

    def test_nowhere_to_send_is_dropped_saying_so(self, db):
        async def remove():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                await alerts.set_webhook(s, vault_for(db), None)
        run(remove())
        queue(1)
        Servers().dispatch(db)
        [n] = rows()
        assert n.status == NotificationStatus.DROPPED
        assert n.error == "No Discord webhook or other channel is set up."
        assert deliveries() == []


# --------------------------------------------------------------------------
# The Channels tab
# --------------------------------------------------------------------------


@pytest.fixture
def site():
    config = _config()
    with TestClient(create_app(config), follow_redirects=False) as client:
        client.post("/setup", data={"setup_code": client.app.state.setup_code, "username": "admin",
                                    "password": PASSWORD, "password_confirm": PASSWORD,
                                    "timezone": "UTC"})
        yield client, config


def saved(config):  # type: ignore[no-untyped-def]
    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            return await alerts.load(s)
    return run(go())


ALL_GETS = {f"gets_{g}": "1" for g in channels.GROUPS}


class TestPage:
    def test_nothing_set_up(self, site):
        client, _ = site
        page = flat(client.get("/alerts/channels").text)
        assert "Not set up: paste a webhook under" in page
        assert '<span class="subnav-hint warn">none</span>' in page
        assert '<span class="subnav-hint warn">no channel</span>' in page, "Messages and Rules too"
        assert "No alert channel set up yet" in client.get("/").text

    def test_ntfy_is_sealed_and_never_shown(self, site):
        client, config = site
        answer = client.post("/alerts/channels/ntfy", data={"url": NTFY, "token": "tk_secret",
                                                            "gets_problems": "1"})
        assert answer.status_code == 303 and answer.headers["location"] == "/alerts/channels?saved=ntfy#ntfy"
        ntfy = saved(config)["ntfy"]
        assert ntfy["host"] == "ntfy.example.lan" and ntfy["gets"] == ["problems"]
        assert "spark-alerts-7f3k" not in json.dumps(ntfy) and "tk_secret" not in json.dumps(ntfy)
        page = flat(client.get("/alerts/channels?saved=ntfy").text)
        assert "ntfy saved." in page and "Saved, to ntfy.example.lan" in page
        assert "spark-alerts-7f3k" not in page and "tk_secret" not in page
        assert 'name="gets_problems" value="1" checked' in page
        assert 'name="gets_recoveries" value="1" >' in page or 'name="gets_recoveries" value="1"  >' in page
        raw = b"".join(Path(str(config.app.db_path) + x).read_bytes()
                       for x in ("", "-wal") if Path(str(config.app.db_path) + x).exists())
        assert b"spark-alerts-7f3k" not in raw and b"tk_secret" not in raw
        assert '<span class="subnav-hint">1</span>' in page

    def test_blank_keeps_what_is_saved(self, site):
        client, config = site
        client.post("/alerts/channels/ntfy", data={"url": NTFY, "token": "tk_secret", **ALL_GETS})
        before = saved(config)["ntfy"]
        client.post("/alerts/channels/ntfy", data={"url": "", "token": "", "gets_news": "1"})
        after = saved(config)["ntfy"]
        assert after["url_sealed"] == before["url_sealed"] and after["token_sealed"] == before["token_sealed"]
        assert after["gets"] == ["news"]

    def test_refused_says_why(self, site):
        client, config = site
        answer = client.post("/alerts/channels/ntfy", data={"url": "https://ntfy.sh/", **ALL_GETS})
        assert answer.status_code == 400 and "Give the topic&#39;s address" in answer.text
        assert "ntfy" not in saved(config)
        answer = client.post("/alerts/channels/webhook", data={"url": "", **ALL_GETS})
        assert answer.status_code == 400 and "Give the webhook&#39;s address." in answer.text

    def test_email_keeps_what_was_typed_when_refused(self, site):
        client, _ = site
        answer = client.post("/alerts/channels/email", data={
            "host": "smtp.example.com", "port": "587", "security": "none", "username": "me",
            "password": "pw", "sender": "spark@example.com", "to": "me@example.com"})
        assert answer.status_code == 400
        page = flat(answer.text)
        assert "never sent unencrypted" in page and 'value="smtp.example.com"' in page
        assert 'value="pw"' not in page, "a password is never put back on the page"

    def test_email_saved_shows_no_password(self, site):
        client, config = site
        client.post("/alerts/channels/email", data={
            "host": "smtp.example.com", "port": "465", "security": "tls", "username": "me",
            "password": "mail-pass", "sender": "spark@example.com",
            "to": "me@example.com, you@example.com", **ALL_GETS})
        email = saved(config)["email"]
        assert email["to"] == ["me@example.com", "you@example.com"] and email["port"] == 465
        assert "mail-pass" not in json.dumps(email)
        page = flat(client.get("/alerts/channels").text)
        assert 'value="me@example.com, you@example.com"' in page and "mail-pass" not in page
        assert "Saved: type a new one to replace it" in page

    def test_a_test_says_what_happened(self, site, monkeypatch):
        client, _ = site
        client.post("/alerts/channels/ntfy", data={"url": NTFY, **ALL_GETS})
        page = client.post("/alerts/channels/ntfy/test")
        assert page.status_code == 400 and "The test to ntfy did not arrive" in page.text
        assert "no network in tests" in page.text

        async def fine(channel, message, *, client=None, tz=None):  # type: ignore[no-untyped-def]
            return channels.SendResult(True)
        monkeypatch.setattr(channels, "send", fine)
        page = flat(client.post("/alerts/channels/ntfy/test").text)
        assert "Test sent to ntfy. Check that it arrived." in page
        messages = flat(client.get("/alerts/messages").text)
        assert "<strong>Test alert to ntfy</strong>" in messages
        assert '<span class="muted">ntfy</span> <span class="pill ok">sent</span>' in messages
        assert '<span class="muted">ntfy</span> <span class="pill bad">failed</span>' in messages

    def test_a_test_needs_it_set_up(self, site):
        client, _ = site
        page = client.post("/alerts/channels/webhook/test")
        assert page.status_code == 400 and "Set up Webhook first." in page.text

    def test_remove(self, site):
        client, config = site
        client.post("/alerts/channels/webhook", data={"url": WEBHOOK, **ALL_GETS})
        assert "webhook" in saved(config)
        client.post("/alerts/channels/webhook/delete")
        assert "webhook" not in saved(config)
        assert client.post("/alerts/channels/discord/delete").status_code == 404, \
            "Discord's webhook is removed under Rules"

    def test_discord_gets(self, site):
        client, config = site
        client.post("/settings/alerts", data={"enabled": "1", "notify_on_recovery": "1",
                                              "webhook": HOOK})
        page = flat(client.get("/alerts/channels").text)
        assert "Its webhook is saved under" in page
        client.post("/alerts/channels/discord", data={"gets_problems": "1"})
        assert saved(config)["discord_gets"] == ["problems"]

    def test_signed_out(self):
        config = _config()
        with TestClient(create_app(config), follow_redirects=False) as client:
            assert client.get("/alerts/channels").status_code in (302, 303, 307)
            assert client.post("/alerts/channels/ntfy", data={"url": NTFY}).status_code in (302, 303, 307)
            assert "ntfy" not in saved(config)


def test_migration_25_from_a_version_24_database():
    from sqlalchemy import inspect

    async def shape(wind_back: bool):  # type: ignore[no-untyped-def]
        config = _config()
        D.init_engine(config)
        await D.init_db(config)
        if wind_back:
            async with D.session_scope() as s:
                await s.execute(text("DROP TABLE delivery"))
                await D._set_version(s, 24)
            await D.close_engine()
            D.init_engine(config)
            await D.init_db(config)
        async with D.session_scope() as s:
            connection = await s.connection()
            columns = await connection.run_sync(
                lambda sync: sorted(c["name"] for c in inspect(sync).get_columns("delivery")))
            version = await D._get_version(s)
        await D.close_engine()
        return columns, version

    assert run(shape(True)) == run(shape(False))
    assert run(shape(False))[1] == D.CURRENT_VERSION >= 25
