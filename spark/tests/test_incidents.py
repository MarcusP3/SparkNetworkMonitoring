"""Tests for the incident lifecycle around pausing.

The bug: a target that was down and then paused kept its incident open, because
pausing only changed the status. Resuming set the status to UNKNOWN, so the next
failure read as a fresh transition and opened a *second* incident — and recovery
could never close either, since recovery requires the previous status to be
DOWN and it was UNKNOWN. Three pause cycles, three incidents, all claiming to be
ongoing simultaneously.

None of that raised. An incident with no `closed_at` is a valid row; the only
symptom was a dashboard reporting outages that had ended weeks earlier.

So the assertions here are about the invariant that was never stated: a target
can be down at most once at a time, and therefore has at most one open incident.
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, inspect, select, text

from spark import db as D
from spark.checks.base import CheckOutcome
from spark.config import Config
from spark.engine.state import (
    PAUSED,
    RECOVERED,
    SUPERSEDED,
    apply_outcome,
    close_open_incidents,
)
from spark.models import CheckType, HealthStatus, Incident, Target, utcnow

DOWN = CheckOutcome(ok=False, detail="no reply to 3 echo request(s)")
UP = CheckOutcome(ok=True, latency_ms=1.2)


def make_config(tmp: Path) -> Config:
    config = Config.model_validate(
        {"app": {"data_dir": str(tmp / "data"), "port": 9704, "log_level": "WARNING"},
         "network": {"subnets": []}}
    )
    config.app.data_dir.mkdir(parents=True, exist_ok=True)
    return config


@pytest.fixture
def db():
    tmp = Path(tempfile.mkdtemp(prefix="spark-incidents-"))
    cfg = make_config(tmp)

    async def setup():
        D.init_engine(cfg)
        await D.init_db(cfg)
        async with D.session_scope() as session:
            session.add(Target(name="pi", check_type=CheckType.PING,
                               address="10.1.10.14", failure_threshold=2,
                               recovery_threshold=2))

    asyncio.run(setup())
    yield cfg
    asyncio.run(D.close_engine())


async def drive(outcome: CheckOutcome, times: int = 1) -> None:
    """Feed the state machine, committing each result as the runner does."""
    for _ in range(times):
        async with D.session_scope() as session:
            target = await session.get(Target, 1)
            await apply_outcome(session, target, outcome)


async def pause() -> None:
    """What the toggle route does when pausing."""
    async with D.session_scope() as session:
        target = await session.get(Target, 1)
        target.enabled = False
        target.status = HealthStatus.PAUSED
        await close_open_incidents(session, target.id, resolution=PAUSED)


async def resume() -> None:
    async with D.session_scope() as session:
        target = await session.get(Target, 1)
        target.enabled = True
        target.status = HealthStatus.UNKNOWN
        target.consecutive_failures = 0
        target.consecutive_successes = 0


async def incidents() -> list[Incident]:
    async with D.session_scope() as session:
        return list(
            (await session.execute(select(Incident).order_by(Incident.opened_at)))
            .scalars()
            .all()
        )


async def open_count() -> int:
    async with D.session_scope() as session:
        return int(await session.scalar(
            select(func.count()).select_from(Incident).where(Incident.closed_at.is_(None))
        ) or 0)


class TestOneOpenIncidentAtATime:
    def test_the_reported_bug(self, db):
        """Pause and resume a down target three times."""

        async def go():
            await drive(DOWN, 2)                  # opens one
            for _ in range(3):
                await pause()
                await resume()
                await drive(DOWN, 2)
            return await open_count(), len(await incidents())

        still_open, total = asyncio.run(go())
        # One open incident is the invariant; four rows is the right history.
        #
        # Each pause genuinely ended an observation and each resume began a new
        # one, so four separate outages is what was actually seen. Merging them
        # into a single incident would claim knowledge of the gaps, which is
        # the opposite mistake. What was broken is that all four used to be
        # open at once, every one reporting an ongoing outage.
        assert still_open == 1, f"{still_open} incidents claim to be ongoing at once"
        assert total == 4

    def test_pausing_closes_the_open_incident(self, db):
        async def go():
            await drive(DOWN, 2)
            opened = await open_count()
            await pause()
            return opened, await open_count(), (await incidents())[0].resolution

        opened, after, resolution = asyncio.run(go())
        assert opened == 1 and after == 0
        # Not "recovered": nobody knows whether it came back, only that we
        # stopped looking.
        assert resolution == PAUSED

    def test_a_paused_incident_has_a_real_duration(self, db):
        async def go():
            await drive(DOWN, 2)
            await pause()
            return (await incidents())[0]

        incident = asyncio.run(go())
        assert incident.closed_at is not None
        assert incident.duration_seconds is not None and incident.duration_seconds >= 0

    def test_going_down_again_continues_rather_than_duplicates(self, db):
        async def go():
            await drive(DOWN, 2)
            # Force the state back to UNKNOWN without closing anything, which
            # is exactly what resuming used to do.
            async with D.session_scope() as session:
                target = await session.get(Target, 1)
                target.status = HealthStatus.UNKNOWN
                target.consecutive_failures = 0
            await drive(DOWN, 2)
            return len(await incidents()), await open_count()

        total, still_open = asyncio.run(go())
        assert total == 1 and still_open == 1

    def test_recovery_closes_every_open_incident_not_just_the_newest(self, db):
        async def go():
            now = utcnow()
            async with D.session_scope() as session:
                # A database that already contains the corruption.
                for minutes in (30, 20, 10):
                    session.add(Incident(target_id=1,
                                         opened_at=now - timedelta(minutes=minutes)))
                target = await session.get(Target, 1)
                target.status = HealthStatus.DOWN
            await drive(UP, 2)
            return await open_count(), [i.resolution for i in await incidents()]

        still_open, resolutions = asyncio.run(go())
        # Closing only the newest leaves the older ones open forever; they
        # would still read as ongoing outages years from now.
        assert still_open == 0
        assert resolutions.count(SUPERSEDED) == 2 and resolutions.count(RECOVERED) == 1

    def test_a_normal_outage_still_opens_and_closes_one_incident(self, db):
        async def go():
            await drive(DOWN, 2)
            await drive(UP, 2)
            rows = await incidents()
            return len(rows), rows[0].closed_at is not None, rows[0].resolution

        total, closed, resolution = asyncio.run(go())
        # The fix must not break the ordinary path it sits in the middle of.
        assert total == 1 and closed
        assert resolution == RECOVERED

    def test_a_single_failure_still_opens_nothing(self, db):
        async def go():
            await drive(DOWN, 1)          # threshold is 2
            return len(await incidents())

        assert asyncio.run(go()) == 0


class TestMigration:
    def test_a_version_3_database_gains_the_column_and_is_repaired(self):
        """The migration, against a database carrying the actual corruption."""
        tmp = Path(tempfile.mkdtemp(prefix="spark-incident-migrate-"))
        cfg = make_config(tmp)
        now = utcnow()

        async def build_broken():
            D.init_engine(cfg)
            await D.init_db(cfg)
            async with D.session_scope() as session:
                session.add(Target(name="pi", check_type=CheckType.PING,
                                   address="10.1.10.14"))
            async with D.session_scope() as session:
                for minutes in (60, 40, 20):
                    session.add(Incident(target_id=1,
                                         opened_at=now - timedelta(minutes=minutes)))
            async with D.session_scope() as session:
                # Wind back to before the column existed.
                await session.execute(text("ALTER TABLE incident DROP COLUMN resolution"))
                await D._set_version(session, 3)
            await D.close_engine()

        async def upgrade():
            D.init_engine(cfg)
            await D.init_db(cfg)
            async with D.session_scope() as session:
                version = await D._get_version(session)
                rows = list(
                    (await session.execute(select(Incident).order_by(Incident.opened_at)))
                    .scalars().all()
                )
                data = [(r.closed_at, r.resolution) for r in rows]
            await D.close_engine()
            return version, data

        asyncio.run(build_broken())
        version, data = asyncio.run(upgrade())

        assert version == D.CURRENT_VERSION >= 5
        # The two superseded ones are closed at the moment the next opened --
        # the last instant each can honestly be said to have been running.
        assert data[0][0] == now - timedelta(minutes=40)
        assert data[1][0] == now - timedelta(minutes=20)
        assert data[0][1] == data[1][1] == SUPERSEDED
        # The newest stays open: if the target is still down, it really is.
        assert data[2][0] is None and data[2][1] is None

    def test_the_migrated_schema_matches_a_fresh_one(self):
        """Hand-written DDL drifts from the model; this is the tripwire.

        Migration 4 is an ALTER TABLE rather than something generated from the
        model, so nothing but a comparison stops the two diverging.
        """
        fresh = Path(tempfile.mkdtemp(prefix="spark-schema-fresh-"))
        migrated = Path(tempfile.mkdtemp(prefix="spark-schema-migrated-"))

        async def columns_of(cfg, wind_back: bool) -> set[str]:
            D.init_engine(cfg)
            await D.init_db(cfg)
            if wind_back:
                async with D.session_scope() as session:
                    await session.execute(
                        text("ALTER TABLE incident DROP COLUMN resolution")
                    )
                    await D._set_version(session, 3)
                await D.close_engine()
                D.init_engine(cfg)
                await D.init_db(cfg)
            async with D.session_scope() as session:
                connection = await session.connection()
                names = await connection.run_sync(
                    lambda sync: {c["name"] for c in inspect(sync).get_columns("incident")}
                )
            await D.close_engine()
            return names

        a = asyncio.run(columns_of(make_config(fresh), wind_back=False))
        b = asyncio.run(columns_of(make_config(migrated), wind_back=True))
        assert a == b, f"migrated schema differs from create_all: {a ^ b}"
