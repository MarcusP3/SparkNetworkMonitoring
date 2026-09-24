"""Scheduled SNMP polling: counter arithmetic, recording, scheduling, history.

The arithmetic is where this goes wrong without anyone noticing. A wrap handled
as a reset loses a minute; a reset handled as a wrap stores 40 Gbps on a
gigabit port, and a chart scaled to that spike shows every real minute as a
flat line along the bottom. Neither raises. So each rule in `snmp_poll` gets a
test that pins it with numbers worked out by hand.
"""

from __future__ import annotations

import asyncio
import socket
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select, text

from spark import db as D
from spark import scheduler as scheduler_module
from spark.collectors.base import DeviceHealth, InterfaceStat
from spark.config import Config
from spark.models import (
    FIVE_MINUTES,
    ONE_HOUR,
    Device,
    SnmpDevice,
    SnmpHealthRollup,
    SnmpHealthSample,
    SnmpInterface,
    SnmpInterfaceRollup,
    SnmpInterfaceSample,
    SnmpPoll,
    utcnow,
)
from spark.retention import run_retention
from spark.snmp_config import ProfileInput, add_device, save_profile
from spark.snmp_poll import (
    MAX_INTERVAL,
    MIN_INTERVAL,
    WRAP_32,
    Reading,
    clamp_interval,
    counter_delta,
    detect_reboot,
    interface_rates,
    poll_device,
    record_poll,
)
from spark.vault import vault_for

T0 = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)
GIG = 1000


def stat(index=1, *, octets_in=0, octets_out=0, errors_in=0, errors_out=0,
         bits64=True, oper="up", speed=GIG, name=None) -> InterfaceStat:
    return InterfaceStat(
        index=index, name=name or f"port{index}", descr=f"Port {index}",
        admin_status="up", oper_status=oper, speed_mbps=speed,
        in_octets=octets_in, out_octets=octets_out,
        in_errors=errors_in, out_errors=errors_out,
        counters_are_64bit=bits64,
    )


def reading(*, octets_in=0, octets_out=0, errors_in=0, errors_out=0, bits=64,
            at=T0) -> Reading:
    return Reading(bits=bits, in_octets=octets_in, out_octets=octets_out,
                   in_errors=errors_in, out_errors=errors_out, at=at)


# --------------------------------------------------------------------------
# Counter arithmetic
# --------------------------------------------------------------------------


class TestCounterDelta:
    def test_forward(self):
        assert counter_delta(100, 350, 64) == 250

    def test_a_32_bit_counter_that_wrapped(self):
        # 100 below the top, then 50 past zero: 150 octets moved.
        assert counter_delta(WRAP_32 - 100, 50, 32) == 150

    def test_a_64_bit_counter_going_backwards_is_a_reset_not_a_wrap(self):
        # Treated as a wrap this would be ~1.8e19 octets.
        assert counter_delta(5_000_000, 1_000, 64) is None

    def test_a_missing_reading_is_unknown_not_zero(self):
        assert counter_delta(None, 10, 64) is None
        assert counter_delta(10, None, 32) is None


