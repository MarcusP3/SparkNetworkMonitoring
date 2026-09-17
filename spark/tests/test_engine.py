"""Tests for the check engine.

The state machine gets the most attention here because it is the part that is
both easy to get subtly wrong and impossible to notice in production: a
hysteresis off-by-one doesn't crash, it just pages you at 3 a.m. for a dropped
packet, or stays silent through a real outage.
"""

from __future__ import annotations

import asyncio
import socket
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import func, select

from spark import db as D
from spark.checks.base import CheckOutcome, CheckSpec, describe_exception
from spark.checks.net import _split_host_port, check_dns, check_tcp, run_check
from spark.config import Config
from spark.engine.state import apply_outcome, human_duration, observed_status
from spark.models import CheckResult, CheckType, HealthStatus, Incident, Target


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def session_factory():
    """A fresh on-disk database per test, torn down afterwards."""
    tmp = Path(tempfile.mkdtemp(prefix="spark-engine-"))
    config = Config.model_validate(
        {"app": {"data_dir": str(tmp / "data")}, "network": {"subnets": []}}
    )
    config.app.data_dir.mkdir(parents=True, exist_ok=True)

    async def setup():
        D.init_engine(config)
        await D.init_db(config)

    asyncio.run(setup())
    yield D.session_scope
    asyncio.run(D.close_engine())


def make_target(**kwargs) -> Target:
    defaults = dict(
        name="t",
        check_type=CheckType.PING,
        address="192.0.2.1",
        failure_threshold=3,
        recovery_threshold=2,
        status=HealthStatus.UNKNOWN,
    )
    defaults.update(kwargs)
    return Target(**defaults)


UP = CheckOutcome.up(latency_ms=1.0, detail="ok")
DOWN = CheckOutcome.down("no reply")
DEGRADED = CheckOutcome.warn("25% loss", latency_ms=9.0)


# --------------------------------------------------------------------------
# Outcome mapping
# --------------------------------------------------------------------------


class TestObservedStatus:
    def test_ok_is_up(self):
        assert observed_status(UP) is HealthStatus.UP

    def test_ok_but_degraded_is_degraded(self):
        assert observed_status(DEGRADED) is HealthStatus.DEGRADED

    def test_not_ok_is_down(self):
        assert observed_status(DOWN) is HealthStatus.DOWN

    def test_degraded_flag_is_ignored_when_not_ok(self):
        # A failing check is down, whatever else it says about itself.
        assert observed_status(CheckOutcome(ok=False, degraded=True)) is HealthStatus.DOWN


# --------------------------------------------------------------------------
# Hysteresis
# --------------------------------------------------------------------------


