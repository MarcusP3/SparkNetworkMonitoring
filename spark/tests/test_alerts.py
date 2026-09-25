"""Alerting: what gets sent, what does not, and that it survives Discord.

An alerting system fails in two directions and both are silent from where the
code sits. Too much -- thirty messages when a switch goes, a "recovered" for
something nobody was told broke, the same outage twice -- and people mute the
channel, after which it might as well not exist. Too little -- a lost message
when Discord returned a 500, an outage during quiet hours never mentioned --
and it fails the one time it matters. Each test pins one of those.

Discord is faked with httpx's MockTransport; nothing here touches the network.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from spark import alerts
from spark import db as D
from spark.checks.base import CheckOutcome
from spark.collectors.base import DeviceHealth
from spark.config import Config
from spark.main import create_app
from spark.models import (
    CheckType,
    Device,
    HealthStatus,
    Notification,
    NotificationStatus,
    SnmpDevice,
    Target,
    utcnow,
)
from spark.snmp_config import ProfileInput, save_profile
from spark.vault import vault_for

HOOK = "https://discord.com/api/webhooks/123456789012345678/abcDEF-secret-token_xyz"
PASSWORD = "correct horse battery"


def _config() -> Config:
    tmp = Path(tempfile.mkdtemp(prefix="spark-alerts-"))
    config = Config.model_validate({
        "app": {"data_dir": str(tmp / "data"), "log_level": "WARNING"},
        "network": {"subnets": []},
    })
    config.app.data_dir.mkdir(parents=True, exist_ok=True)
    return config


@pytest.fixture
def db():
    config = _config()

    async def setup():
        D.init_engine(config)
        await D.init_db(config)
        async with D.session_scope() as s:
            s.add(Target(name="gateway", check_type=CheckType.PING, address="10.0.0.1",
                         failure_threshold=2, recovery_threshold=1))
            s.add(Target(name="nas", check_type=CheckType.PING, address="10.0.0.5",
                         failure_threshold=1, recovery_threshold=1, depends_on_target_id=1))
            await alerts.set_webhook(s, vault_for(config), HOOK)

    asyncio.run(setup())
    yield config
    asyncio.run(D.close_engine())


def rows() -> list[Notification]:
    async def go():
        async with D.session_scope() as s:
            result = list((await s.execute(select(Notification).order_by(Notification.id))).scalars())
            for r in result:
                s.expunge(r)
            return result
    return asyncio.run(go())


async def _set(**changes) -> None:  # type: ignore[no-untyped-def]
    async with D.session_scope() as s:
        settings = await alerts.load(s)
        settings.update(changes)
        await D.save_setting(s, alerts.SETTING, settings)


# --------------------------------------------------------------------------
# The webhook
# --------------------------------------------------------------------------


class TestWebhook:
    def test_a_discord_webhook_is_accepted_and_normalised(self):
        assert alerts.validate_webhook(f"  {HOOK}?wait=true ") == HOOK
        assert alerts.validate_webhook(HOOK.replace("discord.com", "discordapp.com"))

    @pytest.mark.parametrize("bad", [
        "", "not a url", HOOK.replace("https", "http"),
        "https://discord.com.evil.example/api/webhooks/1/x",
        "https://evil.example/api/webhooks/1/x",
        "https://discord.com/channels/1/2",
        "https://discord.com/api/webhooks/notanumber/x",
        "https://user:pw@discord.com/api/webhooks/1/x",
        "https://discord.com:8443/api/webhooks/1/x",
        "https://10.0.0.1/api/webhooks/1/x",
    ])
    def test_anything_else_is_refused(self, bad):
        # The server fetches this URL; any other host would make the form a
        # way to send requests into the LAN.
        with pytest.raises(alerts.AlertError):
            alerts.validate_webhook(bad)

    def test_starting_spark_seals_a_plaintext_webhook(self):
        config = _config()

        async def plant():
            D.init_engine(config)
            await D.init_db(config)
            async with D.session_scope() as s:
                settings = await alerts.load(s)
                settings.pop("discord_webhook_sealed", None)
                settings["discord_webhook_url"] = HOOK
                await D.save_setting(s, alerts.SETTING, settings)
            await D.close_engine()

        asyncio.run(plant())
        with TestClient(create_app(config)):
            pass
        settings = saved(config)
        assert "discord_webhook_url" not in settings
        assert alerts.webhook_url(settings, vault_for(config)) == HOOK

    def test_a_plaintext_webhook_from_an_older_version_is_sealed_at_start(self):
        config = _config()

        async def go():
            D.init_engine(config)
            await D.init_db(config)
            async with D.session_scope() as s:
                settings = await alerts.load(s)
                settings.pop("discord_webhook_sealed", None)
                settings["discord_webhook_url"] = HOOK
                await D.save_setting(s, alerts.SETTING, settings)
            async with D.session_scope() as s:
                first = await alerts.seal_plaintext_webhook(s, vault_for(config))
            async with D.session_scope() as s:
                again = await alerts.seal_plaintext_webhook(s, vault_for(config))
                settings = await alerts.load(s)
            await D.close_engine()
            return first, again, settings

        first, again, settings = asyncio.run(go())
        assert first is True and again is False
        assert "discord_webhook_url" not in settings
        assert alerts.webhook_url(settings, vault_for(config)) == HOOK
        raw = b"".join(Path(str(config.app.db_path) + x).read_bytes()
                       for x in ("", "-wal") if Path(str(config.app.db_path) + x).exists())
        assert b"abcDEF-secret-token" not in raw


# --------------------------------------------------------------------------
# Quiet hours
# --------------------------------------------------------------------------


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 24, hour, minute, tzinfo=timezone.utc)


class TestQuietHours:
    def test_an_overnight_window(self):
        s = {"quiet_hours_start": "22:00", "quiet_hours_end": "07:00", "timezone": "UTC"}
        assert alerts.in_quiet_hours(s, at(23))
        assert alerts.in_quiet_hours(s, at(3))
        assert not alerts.in_quiet_hours(s, at(7))
        assert not alerts.in_quiet_hours(s, at(12))
        assert alerts.in_quiet_hours(s, at(22))

    def test_a_daytime_window(self):
        s = {"quiet_hours_start": "09:00", "quiet_hours_end": "17:30", "timezone": "UTC"}
        assert alerts.in_quiet_hours(s, at(12)) and not alerts.in_quiet_hours(s, at(18))

    def test_the_window_is_in_the_saved_time_zone(self):
        # Chicago is UTC-5 in September. Each case below gives the opposite
        # answer if the window were read as UTC.
        s = {"quiet_hours_start": "22:00", "quiet_hours_end": "07:00",
             "timezone": "America/Chicago"}
        assert alerts.in_quiet_hours(s, at(8))        # 03:00 in Chicago
        assert not alerts.in_quiet_hours(s, at(23))   # 18:00 in Chicago

    @pytest.mark.parametrize("start,end", [("", ""), ("22:00", ""), ("08:00", "08:00")])
    def test_no_window_means_never_quiet(self, start, end):
        s = {"quiet_hours_start": start, "quiet_hours_end": end}
        assert not any(alerts.in_quiet_hours(s, at(h)) for h in range(24))

    def test_an_unknown_zone_falls_back_to_utc(self):
        s = {"quiet_hours_start": "01:00", "quiet_hours_end": "02:00", "timezone": "Mars/Base"}
        assert alerts.in_quiet_hours(s, at(1, 30))


# --------------------------------------------------------------------------
# Targets
# --------------------------------------------------------------------------


def run_checks(monkeypatch, target_id: int, *results: bool) -> None:
    """Run the real check runner with the probe itself faked."""
    from spark.engine import runner

    outcomes = iter(results)

    async def fake(check_type, spec):  # type: ignore[no-untyped-def]
        ok = next(outcomes)
        return CheckOutcome.up(1.0) if ok else CheckOutcome.down("no reply to 3 pings")

    monkeypatch.setattr(runner, "run_check", fake)
    for _ in results:
        asyncio.run(runner.run_target(target_id))


class TestTargets:
    def test_down_once_then_back_once(self, db, monkeypatch):
        run_checks(monkeypatch, 1, False, False, False, False, True)
        sent = rows()
        assert [n.kind for n in sent] == ["down", "recovered"]
        down, up = sent
        assert down.subject == "gateway is down" and down.tone == "bad"
        assert "no reply to 3 pings" in down.body and "10.0.0.1" in down.body
        assert up.subject == "gateway is back up" and "down for" in up.body
        assert down.incident_id == up.incident_id is not None

    def test_one_failure_below_the_threshold_is_not_an_alert(self, db, monkeypatch):
        run_checks(monkeypatch, 1, False, True, False, True)
        assert rows() == []

    def test_a_second_outage_is_a_second_alert(self, db, monkeypatch):
        run_checks(monkeypatch, 1, False, False, True, False, False)
        assert [n.kind for n in rows()] == ["down", "recovered", "down"]

    def test_a_failure_explained_by_its_dependency_is_not_sent(self, db, monkeypatch):
        run_checks(monkeypatch, 1, False, False)       # gateway down: alerted
        run_checks(monkeypatch, 2, False, True)        # nas behind it: silent both ways
        assert [n.subject for n in rows()] == ["gateway is down"]

    def test_recovery_of_an_outage_nobody_was_told_about_is_not_sent(self, db, monkeypatch):
        asyncio.run(_set(enabled=False))
        run_checks(monkeypatch, 1, False, False)
        asyncio.run(_set(enabled=True))
        run_checks(monkeypatch, 1, True)
        assert rows() == []

    def test_recovery_alerts_can_be_switched_off(self, db, monkeypatch):
        asyncio.run(_set(notify_on_recovery=False))
        run_checks(monkeypatch, 1, False, False, True)
        assert [n.kind for n in rows()] == ["down"]

    def test_a_muted_target_is_not_alerted(self, db, monkeypatch):
        async def mute():
            async with D.session_scope() as s:
                (await s.get(Target, 1)).muted_until = utcnow() + timedelta(hours=1)
        asyncio.run(mute())
        run_checks(monkeypatch, 1, False, False)
        assert rows() == []

    def test_degraded_is_not_an_alert(self, db, monkeypatch):
        from spark.engine import runner

        async def fake(check_type, spec):  # type: ignore[no-untyped-def]
            return CheckOutcome.warn("20% packet loss")

        monkeypatch.setattr(runner, "run_check", fake)
        asyncio.run(runner.run_target(1))
        assert rows() == []


# --------------------------------------------------------------------------
# SNMP
# --------------------------------------------------------------------------


@pytest.fixture
def snmp_db(db):
    async def add():
        async with D.session_scope() as s:
            s.add(Device(mac="aa:bb:cc:00:00:01", primary_ip="10.0.0.2",
                         friendly_name="core-switch", last_seen=utcnow()))
            await s.flush()
            profile = await save_profile(s, vault_for(db), ProfileInput(
                name="lab", version="v2c", community="x"))
            s.add(SnmpDevice(device_id=1, profile_id=profile.id, enabled=True))
    asyncio.run(add())
    return db


def poll(when: datetime, answered: bool) -> None:
    from spark.snmp_poll import record_poll

    health = (DeviceHealth(reachable=True, uptime_seconds=1000.0 + when.timestamp())
              if answered else DeviceHealth(reachable=False, error="Unreachable: no response"))

    async def go():
        async with D.session_scope() as s:
            await record_poll(s, 1, health, [], when, interval=60)
    asyncio.run(go())


T0 = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


class TestSnmp:
    def test_silent_for_three_minutes_then_back(self, snmp_db):
        poll(T0, True)
        poll(T0 + timedelta(seconds=60), False)
        poll(T0 + timedelta(seconds=120), False)
        assert rows() == [], "two missed polls are not an outage"
        poll(T0 + timedelta(seconds=180), False)
        poll(T0 + timedelta(seconds=240), False)
        poll(T0 + timedelta(seconds=300), True)
        sent = rows()
        assert [n.kind for n in sent] == ["snmp_down", "snmp_up"]
        assert sent[0].subject == "core-switch stopped answering SNMP"
        assert "3m 0s" in sent[0].body and "silent for 5m 0s" in sent[1].body

    def test_a_device_that_never_answered_is_not_an_outage(self, snmp_db):
        for k in range(6):
            poll(T0 + timedelta(minutes=k), False)
        assert rows() == []

    def test_a_target_already_down_on_the_device_covers_it(self, snmp_db):
        async def watch():
            async with D.session_scope() as s:
                s.add(Target(name="switch ping", check_type=CheckType.PING, address="10.0.0.2",
                             device_id=1, status=HealthStatus.DOWN))
        asyncio.run(watch())
        poll(T0, True)
        for k in range(1, 5):
            poll(T0 + timedelta(minutes=k), False)
        assert rows() == []

    def test_snmp_alerts_can_be_switched_off(self, snmp_db):
        asyncio.run(_set(notify_on_snmp=False))
        poll(T0, True)
        for k in range(1, 5):
            poll(T0 + timedelta(minutes=k), False)
        assert rows() == []


# --------------------------------------------------------------------------
# New devices
# --------------------------------------------------------------------------


def sweep(monkeypatch, config, *ips: str) -> None:
    from spark.discovery import runner
    from spark.discovery.sweep import Observation, SubnetResult, SweepReport

    async def fake(plan):  # type: ignore[no-untyped-def]
        report = SweepReport(finished_at=utcnow())
        result = SubnetResult(name="LAN", cidr="10.0.0.0/24", attached=True,
                              probed=254, answered=len(ips))
        result.observations = [Observation(ip=ip, mac=f"aa:bb:cc:dd:00:{i:02x}",
                                           vendor="Espressif") for i, ip in enumerate(ips)]
        report.subnets = [result]
        return report

    async def one_subnet(session):  # type: ignore[no-untyped-def]
        from spark.models import Subnet
        return [Subnet(cidr="10.0.0.0/24", name="LAN", attached=True, enabled=True)]

    monkeypatch.setattr(runner, "sweep_all", fake)
    monkeypatch.setattr(runner, "enabled_subnets", one_subnet)
    asyncio.run(runner.run_sweep(config))


class TestNewDevices:
    def test_the_first_sweep_is_a_baseline_and_later_ones_report(self, db, monkeypatch):
        sweep(monkeypatch, db, "10.0.0.20", "10.0.0.21", "10.0.0.22")
        assert rows() == [], "the first sweep finds everything; none of it is news"
        sweep(monkeypatch, db, "10.0.0.20", "10.0.0.21", "10.0.0.22", "10.0.0.99")
        [n] = rows()
        assert n.subject == "1 new device on the network"
        assert "10.0.0.99" in n.body and "Espressif" in n.body

    def test_new_device_alerts_can_be_switched_off(self, db, monkeypatch):
        asyncio.run(_set(notify_on_new_device=False))
        sweep(monkeypatch, db, "10.0.0.20")
        sweep(monkeypatch, db, "10.0.0.20", "10.0.0.99")
        assert rows() == []


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------


class FakeDiscord:
    """Records posts; answers with whatever status the test queues."""

    def __init__(self, *statuses: int, retry_after: float = 2.5):
        self.statuses = list(statuses)
        self.retry_after = retry_after
        self.posts: list[dict] = []
        self.write_ok: list[bool] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.posts.append(json.loads(request.content))
        # The dispatcher must hold no database connection while Discord
        # answers: a write from elsewhere has to go straight through.
        try:
            async with D.session_scope() as s:
                await D.save_setting(s, "probe", {"at": utcnow().isoformat()})
            self.write_ok.append(True)
        except Exception:  # noqa: BLE001
            self.write_ok.append(False)
        status = self.statuses.pop(0) if self.statuses else 204
        if status == 429:
            return httpx.Response(429, json={"message": "You are being rate limited.",
                                             "retry_after": self.retry_after})
        if status == 404:
            return httpx.Response(404, json={"message": "Unknown Webhook", "code": 10015})
        return httpx.Response(status)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def queue(n: int, kind: str = "down", tone: str = "bad") -> None:
    async def go():
        async with D.session_scope() as s:
            for i in range(n):
                await alerts.enqueue(s, kind=kind, subject=f"thing-{i} is down",
                                     body="`10.0.0.1`", tone=tone)
    asyncio.run(go())


def dispatch(config, discord: FakeDiscord) -> int:
    async def go():
        async with discord.client() as client:
            return await alerts.dispatch(config, client=client)
    return asyncio.run(go())


class TestDispatch:
    def test_a_due_alert_is_posted_once_and_marked_sent(self, db):
        queue(1)
        discord = FakeDiscord()
        assert dispatch(db, discord) == 1
        assert dispatch(db, discord) == 0
        [body] = discord.posts
        assert body["username"] == "SPARK"
        assert body["allowed_mentions"] == {"parse": []}, "a device named @everyone must not ping"
        assert body["embeds"][0]["title"] == "thing-0 is down"
        assert body["embeds"][0]["color"] == alerts.TONES["bad"]
        [n] = rows()
        assert n.status == NotificationStatus.SENT and n.sent_at and n.attempts == 1
        assert discord.write_ok == [True]

    def test_a_server_error_backs_off_and_retries(self, db):
        queue(1)
        discord = FakeDiscord(500)
        dispatch(db, discord)
        [n] = rows()
        assert n.status == NotificationStatus.PENDING and n.attempts == 1
        assert "500" in n.error
        wait = (n.next_attempt_at - utcnow()).total_seconds()
        assert 20 < wait <= 30
        assert dispatch(db, discord) == 0, "not due again yet"

    def test_it_gives_up_after_the_last_retry(self, db):
        queue(1)
        discord = FakeDiscord(*([503] * alerts.MAX_ATTEMPTS))

        async def due_now():
            async with D.session_scope() as s:
                for n in (await s.execute(select(Notification))).scalars():
                    n.next_attempt_at = utcnow() - timedelta(seconds=1)
        for _ in range(alerts.MAX_ATTEMPTS):
            asyncio.run(due_now())
            dispatch(db, discord)
        [n] = rows()
        assert n.status == NotificationStatus.FAILED and n.attempts == alerts.MAX_ATTEMPTS

    def test_a_deleted_webhook_fails_at_once(self, db):
        queue(1)
        dispatch(db, FakeDiscord(404))
        [n] = rows()
        assert n.status == NotificationStatus.FAILED and "Unknown Webhook" in n.error

    def test_the_rate_limit_is_honoured_for_everything_behind_it(self, db):
        queue(2)
        discord = FakeDiscord(429, retry_after=7.0)
        dispatch(db, discord)
        assert len(discord.posts) == 1, "stop at the first 429"
        for n in rows():
            assert n.status == NotificationStatus.PENDING
            assert 5 < (n.next_attempt_at - utcnow()).total_seconds() <= 7

    def test_a_burst_goes_as_one_message(self, db):
        queue(6)
        discord = FakeDiscord()
        dispatch(db, discord)
        [body] = discord.posts
        assert body["embeds"][0]["title"] == "6 alerts"
        assert body["embeds"][0]["description"].count("is down") == 6
        assert all(n.status == NotificationStatus.SENT for n in rows())

    def test_without_a_webhook_alerts_are_dropped_with_the_reason(self, db):
        async def remove():
            async with D.session_scope() as s:
                await alerts.set_webhook(s, vault_for(db), None)
        asyncio.run(remove())
        queue(1)
        discord = FakeDiscord()
        dispatch(db, discord)
        [n] = rows()
        assert discord.posts == []
        assert n.status == NotificationStatus.DROPPED and "No Discord webhook" in n.error

    def test_quiet_hours_hold_then_one_digest(self, db, monkeypatch):
        asyncio.run(_set(quiet_hours_start="00:00", quiet_hours_end="23:59", timezone="UTC"))
        monkeypatch.setattr(alerts, "utcnow", lambda: at(3))
        queue(2)
        discord = FakeDiscord()
        dispatch(db, discord)
        assert discord.posts == []
        assert {n.status for n in rows()} == {NotificationStatus.HELD}

        monkeypatch.setattr(alerts, "utcnow", lambda: datetime(2026, 9, 25, 23, 59, 30,
                                                               tzinfo=timezone.utc))
        dispatch(db, discord)
        [digest] = discord.posts
        assert digest["embeds"][0]["title"] == "While quiet hours were on: 2 alerts"
        assert {n.status for n in rows()} == {NotificationStatus.SENT}


# --------------------------------------------------------------------------
# The Settings card
# --------------------------------------------------------------------------


@pytest.fixture
def site(monkeypatch):
    config = _config()
    with TestClient(create_app(config), follow_redirects=False) as client:
        client.post("/setup", data={"username": "admin", "password": PASSWORD,
                                    "password_confirm": PASSWORD})
        yield client, config


def saved(config) -> dict:  # type: ignore[no-untyped-def]
    async def go():
        D.init_engine(config)
        async with D.session_scope() as s:
            return await alerts.load(s)
    return asyncio.run(go())


FORM = {"enabled": "1", "notify_on_recovery": "1", "notify_on_snmp": "1",
        "notify_on_new_device": "1", "quiet_start": "", "quiet_end": "",
        "timezone": "America/Chicago"}


class TestSettingsCard:
    def test_saving_a_webhook_seals_it_and_never_shows_it(self, site):
        client, config = site
        assert client.post("/settings/alerts", data={**FORM, "webhook": HOOK}).status_code == 303
        settings = saved(config)
        assert alerts.webhook_url(settings, vault_for(config)) == HOOK
        page = client.get("/settings/alerts").text
        assert "abcDEF-secret-token" not in page and "123456789012345678" not in page
        assert "Saved — paste a new one" in page

    def test_a_blank_webhook_keeps_the_saved_one(self, site):
        client, config = site
        client.post("/settings/alerts", data={**FORM, "webhook": HOOK})
        client.post("/settings/alerts", data={**FORM, "webhook": "", "notify_on_snmp": ""})
        settings = saved(config)
        assert alerts.webhook_url(settings, vault_for(config)) == HOOK
        assert settings["notify_on_snmp"] is False

    def test_a_wrong_url_is_refused_with_what_to_do(self, site):
        client, config = site
        response = client.post("/settings/alerts",
                               data={**FORM, "webhook": "https://example.com/hook"})
        assert response.status_code == 400 and "Copy Webhook URL" in response.text
        assert not saved(config).get("discord_webhook_sealed")

    def test_quiet_hours_need_both_ends(self, site):
        client, _ = site
        response = client.post("/settings/alerts", data={**FORM, "quiet_start": "22:00"})
        assert response.status_code == 400 and "both a start and an end" in response.text
        assert client.post("/settings/alerts",
                           data={**FORM, "quiet_start": "25:00", "quiet_end": "07:00"}
                           ).status_code == 400

    def test_send_a_test(self, site, monkeypatch):
        client, config = site
        client.post("/settings/alerts", data={**FORM, "webhook": HOOK})
        posted = []

        async def fake_post(url, body, *, client=None):  # type: ignore[no-untyped-def]
            posted.append((url, body))
            return alerts.SendResult(True)

        monkeypatch.setattr(alerts, "post", fake_post)
        page = client.post("/settings/alerts/test").text
        assert "Test alert sent" in page
        assert posted and posted[0][0] == HOOK
        assert "Test alert" in page and ">sent<" in page  # in the Recent log

    def test_a_failed_test_says_what_discord_said(self, site, monkeypatch):
        client, _ = site
        client.post("/settings/alerts", data={**FORM, "webhook": HOOK})

        async def fake_post(url, body, *, client=None):  # type: ignore[no-untyped-def]
            return alerts.SendResult(False, "Discord answered 404: Unknown Webhook",
                                     permanent=True)

        monkeypatch.setattr(alerts, "post", fake_post)
        response = client.post("/settings/alerts/test")
        assert response.status_code == 400 and "Unknown Webhook" in response.text

    def test_remove_the_webhook(self, site):
        client, config = site
        client.post("/settings/alerts", data={**FORM, "webhook": HOOK})
        client.post("/settings/alerts/webhook/delete")
        assert not saved(config).get("discord_webhook_sealed")
        assert "Send a test" not in client.get("/settings/alerts").text

    def test_the_dashboard_says_when_nothing_can_alert(self, site):
        client, _ = site
        assert "No Discord webhook configured" in client.get("/").text
        client.post("/settings/alerts", data={**FORM, "webhook": HOOK})
        assert "No Discord webhook configured" not in client.get("/").text
        client.post("/settings/alerts", data={**FORM, "enabled": ""})
        assert "Alerts are switched off" in client.get("/").text


def test_setup_seals_a_webhook_and_refuses_a_bad_one():
    config = _config()
    with TestClient(create_app(config), follow_redirects=False) as client:
        bad = client.post("/setup", data={"username": "admin", "password": PASSWORD,
                                          "password_confirm": PASSWORD,
                                          "discord_webhook_url": "https://example.com/x"})
        assert bad.status_code == 400 and "Copy Webhook URL" in bad.text
        good = client.post("/setup", data={"username": "admin", "password": PASSWORD,
                                           "password_confirm": PASSWORD,
                                           "discord_webhook_url": HOOK})
        assert good.status_code in (302, 303)
    settings = saved(config)
    assert "discord_webhook_url" not in settings
    assert alerts.webhook_url(settings, vault_for(config)) == HOOK


# --------------------------------------------------------------------------
# Migration 8
# --------------------------------------------------------------------------


def test_migration_8_replaces_the_old_notification_table():
    config = _config()

    async def go():
        D.init_engine(config)
        await D.init_db(config)
        async with D.session_scope() as s:
            await s.execute(text("DROP TABLE notification"))
            await s.execute(text(
                "CREATE TABLE notification (id INTEGER PRIMARY KEY, ts DATETIME, "
                "channel VARCHAR(32), incident_id INTEGER, subject VARCHAR(512), "
                "body TEXT, ok BOOLEAN, error TEXT)"))
            await D._set_version(s, 7)
        await D.close_engine()
        D.init_engine(config)
        await D.init_db(config)
        async with D.session_scope() as s:
            cols = [r[1] for r in (await s.execute(text("PRAGMA table_info(notification)"))).all()]
            version = await D._get_version(s)
            await alerts.enqueue(s, kind="test", subject="works", dedupe_key="k")
        async with D.session_scope() as s:
            again = await alerts.enqueue(s, kind="test", subject="works", dedupe_key="k")
        await D.close_engine()
        return cols, version, again

    cols, version, again = asyncio.run(go())
    assert version == D.CURRENT_VERSION >= 8
    assert {"status", "attempts", "next_attempt_at", "dedupe_key", "kind"} <= set(cols)
    assert "ok" not in cols
    assert again is None, "the same dedupe key must not queue twice"


def test_the_suite_cannot_reach_discord():
    """conftest.py swaps the default client; this proves the swap is in force."""
    result = asyncio.run(alerts.post(HOOK, alerts.payload([])))
    assert not result.ok and "no network in tests" in result.error