class TestInterfaceRates:
    def test_the_rate_is_bits_per_second_over_the_real_interval(self):
        # 7,500,000 octets in 60 s = 60,000,000 bits / 60 s = 1 Mbps.
        rates = interface_rates(
            reading(octets_in=0, octets_out=1_000),
            stat(octets_in=7_500_000, octets_out=1_000 + 750_000),
            T0 + timedelta(seconds=60), interval=60,
        )
        assert rates.in_bps == pytest.approx(1_000_000)
        assert rates.out_bps == pytest.approx(100_000)

    def test_the_first_reading_is_only_a_baseline(self):
        assert interface_rates(None, stat(octets_in=10), T0, interval=60) is None

    def test_a_32_bit_wrap_gives_the_true_rate(self):
        before = WRAP_32 - 3_750_000
        rates = interface_rates(
            reading(octets_in=before, bits=32),
            stat(octets_in=3_750_000, bits64=False),
            T0 + timedelta(seconds=60), interval=60,
        )
        assert rates.in_bps == pytest.approx(1_000_000)

    def test_a_32_bit_rate_above_the_link_speed_is_discarded(self):
        # 100 Mbps port. A wrap that would imply 500 Mbps is two wraps
        # misread as one, or a reset misread as a wrap -- not traffic.
        rates = interface_rates(
            reading(octets_in=WRAP_32 - 10, bits=32),
            stat(octets_in=3_750_000_000 - 10, bits64=False, speed=100),
            T0 + timedelta(seconds=60), interval=60,
        )
        assert rates is None

    def test_a_64_bit_rate_above_a_nominal_speed_is_kept(self):
        # Loopback reports 10 Mbps and carries whatever it is given.
        rates = interface_rates(
            reading(octets_in=0),
            stat(octets_in=750_000_000, speed=10),
            T0 + timedelta(seconds=60), interval=60,
        )
        assert rates.in_bps == pytest.approx(100_000_000)

    def test_a_change_of_counter_width_is_a_new_baseline(self):
        rates = interface_rates(
            reading(octets_in=9_000_000_000, bits=64),
            stat(octets_in=1_000, bits64=False),
            T0 + timedelta(seconds=60), interval=60,
        )
        assert rates is None

    def test_a_gap_longer_than_three_intervals_is_a_new_baseline(self):
        prev = reading(octets_in=0)
        current = stat(octets_in=1_000_000)
        assert interface_rates(prev, current, T0 + timedelta(seconds=180), interval=60)
        assert interface_rates(prev, current, T0 + timedelta(seconds=181), interval=60) is None

    def test_a_reboot_is_a_new_baseline(self):
        rates = interface_rates(reading(octets_in=0), stat(octets_in=500),
                                T0 + timedelta(seconds=60), interval=60, rebooted=True)
        assert rates is None

    def test_no_time_passing_gives_no_rate(self):
        assert interface_rates(reading(), stat(octets_in=5), T0, interval=60) is None

    def test_errors_are_the_count_in_the_interval(self):
        rates = interface_rates(
            reading(errors_in=40, errors_out=7),
            stat(errors_in=43, errors_out=7),
            T0 + timedelta(seconds=60), interval=60,
        )
        assert (rates.in_errors, rates.out_errors) == (3, 0)


class TestRebootDetection:
    def test_uptime_that_kept_pace_is_not_a_reboot(self):
        assert not detect_reboot(1000.0, 1060.5, 60.0)

    def test_uptime_that_went_backwards_is(self):
        assert detect_reboot(1000.0, 12.0, 60.0)

    def test_a_reboot_hidden_inside_a_long_gap_is_still_caught(self):
        # Up 1h, SPARK away for 3h, device rebooted 2h ago: uptime grew, but
        # by an hour less than the time that passed.
        assert detect_reboot(3600.0, 7200.0, 3 * 3600.0)

    def test_unknown_uptime_is_not_a_reboot(self):
        assert not detect_reboot(None, 50.0, 60.0)
        assert not detect_reboot(50.0, None, 60.0)


def test_interval_is_clamped_and_defaults_on_garbage():
    assert clamp_interval(5) == MIN_INTERVAL
    assert clamp_interval(99_999) == MAX_INTERVAL
    assert clamp_interval("120") == 120
    assert clamp_interval(None) == 60
    assert clamp_interval("soon") == 60


# --------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------


def _config(tmp: Path) -> Config:
    config = Config.model_validate({
        "app": {"data_dir": str(tmp / "data"), "log_level": "WARNING"},
        "network": {"subnets": []},
    })
    config.app.data_dir.mkdir(parents=True, exist_ok=True)
    return config


@pytest.fixture
def db():
    """A database with one device on the SNMP list (row id 1)."""
    tmp = Path(tempfile.mkdtemp(prefix="spark-poll-"))
    config = _config(tmp)

    async def setup():
        D.init_engine(config)
        await D.init_db(config)
        async with D.session_scope() as s:
            s.add(Device(mac="aa:bb:cc:00:00:01", primary_ip="127.0.0.1",
                         friendly_name="lab", last_seen=utcnow()))
            await s.flush()
            profile = await save_profile(
                s, vault_for(config),
                ProfileInput(name="lab", version="v2c", community="sparktest", port="11161"),
            )
            await add_device(s, 1, profile.id)

    asyncio.run(setup())
    yield config
    asyncio.run(D.close_engine())