class TestHysteresis:
    def test_does_not_go_down_before_threshold(self, session_factory):
        async def go():
            async with session_factory() as s:
                target = make_target(status=HealthStatus.UP, failure_threshold=3)
                s.add(target)
                await s.flush()

                statuses = []
                for _ in range(2):
                    await apply_outcome(s, target, DOWN)
                    statuses.append(target.status)
                return statuses, target.consecutive_failures


        statuses, failures = asyncio.run(go())
        # Two failures with a threshold of three is not an outage yet.
        assert all(st is HealthStatus.UP for st in statuses)
        assert failures == 2

    def test_goes_down_exactly_on_threshold(self, session_factory):
        async def go():
            async with session_factory() as s:
                target = make_target(status=HealthStatus.UP, failure_threshold=3)
                s.add(target)
                await s.flush()
                transitions = [await apply_outcome(s, target, DOWN) for _ in range(3)]
                return transitions, target.status

        transitions, status = asyncio.run(go())
        assert [t.changed for t in transitions] == [False, False, True]
        assert transitions[-1].went_down
        assert status is HealthStatus.DOWN

    def test_a_single_success_resets_the_failure_count(self, session_factory):
        async def go():
            async with session_factory() as s:
                target = make_target(status=HealthStatus.UP, failure_threshold=3)
                s.add(target)
                await s.flush()
                await apply_outcome(s, target, DOWN)
                await apply_outcome(s, target, DOWN)
                await apply_outcome(s, target, UP)      # resets
                await apply_outcome(s, target, DOWN)
                await apply_outcome(s, target, DOWN)
                return target.status, target.consecutive_failures

        status, failures = asyncio.run(go())
        # Five failures overall, but never three in a row.
        assert status is HealthStatus.UP
        assert failures == 2

    def test_recovery_needs_its_threshold_too(self, session_factory):
        async def go():
            async with session_factory() as s:
                target = make_target(
                    status=HealthStatus.UP, failure_threshold=1, recovery_threshold=2
                )
                s.add(target)
                await s.flush()
                await apply_outcome(s, target, DOWN)
                after_down = target.status
                first = await apply_outcome(s, target, UP)
                after_one = target.status
                second = await apply_outcome(s, target, UP)
                return after_down, first, after_one, second, target.status

        after_down, first, after_one, second, final = asyncio.run(go())
        assert after_down is HealthStatus.DOWN
        # A flapping target that answers once must not close its own incident.
        assert not first.changed and after_one is HealthStatus.DOWN
        assert second.recovered and final is HealthStatus.UP

    def test_threshold_of_zero_is_treated_as_one(self, session_factory):
        async def go():
            async with session_factory() as s:
                target = make_target(status=HealthStatus.UP, failure_threshold=0)
                s.add(target)
                await s.flush()
                return await apply_outcome(s, target, DOWN)

        # A misconfigured 0 must not mean "never go down".
        assert asyncio.run(go()).went_down


class TestDegraded:
    def test_degraded_is_immediate_and_opens_no_incident(self, session_factory):
        async def go():
            async with session_factory() as s:
                target = make_target(status=HealthStatus.UP)
                s.add(target)
                await s.flush()
                transition = await apply_outcome(s, target, DEGRADED)
                count = await s.scalar(select(func.count()).select_from(Incident))
                return transition, target.status, count

        transition, status, incidents = asyncio.run(go())
        assert transition.changed and status is HealthStatus.DEGRADED
        # An early warning delayed is not an early warning.
        assert transition.incident_opened is None
        assert incidents == 0

    def test_degraded_counts_as_a_success_for_recovery(self, session_factory):
        async def go():
            async with session_factory() as s:
                target = make_target(
                    status=HealthStatus.UP, failure_threshold=1, recovery_threshold=1
                )
                s.add(target)
                await s.flush()
                await apply_outcome(s, target, DOWN)
                transition = await apply_outcome(s, target, DEGRADED)
                return transition, target.status

        transition, status = asyncio.run(go())
        # Reachable-but-impaired ends the outage; it does not prolong it.
        assert transition.recovered
        assert status is HealthStatus.DEGRADED


# --------------------------------------------------------------------------
# Incidents
# --------------------------------------------------------------------------


