"""Watch selected: tick devices on the Devices page, watch them all at once.

It must do exactly what pressing Watch on each row would -- a ping target per
device, the device reviewed -- skip anything that cannot or should not be
watched, answer at once however many were ticked, and bring you back to the
list you were working down.
"""

from __future__ import annotations

import asyncio
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from spark import db as D
from spark import scheduler as scheduler_module
from spark.config import Config
from spark.main import create_app
from spark.models import CheckType, Device, Target, utcnow

PASSWORD = "correct horse battery"


def _config() -> Config:
    tmp = Path(tempfile.mkdtemp(prefix="spark-bulk-"))
    config = Config.model_validate({
        "app": {"data_dir": str(tmp / "data"), "log_level": "WARNING"},
        "network": {"subnets": []},
    })
    config.app.data_dir.mkdir(parents=True, exist_ok=True)
    return config


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


async def _targets() -> list[Target]:
    async with D.session_scope() as s:
        rows = list((await s.execute(select(Target).order_by(Target.device_id))).scalars())
        for r in rows:
            s.expunge(r)
        return rows


async def _device(device_id: int) -> Device:
    async with D.session_scope() as s:
        device = await s.get(Device, device_id)
        s.expunge(device)
        return device


# 1, 2: watchable. 3: already watched. 4: no address. 5: ignored.
@pytest.fixture
def client(monkeypatch):
    config = _config()

    async def seed():
        D.init_engine(config)
        await D.init_db(config)
        async with D.session_scope() as s:
            for n, (ip, ignored) in enumerate(
                [("10.0.0.1", False), ("10.0.0.2", False), ("10.0.0.3", False),
                 (None, False), ("10.0.0.5", True)], start=1):
                s.add(Device(mac=f"aa:bb:cc:00:00:{n:02x}", primary_ip=ip, friendly_name=f"dev{n}",
                             ignored=ignored, last_seen=utcnow(), acknowledged=False))
            await s.flush()
            s.add(Target(name="dev3", check_type=CheckType.PING, address="10.0.0.3", device_id=3))
        await D.close_engine()
    run(seed())

    async def not_awaited(target_id):  # type: ignore[no-untyped-def]
        raise AssertionError("Watch selected must not run checks inside the request")
    monkeypatch.setattr(scheduler_module, "run_now", not_awaited)

    with TestClient(create_app(config), follow_redirects=False) as c:
        c.post("/setup", data={"username": "admin", "password": PASSWORD,
                               "password_confirm": PASSWORD, "timezone": "UTC"})
        yield c


class TestThePage:
    def test_only_devices_that_can_be_watched_get_a_box(self, client):
        page = client.get("/devices?per_page=25").text
        boxes = re.findall(r'<input type="checkbox" class="pick" name="device_id" value="(\d+)"', page)
        assert sorted(boxes) == ["1", "2"]
        assert 'class="pick-all"' in page
        assert '<form method="post" action="/devices/watch-selected" id="bulk-watch"' in page
        assert '<input type="hidden" name="back" value="/devices?per_page=25">' in page

    def test_nothing_to_watch_means_no_boxes_and_no_bar(self, client):
        client.post("/devices/watch-selected", data={"device_id": ["1", "2"]})
        page = client.get("/devices").text
        assert 'class="pick' not in page and 'id="bulk-watch"' not in page


class TestWatching:
    def test_ticked_devices_are_watched_and_the_rest_skipped(self, client):
        response = client.post("/devices/watch-selected",
                               data={"device_id": ["1", "2", "3", "4", "5"],
                                     "back": "/devices?snmp=off&per_page=25"})
        assert response.status_code == 303
        assert response.headers["location"] == "/devices?snmp=off&per_page=25&watched=2"
        targets = run(_targets())
        assert [(t.device_id, t.check_type, t.address, t.name) for t in targets] == [
            (1, CheckType.PING, "10.0.0.1", "dev1"),
            (2, CheckType.PING, "10.0.0.2", "dev2"),
            (3, CheckType.PING, "10.0.0.3", "dev3"),     # the one that was there already
        ]
        assert run(_device(1)).acknowledged and run(_device(2)).acknowledged
        assert not run(_device(5)).acknowledged, "an ignored device is left alone"

    def test_the_page_says_how_many(self, client):
        page = client.get("/devices?watched=2").text
        assert "Now watching 2 more devices." in page and '<a href="/targets">' in page
        page = client.get("/devices?watched=0").text
        assert "Nothing new to watch" in page
        assert "Now watching" not in client.get("/devices?watched=abc").text

    def test_first_checks_are_queued_seconds_apart_not_run_in_the_request(self, client):
        before = datetime.now(timezone.utc)
        client.post("/devices/watch-selected", data={"device_id": ["1", "2"]})
        scheduler = scheduler_module.get_scheduler()
        due = [scheduler.get_job(scheduler_module.job_id(t.id)).next_run_time
               for t in run(_targets()) if t.device_id in (1, 2)]
        assert all(0 < (d - before).total_seconds() < 5 for d in due), due
        assert abs((due[1] - due[0]).total_seconds()) >= 0.15, "spread out, not all at once"

    def test_pressing_twice_makes_no_duplicates(self, client):
        client.post("/devices/watch-selected", data={"device_id": ["1"]})
        response = client.post("/devices/watch-selected", data={"device_id": ["1", "1"]})
        assert response.headers["location"].endswith("watched=0")
        assert [t.device_id for t in run(_targets())].count(1) == 1

    @pytest.mark.parametrize("junk", [["abc"], ["99999999999999999999"], ["-1"], [""], []])
    def test_junk_ids_watch_nothing_and_do_not_fail(self, client, junk):
        response = client.post("/devices/watch-selected", data={"device_id": junk})
        assert response.status_code == 303 and response.headers["location"] == "/devices?watched=0"
        assert len(run(_targets())) == 1

    def test_more_ids_than_any_page_holds_is_refused(self, client):
        response = client.post("/devices/watch-selected",
                               data={"device_id": [str(i) for i in range(1, 502)]})
        assert response.status_code == 400
        assert len(run(_targets())) == 1

    def test_a_second_batch_replaces_the_first_count(self, client):
        response = client.post("/devices/watch-selected",
                               data={"device_id": ["2"], "back": "/devices?watched=1&per_page=25"})
        assert response.headers["location"] == "/devices?per_page=25&watched=1"

    def test_it_only_returns_to_devices(self, client):
        response = client.post("/devices/watch-selected",
                               data={"device_id": ["1"], "back": "https://evil.example/"})
        assert response.headers["location"] == "/devices?watched=1"