def healthy(uptime=1000.0, **extra) -> DeviceHealth:
    return DeviceHealth(reachable=True, uptime_seconds=uptime, cpu_percent=12.5,
                        memory_percent=40.0, **extra)


async def _record(health, interfaces, when, interval=60):
    async with D.session_scope() as s:
        await record_poll(s, 1, health, interfaces, when, interval=interval)


async def _all(model):
    async with D.session_scope() as s:
        return list((await s.execute(select(model))).scalars())


class TestRecording:
    def test_the_first_poll_records_health_and_baselines_but_no_traffic(self, db):
        async def go():
            await _record(healthy(), [stat(1, octets_in=100), stat(2, oper="down")], T0)
            return (await _all(SnmpPoll), await _all(SnmpInterface),
                    await _all(SnmpHealthSample), await _all(SnmpInterfaceSample))

        polls, ifaces, health, traffic = asyncio.run(go())
        assert polls[0].cpu_percent == 12.5 and polls[0].last_ok_at == T0
        assert (polls[0].interfaces_total, polls[0].interfaces_up) == (2, 1)
        assert {i.if_index for i in ifaces} == {1, 2}
        assert len(health) == 1 and health[0].reachable
        assert traffic == []

    def test_the_second_poll_stores_rates_for_up_interfaces_only(self, db):
        async def go():
            await _record(healthy(1000), [stat(1, octets_in=0), stat(2, oper="down")], T0)
            await _record(healthy(1060),
                          [stat(1, octets_in=7_500_000), stat(2, oper="down")],
                          T0 + timedelta(seconds=60))
            return await _all(SnmpInterfaceSample), await _all(SnmpInterface)

        traffic, ifaces = asyncio.run(go())
        assert len(traffic) == 1, "the down port must not add a row of zeros"
        assert traffic[0].in_bps == pytest.approx(1_000_000)
        port1 = next(i for i in ifaces if i.if_index == 1)
        assert port1.in_bps == pytest.approx(1_000_000)

    def test_an_unanswered_poll_keeps_the_baseline_and_the_next_answer_bridges_it(self, db):
        async def go():
            await _record(healthy(1000), [stat(1, octets_in=0)], T0)
            await _record(DeviceHealth(reachable=False, error="Unreachable: timeout"), [],
                          T0 + timedelta(seconds=60))
            polls = await _all(SnmpPoll)
            await _record(healthy(1120), [stat(1, octets_in=15_000_000)],
                          T0 + timedelta(seconds=120))
            return polls, await _all(SnmpHealthSample), await _all(SnmpInterfaceSample)

        after_miss, health, traffic = asyncio.run(go())
        assert after_miss[0].last_error == "Unreachable: timeout"
        assert after_miss[0].last_ok_at == T0, "a miss must not move last_ok_at"
        assert [h.reachable for h in health] == [True, False, True]
        # 15,000,000 octets over the 120 s since the last *answer*: 1 Mbps.
        assert len(traffic) == 1
        assert traffic[0].in_bps == pytest.approx(1_000_000)

    def test_after_a_reboot_there_is_no_rate_only_a_new_baseline(self, db):
        async def go():
            await _record(healthy(50_000), [stat(1, octets_in=9_000_000_000)], T0)
            await _record(healthy(45), [stat(1, octets_in=2_000)],
                          T0 + timedelta(seconds=60))
            await _record(healthy(105), [stat(1, octets_in=2_000 + 750_000)],
                          T0 + timedelta(seconds=120))
            return await _all(SnmpInterfaceSample)

        traffic = asyncio.run(go())
        assert len(traffic) == 1, "the reboot poll must not store a rate"
        assert traffic[0].in_bps == pytest.approx(100_000)

    def test_a_status_change_is_timestamped(self, db):
        async def go():
            await _record(healthy(1000), [stat(1)], T0)
            await _record(healthy(1060), [stat(1)], T0 + timedelta(seconds=60))
            first = (await _all(SnmpInterface))[0].status_changed_at
            await _record(healthy(1120), [stat(1, oper="down")], T0 + timedelta(seconds=120))
            return first, (await _all(SnmpInterface))[0]

        before, iface = asyncio.run(go())
        assert before is None
        assert iface.status_changed_at == T0 + timedelta(seconds=120)
        assert iface.oper_status == "down"

    def test_an_interface_that_disappears_is_kept_with_its_history(self, db):
        async def go():
            await _record(healthy(1000), [stat(1), stat(2)], T0)
            await _record(healthy(1060), [stat(1)], T0 + timedelta(seconds=60))
            return await _all(SnmpInterface), await _all(SnmpPoll)

        ifaces, polls = asyncio.run(go())
        gone = next(i for i in ifaces if i.if_index == 2)
        assert gone.last_seen == T0 < polls[0].last_ok_at

    def test_a_counter_above_sqlites_signed_range_round_trips(self, db):
        top = (1 << 64) - 1

        async def go():
            await _record(healthy(), [stat(1, octets_in=top, octets_out=1 << 63)], T0)
            return (await _all(SnmpInterface))[0]

        iface = asyncio.run(go())
        assert iface.in_octets == top
        assert iface.out_octets == 1 << 63

    def test_removing_the_device_takes_its_history(self, db):
        async def go():
            await _record(healthy(1000), [stat(1, octets_in=0)], T0)
            await _record(healthy(1060), [stat(1, octets_in=100)], T0 + timedelta(seconds=60))
            async with D.session_scope() as s:
                await s.delete(await s.get(SnmpDevice, 1))
            counts = []
            async with D.session_scope() as s:
                for table in ("snmp_poll", "snmp_interface", "snmp_health_sample",
                              "snmp_interface_sample"):
                    counts.append(await s.scalar(text(f"SELECT COUNT(*) FROM {table}")))
            return counts

        assert asyncio.run(go()) == [0, 0, 0, 0]


