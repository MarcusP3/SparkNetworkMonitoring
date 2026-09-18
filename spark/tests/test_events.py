"""Tests for live updates.

The valuable assertions here are about what does *not* get published. An event
per check would be a request per check per open tab, which is the difference
between a page that updates itself and a page that hammers the server it is
monitoring.
"""

from __future__ import annotations

import asyncio
import socket
import tempfile
from pathlib import Path

import pytest

from spark import db as D
from spark import events
from spark.config import Config
from spark.engine.runner import run_target
from spark.models import CheckType, HealthStatus, Target


@pytest.fixture(autouse=True)
def clean_subscribers():
    yield
    for queue in list(events._subscribers):
        events.unsubscribe(queue)


@pytest.fixture
def engine_db():
    tmp = Path(tempfile.mkdtemp(prefix="spark-events-"))
    config = Config.model_validate(
        {"app": {"data_dir": str(tmp / "data")}, "network": {"subnets": []}}
    )
    config.app.data_dir.mkdir(parents=True, exist_ok=True)

    async def setup():
        D.init_engine(config)
        await D.init_db(config)

    asyncio.run(setup())
    yield
    asyncio.run(D.close_engine())


def _dead_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class TestPubSub:
    def test_publish_reaches_every_subscriber(self):
        async def go():
            a, b = events.subscribe(), events.subscribe()
            events.publish({"kind": "status"})
            return await a.get(), await b.get()

        first, second = asyncio.run(go())
        assert first == second == {"kind": "status"}

    def test_a_stalled_subscriber_cannot_grow_without_bound(self):
        async def go():
            queue = events.subscribe()
            for index in range(events.QUEUE_SIZE * 10):
                events.publish({"n": index})
            return queue.qsize()

        # A laptop asleep with a tab open must not be a memory leak.
        assert asyncio.run(go()) == events.QUEUE_SIZE

    def test_a_stalled_subscriber_keeps_the_newest_events(self):
        async def go():
            queue = events.subscribe()
            for index in range(events.QUEUE_SIZE + 5):
                events.publish({"n": index})
            drained = [queue.get_nowait() for _ in range(queue.qsize())]
            return drained[-1]["n"]

        # Dropping the oldest, not refusing the newest: a stale page should
        # catch up to now, not resume from history.
        assert asyncio.run(go()) == events.QUEUE_SIZE + 4

    def test_publish_with_no_subscribers_is_harmless(self):
        events.publish({"kind": "status"})
        assert events.subscriber_count() == 0

    def test_unsubscribe_is_idempotent(self):
        async def go():
            queue = events.subscribe()
            events.unsubscribe(queue)
            events.unsubscribe(queue)
            return events.subscriber_count()

        assert asyncio.run(go()) == 0


class TestRunnerPublishing:
    def test_publishes_only_on_a_real_transition(self, engine_db):
        async def go():
            port = _dead_port()
            async with D.session_scope() as session:
                target = Target(
                    name="dead",
                    check_type=CheckType.TCP,
                    address="127.0.0.1",
                    params={"port": port},
                    timeout_seconds=1,
                    failure_threshold=2,
                    recovery_threshold=1,
                    status=HealthStatus.UP,
                )
                session.add(target)
                await session.flush()
                target_id = target.id

            queue = events.subscribe()
            await run_target(target_id)
            below_threshold = queue.qsize()
            await run_target(target_id)
            at_threshold = queue.qsize()
            event = await asyncio.wait_for(queue.get(), timeout=1)

            # A third failing check changes nothing; it must stay silent.
            await run_target(target_id)
            after = queue.qsize()
            return below_threshold, at_threshold, event, after

        below, at, event, after = asyncio.run(go())
        assert below == 0, "hysteresis not met yet - nothing to tell anyone"
        assert at == 1
        assert event["kind"] == "status"
        assert event["from"] == "up" and event["to"] == "down"
        assert after == 0, "still down is not news"

    def test_committed_before_the_event_fires(self, engine_db):
        """A page reloading on the event must not read the pre-change state."""

        async def go():
            port = _dead_port()
            async with D.session_scope() as session:
                target = Target(
                    name="dead",
                    check_type=CheckType.TCP,
                    address="127.0.0.1",
                    params={"port": port},
                    timeout_seconds=1,
                    failure_threshold=1,
                    status=HealthStatus.UP,
                )
                session.add(target)
                await session.flush()
                target_id = target.id

            queue = events.subscribe()
            await run_target(target_id)
            await asyncio.wait_for(queue.get(), timeout=1)
            # Read in a fresh session, exactly as the refreshing page would.
            async with D.session_scope() as session:
                return (await session.get(Target, target_id)).status

        assert asyncio.run(go()) is HealthStatus.DOWN

    def test_a_disabled_target_publishes_nothing(self, engine_db):
        async def go():
            async with D.session_scope() as session:
                target = Target(
                    name="paused",
                    check_type=CheckType.TCP,
                    address="127.0.0.1",
                    params={"port": _dead_port()},
                    timeout_seconds=1,
                    failure_threshold=1,
                    enabled=False,
                    status=HealthStatus.PAUSED,
                )
                session.add(target)
                await session.flush()
                target_id = target.id

            queue = events.subscribe()
            await run_target(target_id)
            return queue.qsize()

        assert asyncio.run(go()) == 0

    def test_a_missing_target_publishes_nothing(self, engine_db):
        async def go():
            queue = events.subscribe()
            await run_target(9999)
            return queue.qsize()

        assert asyncio.run(go()) == 0