class TestIncidents:
    def test_incident_opens_once_and_closes_on_recovery(self, session_factory):
        async def go():
            async with session_factory() as s:
                target = make_target(
                    status=HealthStatus.UP, failure_threshold=1, recovery_threshold=1
                )
                s.add(target)
                await s.flush()
                await apply_outcome(s, target, DOWN)
                await apply_outcome(s, target, DOWN)   # still down, no new incident
                await apply_outcome(s, target, UP)
                await s.flush()
                incidents = list(
                    (await s.execute(select(Incident))).scalars().all()
                )
                return incidents

        incidents = asyncio.run(go())
        assert len(incidents) == 1
        assert incidents[0].closed_at is not None
        assert incidents[0].duration_seconds is not None

    def test_records_every_probe_not_just_transitions(self, session_factory):
        async def go():
            async with session_factory() as s:
                target = make_target(status=HealthStatus.UP)
                s.add(target)
                await s.flush()
                for outcome in (UP, DOWN, UP, DEGRADED):
                    await apply_outcome(s, target, outcome)
                await s.flush()
                return await s.scalar(select(func.count()).select_from(CheckResult))

        assert asyncio.run(go()) == 4

    def test_dependency_down_flags_the_incident_as_a_symptom(self, session_factory):
        async def go():
            async with session_factory() as s:
                parent = make_target(name="switch", status=HealthStatus.DOWN)
                s.add(parent)
                await s.flush()
                child = make_target(
                    name="host", status=HealthStatus.UP, failure_threshold=1,
                    depends_on_target_id=parent.id,
                )
                s.add(child)
                await s.flush()
                transition = await apply_outcome(s, child, DOWN)
                return transition

        transition = asyncio.run(go())
        assert transition.went_down
        # The incident is still recorded - you want the history - but flagged
        # so the notifier can stay quiet.
        assert transition.suppressed_by_dependency
        assert transition.incident_opened.suppressed_by_dependency

    def test_dependency_up_does_not_suppress(self, session_factory):
        async def go():
            async with session_factory() as s:
                parent = make_target(name="switch", status=HealthStatus.UP)
                s.add(parent)
                await s.flush()
                child = make_target(
                    name="host", status=HealthStatus.UP, failure_threshold=1,
                    depends_on_target_id=parent.id,
                )
                s.add(child)
                await s.flush()
                return await apply_outcome(s, child, DOWN)

        transition = asyncio.run(go())
        assert transition.went_down
        assert not transition.suppressed_by_dependency


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------


class TestAddressParsing:
    def test_explicit_port_wins(self):
        assert _split_host_port("host", 443) == ("host", 443)

    def test_host_colon_port(self):
        assert _split_host_port("10.1.10.1:8006", None) == ("10.1.10.1", 8006)

    def test_bare_host_has_no_port(self):
        assert _split_host_port("10.1.10.1", None) == ("10.1.10.1", None)

    def test_bracketed_ipv6(self):
        assert _split_host_port("[::1]:8080", None) == ("::1", 8080)

    def test_bare_ipv6_is_not_split_on_its_colons(self):
        host, port = _split_host_port("[fe80::1]", None)
        assert host == "fe80::1" and port is None


class TestTcpCheck:
    def test_open_port_is_up(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        try:
            outcome = asyncio.run(
                check_tcp(CheckSpec(address="127.0.0.1", params={"port": port}))
            )
        finally:
            server.close()
        assert outcome.ok and not outcome.degraded
        assert outcome.latency_ms is not None

    def test_closed_port_is_down_not_an_exception(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        port = server.getsockname()[1]
        server.close()   # nothing is listening now
        outcome = asyncio.run(
            check_tcp(CheckSpec(address="127.0.0.1", params={"port": port}, timeout_seconds=2))
        )
        assert not outcome.ok
        assert outcome.detail   # must say why

    def test_missing_port_is_reported_not_raised(self):
        outcome = asyncio.run(check_tcp(CheckSpec(address="127.0.0.1")))
        assert not outcome.ok
        assert "port" in outcome.detail


class TestDnsCheck:
    def test_unresolvable_name_is_down_not_an_exception(self):
        outcome = asyncio.run(
            check_dns(
                CheckSpec(
                    address="nx.invalid",
                    timeout_seconds=2,
                    params={"server": "127.0.0.1", "rdtype": "A"},
                )
            )
        )
        assert not outcome.ok
        assert outcome.detail


class TestRegistry:
    def test_unknown_check_type_is_down_not_a_crash(self):
        outcome = asyncio.run(run_check("telepathy", CheckSpec(address="x")))
        assert not outcome.ok
        assert "unknown check type" in outcome.detail


class TestHelpers:
    def test_describe_exception_uses_class_name_when_message_is_empty(self):
        # TimeoutError and ConnectionRefusedError often str() to "".
        assert describe_exception(asyncio.TimeoutError()) == "TimeoutError"
        assert "boom" in describe_exception(ValueError("boom"))

    def test_human_duration(self):
        assert human_duration(45) == "45s"
        assert human_duration(252) == "4m 12s"
        assert human_duration(3700) == "1h 1m"
        assert human_duration(200000) == "2d 7h"
        assert human_duration(None) == "an unknown time"