# --------------------------------------------------------------------------
# Scheduling
# --------------------------------------------------------------------------


class TestScheduling:
    def test_jobs_follow_the_list_and_an_unchanged_interval_is_left_alone(self, db):
        async def go():
            scheduler_module.start()
            try:
                assert await scheduler_module.sync_snmp_jobs(db) == 1
                job = scheduler_module.get_scheduler().get_job("snmp:1")
                assert job is not None
                first_due = job.next_run_time
                assert first_due - datetime.now(timezone.utc) < timedelta(seconds=10), \
                    "a new device should be polled within seconds, not a full interval"

                await scheduler_module.sync_snmp_jobs(db)
                assert scheduler_module.get_scheduler().get_job("snmp:1").next_run_time == first_due

                await scheduler_module.sync_snmp_jobs(db, interval=300, row_ids=[1])
                job = scheduler_module.get_scheduler().get_job("snmp:1")
                assert job.trigger.interval == timedelta(seconds=300)

                await scheduler_module.sync_snmp_jobs(db, interval=300, row_ids=[])
                assert scheduler_module.get_scheduler().get_job("snmp:1") is None
            finally:
                await scheduler_module.shutdown()

        asyncio.run(go())

    def test_a_paused_device_is_not_scheduled(self, db):
        async def go():
            async with D.session_scope() as s:
                (await s.get(SnmpDevice, 1)).enabled = False
            scheduler_module.start()
            try:
                return await scheduler_module.sync_snmp_jobs(db)
            finally:
                await scheduler_module.shutdown()

        assert asyncio.run(go()) == 0

    def test_a_paused_device_is_skipped_even_if_a_job_fires(self, db):
        async def go():
            async with D.session_scope() as s:
                (await s.get(SnmpDevice, 1)).enabled = False
            return await poll_device(db, 1), await _all(SnmpHealthSample)

        result, samples = asyncio.run(go())
        assert result is None and samples == []


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------


class TestSnmpRetention:
    def test_old_health_becomes_buckets_that_keep_the_peak(self, db):
        async def go():
            base = utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(days=8)
            async with D.session_scope() as s:
                for minute, cpu in enumerate((10.0, 10.0, 10.0, 10.0, 90.0)):
                    s.add(SnmpHealthSample(snmp_device_id=1,
                                           ts=base + timedelta(minutes=minute),
                                           reachable=True, cpu_percent=cpu,
                                           memory_percent=50.0))
                s.add(SnmpHealthSample(snmp_device_id=1, ts=base + timedelta(seconds=30),
                                       reachable=False))
            summary = await run_retention()
            return summary, await _all(SnmpHealthRollup), await _all(SnmpHealthSample)

        summary, buckets, raw = asyncio.run(go())
        assert "error" not in summary, summary
        assert raw == []
        assert len(buckets) == 1
        b = buckets[0]
        assert (b.samples, b.reachable) == (6, 5)
        # The unanswered poll has no CPU and must not count as 0%.
        assert b.cpu_avg == pytest.approx(26.0)
        assert b.cpu_max == 90.0

    def test_traffic_rolls_up_to_hourly_weighted_by_samples(self, db):
        async def go():
            async with D.session_scope() as s:
                await record_poll(s, 1, healthy(), [stat(1)], T0, interval=60)
            base = utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(days=100)
            async with D.session_scope() as s:
                s.add(SnmpInterfaceRollup(interface_id=1, bucket_start=base,
                                          bucket_seconds=FIVE_MINUTES, samples=5,
                                          in_avg=10.0, in_max=12.0, out_avg=1.0, out_max=1.0,
                                          in_errors=2, out_errors=0))
                s.add(SnmpInterfaceRollup(interface_id=1,
                                          bucket_start=base + timedelta(minutes=5),
                                          bucket_seconds=FIVE_MINUTES, samples=1,
                                          in_avg=70.0, in_max=70.0, out_avg=1.0, out_max=1.0,
                                          in_errors=1, out_errors=0))
            await run_retention()
            async with D.session_scope() as s:
                return (await s.execute(
                    select(SnmpInterfaceRollup)
                    .where(SnmpInterfaceRollup.bucket_seconds == ONE_HOUR)
                )).scalars().first()

        hourly = asyncio.run(go())
        assert hourly is not None
        assert hourly.samples == 6
        # (5*10 + 1*70) / 6 = 20. The plain mean of the two buckets is 40.
        assert hourly.in_avg == pytest.approx(20.0)
        assert hourly.in_max == 70.0
        assert hourly.in_errors == 3


# --------------------------------------------------------------------------
# Against a real agent
# --------------------------------------------------------------------------


def _agent_up() -> bool:
    from spark.collectors import SnmpCollector, SnmpCredential

    async def ask():
        c = SnmpCollector("127.0.0.1", SnmpCredential(community="sparktest", port=11161,
                                                      timeout=1.0, retries=0))
        try:
            return bool(await c.get("1.3.6.1.2.1.1.5.0"))
        except Exception:  # noqa: BLE001
            return False
        finally:
            await c.close()
    return asyncio.run(ask())


needs_agent = pytest.mark.skipif(
    not _agent_up(), reason="no agent on 127.0.0.1:11161 -- run tests/local_agent.sh start"
)


@needs_agent
class TestLivePolling:
    def test_two_polls_of_a_real_agent_give_a_traffic_rate(self, db):
        async def go():
            first = await poll_device(db, 1)
            # Some traffic on loopback between the polls.
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                for _ in range(200):
                    sock.sendto(b"x" * 1000, ("127.0.0.1", 9))
            # net-snmp caches the interface table for a few seconds; two
            # polls inside that window read identical counters and a correct
            # rate of zero. Real polls are a minute apart.
            await asyncio.sleep(6)
            second = await poll_device(db, 1)
            return (first, second, await _all(SnmpPoll), await _all(SnmpInterface),
                    await _all(SnmpInterfaceSample))

        first, second, polls, ifaces, traffic = asyncio.run(go())
        assert first is True and second is True
        poll = polls[0]
        assert poll.last_error is None and poll.uptime_seconds
        assert poll.interfaces_total and poll.interfaces_up
        lo = next(i for i in ifaces if i.name == "lo" or (i.descr or "").startswith("lo"))
        assert lo.counter_bits in (32, 64)
        sample = next(t for t in traffic if t.interface_id == lo.id)
        # 200 datagrams of 1000 bytes is at least 1.6 Mbit, over about six
        # seconds: comfortably above 200 kbps, and far above the idle rate.
        assert sample.in_bps and sample.in_bps > 200_000

    def test_wrong_credentials_are_recorded_as_a_failed_poll(self, db):
        async def go():
            async with D.session_scope() as s:
                await save_profile(s, vault_for(db),
                                   ProfileInput(name="lab", version="v2c",
                                                community="wrong", port="11161"),
                                   profile_id=1)
            return await poll_device(db, 1), await _all(SnmpPoll)

        answered, polls = asyncio.run(go())
        assert answered is False
        assert polls[0].last_ok_at is None
        assert "Unreachable" in polls[0].last_error


# --------------------------------------------------------------------------
# The collector's counters
# --------------------------------------------------------------------------


class TestCollectorCounters:
    """What `collect_interfaces` hands the poller, with the walks faked."""

    def test_a_counter_that_did_not_come_back_is_none_not_zero(self):
        from spark.collectors import oids as O
        [iface] = _collect_with({O.IF_IN_OCTETS: {"1": 500}})
        assert iface.in_octets == 500
        assert iface.out_octets is None, "a missing reading recorded as 0 fakes a spike later"
        assert iface.in_errors is None

    def test_64_bit_only_when_both_directions_answer_it(self):
        from spark.collectors import oids as O
        [iface] = _collect_with({
            O.IF_IN_OCTETS: {"1": 10}, O.IF_OUT_OCTETS: {"1": 20},
            O.IF_HC_IN_OCTETS: {"1": 9_000_000_000},   # no HC out
        })
        assert iface.counters_are_64bit is False
        assert (iface.in_octets, iface.out_octets) == (10, 20)


def _collect_with(tables: dict) -> list[InterfaceStat]:
    from spark.collectors import SnmpCollector
    from spark.collectors import oids as O

    everything = {O.IF_DESCR: {"1": "eth0"}, O.IF_OPER_STATUS: {"1": 1}, **tables}
    collector = SnmpCollector("192.0.2.1")

    async def walk(base, max_rows=4096):  # type: ignore[no-untyped-def]
        return dict(everything.get(base, {}))

    async def walk_raw(base, max_rows=4096):  # type: ignore[no-untyped-def]
        return {}

    collector.walk = walk  # type: ignore[method-assign]
    collector.walk_raw = walk_raw  # type: ignore[method-assign]
    return asyncio.run(collector.collect_interfaces())


# --------------------------------------------------------------------------
# Migration 7
# --------------------------------------------------------------------------

POLL_TABLES = ("snmp_poll", "snmp_interface", "snmp_health_sample",
               "snmp_interface_sample", "snmp_health_rollup", "snmp_interface_rollup")


def _shape(sync) -> dict:  # type: ignore[no-untyped-def]
    from sqlalchemy import inspect

    insp = inspect(sync)
    return {
        table: {
            "columns": sorted((c["name"], str(c["type"]), c["nullable"])
                              for c in insp.get_columns(table)),
            "indexes": sorted((i["name"], tuple(i["column_names"]), bool(i["unique"]))
                              for i in insp.get_indexes(table)),
            "unique": sorted((u["name"], tuple(u["column_names"]))
                             for u in insp.get_unique_constraints(table)),
            "fks": sorted((tuple(f["constrained_columns"]), f["referred_table"],
                           (f.get("options") or {}).get("ondelete"))
                          for f in insp.get_foreign_keys(table)),
        }
        for table in POLL_TABLES
    }


def test_migration_7_builds_what_a_fresh_install_has():
    """From a version-6 database -- stage 1, deployed -- to the polling tables."""
    async def build(wind_back: bool):
        config = _config(Path(tempfile.mkdtemp(prefix="spark-mig7-")))
        D.init_engine(config)
        await D.init_db(config)
        if wind_back:
            async with D.session_scope() as s:
                for table in reversed(POLL_TABLES):
                    await s.execute(text(f"DROP TABLE {table}"))
                await D._set_version(s, 6)
            await D.close_engine()
            D.init_engine(config)
            await D.init_db(config)
        async with D.session_scope() as s:
            connection = await s.connection()
            shape = await connection.run_sync(_shape)
            version = await D._get_version(s)
        await D.close_engine()
        return shape, version

    fresh, fresh_version = asyncio.run(build(False))
    migrated, migrated_version = asyncio.run(build(True))
    assert migrated_version == fresh_version == D.CURRENT_VERSION >= 7
    assert migrated == fresh
    assert fresh["snmp_interface_sample"]["fks"] == [(("interface_id",), "snmp_interface", "CASCADE")]
