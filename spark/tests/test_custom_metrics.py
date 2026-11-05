"""Custom SNMP metrics: OIDs a person chose, read, kept, charted and alerted on.

The failures worth pinning are the quiet ones. A "28416" that is not read as
a number is a chart that never draws. A scale applied when the value is
stored rather than when it is shown makes a corrected scale fix only the
future. One bad OID failing the whole poll takes the device's health with it.
A rule that does not forget its streak when it is edited fires on numbers
that broke the old rule. None of these raise.
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from spark import custom_metrics as C
from spark import db as D
from spark import snmp_history as H
from spark.collectors.base import DeviceHealth
from spark.config import Config
from spark.main import create_app
from spark.models import (
    FIVE_MINUTES,
    ONE_HOUR,
    AlertIncident,
    AlertMute,
    AlertState,
    CustomAlertOp,
    Device,
    Notification,
    SnmpCustomMetric,
    SnmpCustomRollup,
    SnmpCustomSample,
    SnmpDevice,
    utcnow,
)
from spark.retention import run_retention
from spark.snmp_config import ProfileInput, add_device, remove_device, save_profile
from spark.snmp_poll import record_poll
from spark.vault import vault_for

T0 = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
PASSWORD = "correct horse battery"
GPU = C.extend_base("gpu")


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def _config() -> Config:
    tmp = Path(tempfile.mkdtemp(prefix="spark-custom-"))
    config = Config.model_validate({
        "app": {"data_dir": str(tmp / "data"), "log_level": "WARNING"},
        "network": {"subnets": []},
    })
    config.app.data_dir.mkdir(parents=True, exist_ok=True)
    return config


async def _seed(config: Config) -> None:
    D.init_engine(config)
    await D.init_db(config)
    async with D.session_scope() as s:
        s.add(Device(mac="aa:bb:cc:00:00:01", primary_ip="10.0.0.9",
                     friendly_name="gpu-box", last_seen=utcnow()))
        await s.flush()
        profile = await save_profile(s, vault_for(config), ProfileInput(
            name="lab", version="v2c", community="x"))
        await add_device(s, 1, profile.id)


@pytest.fixture
def db():
    """One device, gpu-box at 10.0.0.9, on the SNMP list as row 1."""
    config = _config()
    run(_seed(config))
    yield config
    run(D.close_engine())


def add(name="VRAM used", oid=f"{GPU}.1", **fields) -> int:  # type: ignore[no-untyped-def]
    async def go():
        async with D.session_scope() as s:
            metric = await C.add_metric(s, 1, C.MetricInput(name=name, oid=oid, **fields))
            return metric.id
    return run(go())


def poll(minute: float, readings: dict[int, C.Reading], *, reachable=True) -> None:
    health = DeviceHealth(reachable=reachable, uptime_seconds=100_000 + minute * 60,
                          error=None if reachable else "Timeout")

    async def go():
        async with D.session_scope() as s:
            await record_poll(s, 1, health, [], T0 + timedelta(minutes=minute),
                              interval=60, custom=readings)
    run(go())


def metric(mid: int) -> SnmpCustomMetric:
    async def go():
        async with D.session_scope() as s:
            row = await s.get(SnmpCustomMetric, mid)
            s.expunge(row)
            return row
    return run(go())


def sent() -> list[Notification]:
    async def go():
        async with D.session_scope() as s:
            return list((await s.execute(select(Notification).order_by(Notification.id))).scalars())
    return run(go())


def kinds() -> list[str]:
    return [n.kind for n in sent()]


def count(model) -> int:  # type: ignore[no-untyped-def]
    async def go():
        async with D.session_scope() as s:
            return await s.scalar(select(func.count()).select_from(model))
    return run(go())


# --------------------------------------------------------------------------
# Pure functions
# --------------------------------------------------------------------------


class TestParsing:
    @pytest.mark.parametrize("value,expected", [
        (28416, 28416.0), (10.5, 10.5), ("28416", 28416.0), ("10.50", 10.5),
        ("  31 C", 31.0), ("-4.5", -4.5), (".5", 0.5), ("1e3", 1000.0), ("+7", 7.0),
    ])
    def test_numbers_and_text_that_starts_with_one(self, value, expected):
        assert C.parse_number(value) == (expected, None)

    @pytest.mark.parametrize("value", ["n/a", "", "abc 12", True, None, b"\x01", "nan", "inf"])
    def test_anything_else_says_why(self, value):
        number, why = C.parse_number(value)
        assert number is None and why

    def test_a_float_overflow_is_not_a_reading(self):
        assert C.parse_number(float("inf"))[0] is None
        assert C.parse_number("1e999")[0] is None

    def test_oids_are_numeric_with_no_leading_dot(self):
        assert C.normalize_oid(" .1.3.6.1.2.1.1.3.0 ") == "1.3.6.1.2.1.1.3.0"
        for bad in ("", "UCD-SNMP-MIB::laLoad.1", "1", "1.3..6", "1.3.6.", "3.6.1", "1.3.a"):
            with pytest.raises(C.MetricError):
                C.normalize_oid(bad)

    def test_an_extend_name_becomes_its_length_prefixed_index(self):
        assert C.extend_base("gpu") == "1.3.6.1.4.1.8072.1.3.2.4.1.2.3.103.112.117"
        for bad in ("", "has space", "a" * 65, "gpu;rm"):
            with pytest.raises(C.MetricError):
                C.extend_base(bad)

    @pytest.mark.parametrize("value,unit,shown", [
        (28416.0, "MiB", "28,416 MiB"), (27.75, "GiB", "27.8 GiB"), (7.0, "%", "7%"),
        (31.0, "°C", "31°C"), (10.5, "W", "10.5 W"), (0.0123, None, "0.0123"),
        (1234.5, None, "1,234"), (None, "W", "—"),
    ])
    def test_values_read_like_a_person_would_write_them(self, value, unit, shown):
        assert C.fmt_value(value, unit) == shown


class TestValidation:
    def test_a_full_form(self):
        clean = C.validate(C.MetricInput(name="  VRAM   used ", oid=".1.3.6.1.4.1.1.1",
                                         unit="MiB", scale="0.5", alert_op="above",
                                         alert_value="45,000", alert_minutes="10"))
        assert clean == C.Clean("VRAM used", "1.3.6.1.4.1.1.1", "MiB", 0.5,
                                CustomAlertOp.ABOVE, 45000.0, 10)

    @pytest.mark.parametrize("fields,message", [
        ({"name": ""}, "name"),
        ({"scale": "0"}, "Scale"),
        ({"scale": "-1"}, "Scale"),
        ({"scale": "abc"}, "Scale"),
        ({"alert_op": "above", "alert_value": ""}, "value to alert at"),
        ({"alert_op": "sideways"}, "above, below"),
        ({"alert_op": "below", "alert_value": "1", "alert_minutes": "0"}, "Minutes"),
        ({"unit": "<b>"}, "unit"),
    ])
    def test_what_cannot_be_saved_says_so(self, fields, message):
        base = {"name": "x", "oid": "1.3.6.1.4.1.1.1", **fields}
        with pytest.raises(C.MetricError, match=message):
            C.validate(C.MetricInput(**base))

    def test_no_alert_ignores_a_value_left_in_the_box(self):
        clean = C.validate(C.MetricInput(name="x", oid="1.3.6.1.4.1.1", alert_value="9"))
        assert clean.alert_op is None and clean.alert_value is None


# --------------------------------------------------------------------------
# Reading from the device
# --------------------------------------------------------------------------


class FakeCollector:
    """Answers GETs from a dict, as pysnmp keys them (no leading dot), and
    fails any request that includes an OID in `fail`."""

    def __init__(self, values: dict, fail: frozenset = frozenset()) -> None:
        self.values = values
        self.fail = fail
        self.requests: list[tuple[str, ...]] = []

    async def get(self, *oids: str) -> dict:
        self.requests.append(oids)
        if self.fail & set(oids):
            raise RuntimeError("genErr")
        return {o: self.values[o] for o in oids if o in self.values}


class TestRead:
    def test_numbers_text_and_missing_values(self):
        collector = FakeCollector({f"{GPU}.1": "28416", f"{GPU}.6": "n/a", "1.3.6.1.2.1.1.3.0": 5})
        got = run(C.read(collector, [(1, f"{GPU}.1"), (2, f"{GPU}.6"), (3, f"{GPU}.9"),
                                     (4, "1.3.6.1.2.1.1.3.0")]))
        assert got[1] == C.Reading(28416.0)
        assert got[2].value is None and "n/a" in got[2].error
        assert got[3] == C.Reading(None, "The device has no value at this OID.")
        assert got[4] == C.Reading(5.0)

    def test_a_failed_request_fails_only_its_own_group(self):
        oids = [(i, f"1.3.6.1.4.1.9.{i}") for i in range(1, 16)]
        values = {oid: i for i, oid in oids}
        collector = FakeCollector(values, fail=frozenset({"1.3.6.1.4.1.9.12"}))
        got = run(C.read(collector, oids))
        assert [len(r) for r in collector.requests] == [C.GET_GROUP, 15 - C.GET_GROUP]
        assert all(got[i] == C.Reading(float(i)) for i in range(1, 11))
        assert all(got[i].value is None and "genErr" in got[i].error for i in range(11, 16))

    def test_it_never_raises(self):
        class Broken:
            async def get(self, *oids):  # type: ignore[no-untyped-def]
                raise OSError()
        got = run(C.read(Broken(), [(1, "1.3.6.1.4.1.9.1")]))
        assert got[1].value is None and "OSError" in got[1].error


# --------------------------------------------------------------------------
# Saving
# --------------------------------------------------------------------------


class TestSaving:
    def test_the_same_oid_twice_on_one_device_is_refused(self, db):
        add()
        with pytest.raises(C.MetricError, match="already has"):
            add(name="again")

    def test_there_is_a_limit_per_device(self, db):
        for i in range(C.MAX_PER_DEVICE):
            add(name=f"m{i}", oid=f"1.3.6.1.4.1.9.{i}")
        with pytest.raises(C.MetricError, match="at most"):
            add(name="one more", oid="1.3.6.1.4.1.9.999")

    def test_an_import_adds_all_or_nothing(self, db):
        add(name="taken", oid=f"{GPU}.2")

        async def go(forms):  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                await C.import_lines(s, 1, forms)
        with pytest.raises(C.MetricError, match="already has"):
            run(go([C.MetricInput(name="a", oid=f"{GPU}.1"),
                    C.MetricInput(name="b", oid=f"{GPU}.2")]))
        assert count(SnmpCustomMetric) == 1
        run(go([C.MetricInput(name="a", oid=f"{GPU}.1"), C.MetricInput(name="c", oid=f"{GPU}.3")]))
        assert count(SnmpCustomMetric) == 3


# --------------------------------------------------------------------------
# Recording, in the poll
# --------------------------------------------------------------------------


class TestRecording:
    def test_an_answered_poll_keeps_the_raw_value_and_the_page_scales_it(self, db):
        mid = add(unit="GiB", scale=str(1 / 1024))
        poll(0, {mid: C.Reading(28416.0)})
        row = metric(mid)
        assert row.last_value == 28416.0 and row.current == pytest.approx(27.75)
        assert row.last_read_at == T0 and row.last_error is None
        assert count(SnmpCustomSample) == 1

    def test_a_value_that_is_not_a_number_is_an_error_and_a_gap(self, db):
        mid = add()
        poll(0, {mid: C.Reading(5.0)})
        poll(1, {mid: C.Reading(None, "Not a number: 'n/a'.")})
        row = metric(mid)
        assert row.last_value is None and row.last_error == "Not a number: 'n/a'."
        assert count(SnmpCustomSample) == 1

    def test_an_unanswered_poll_leaves_the_last_reading_alone(self, db):
        mid = add()
        poll(0, {mid: C.Reading(5.0)})
        poll(1, {mid: C.Reading(9.0)}, reachable=False)
        row = metric(mid)
        assert row.last_value == 5.0 and row.last_read_at == T0
        assert count(SnmpCustomSample) == 1

    def test_a_metric_removed_mid_poll_is_skipped(self, db):
        poll(0, {12345: C.Reading(5.0)})
        assert count(SnmpCustomSample) == 0

    def test_removing_the_device_from_snmp_takes_its_metrics_and_their_state(self, db):
        mid = add(alert_op="above", alert_value="1", alert_minutes="1")
        for minute in range(3):
            poll(minute, {mid: C.Reading(5.0)})
        assert count(AlertState) == 1

        async def go():
            async with D.session_scope() as s:
                await remove_device(s, 1)
        run(go())
        assert count(SnmpCustomMetric) == count(SnmpCustomSample) == count(AlertState) == 0


# --------------------------------------------------------------------------
# The threshold rule
# --------------------------------------------------------------------------


class TestAlerts:
    def test_above_the_line_for_the_whole_time_alerts_once_then_recovers(self, db):
        mid = add(unit="MiB", alert_op="above", alert_value="45000", alert_minutes="5")
        for minute in range(5):
            poll(minute, {mid: C.Reading(46000.0)})
        assert kinds() == [], "four minutes is not five"
        for minute in range(5, 9):
            poll(minute, {mid: C.Reading(47000.0)})
        assert kinds() == ["custom_metric"]
        first = sent()[0]
        assert first.subject == "gpu-box: VRAM used is above 45,000 MiB (47,000 MiB)"
        assert first.tone == "bad" and "`10.0.0.9`" in first.body and "for 5 minutes" in first.body

        poll(9, {mid: C.Reading(44000.0)})           # under, not by the 5% margin
        assert kinds() == ["custom_metric"]
        poll(10, {mid: C.Reading(42000.0)})
        assert kinds() == ["custom_metric", "custom_metric_ok"]
        assert sent()[1].subject == "gpu-box: VRAM used back to normal (42,000 MiB)"

    def test_the_rule_compares_scaled_values(self, db):
        mid = add(unit="GiB", scale=str(1 / 1024), alert_op="above", alert_value="40",
                  alert_minutes="1")
        for minute in range(3):
            poll(minute, {mid: C.Reading(28416.0)})   # 27.75 GiB
        assert kinds() == []
        for minute in range(3, 6):
            poll(minute, {mid: C.Reading(45000.0)})   # 43.9 GiB
        assert kinds() == ["custom_metric"]

    def test_below_the_line(self, db):
        mid = add(name="UPS runtime", unit="min", alert_op="below", alert_value="10",
                  alert_minutes="2")
        for minute in range(3):
            poll(minute, {mid: C.Reading(8.0)})
        assert kinds() == ["custom_metric"]
        assert sent()[0].subject == "gpu-box: UPS runtime is below 10 min (8 min)"
        poll(3, {mid: C.Reading(10.2)})               # over, not by the margin
        poll(4, {mid: C.Reading(11.0)})
        assert kinds() == ["custom_metric", "custom_metric_ok"]

    def test_no_reading_is_neither_a_breach_nor_an_all_clear(self, db):
        mid = add(alert_op="above", alert_value="1", alert_minutes="1")
        for minute in range(3):
            poll(minute, {mid: C.Reading(5.0)})
        poll(3, {mid: C.Reading(None, "Not a number.")})
        assert kinds() == ["custom_metric"]
        poll(4, {mid: C.Reading(0.0)})
        assert kinds() == ["custom_metric", "custom_metric_ok"]

    def test_a_muted_device_records_the_incident_and_sends_nothing(self, db):
        mid = add(alert_op="above", alert_value="1", alert_minutes="1")

        async def mute():
            async with D.session_scope() as s:
                s.add(AlertMute(device_id=1))
        run(mute())
        for minute in range(3):
            poll(minute, {mid: C.Reading(5.0)})
        assert kinds() == []
        assert count(AlertIncident) == 1

    def test_editing_the_rule_forgets_the_old_one(self, db):
        mid = add(alert_op="above", alert_value="1", alert_minutes="1")
        for minute in range(3):
            poll(minute, {mid: C.Reading(5.0)})
        assert kinds() == ["custom_metric"] and count(AlertState) == 1

        async def edit():
            async with D.session_scope() as s:
                row = await s.get(SnmpCustomMetric, mid)
                await C.update_metric(s, row, C.MetricInput(
                    name="VRAM used", oid=row.oid, alert_op="above", alert_value="10",
                    alert_minutes="1"))
        run(edit())
        assert count(AlertState) == 0

        async def incident():
            async with D.session_scope() as s:
                return await s.scalar(select(AlertIncident))
        assert run(incident()).resolution == "no longer watched"

    def test_renaming_alone_keeps_a_standing_alert(self, db):
        mid = add(alert_op="above", alert_value="1", alert_minutes="1")
        for minute in range(3):
            poll(minute, {mid: C.Reading(5.0)})

        async def rename():
            async with D.session_scope() as s:
                row = await s.get(SnmpCustomMetric, mid)
                await C.update_metric(s, row, C.MetricInput(
                    name="GPU memory", oid=row.oid, alert_op="above", alert_value="1",
                    alert_minutes="1"))
        run(rename())
        assert count(AlertState) == 1

    def test_removing_a_metric_closes_its_incident(self, db):
        mid = add(alert_op="above", alert_value="1", alert_minutes="1")
        for minute in range(3):
            poll(minute, {mid: C.Reading(5.0)})

        async def remove():
            async with D.session_scope() as s:
                await C.delete_metric(s, await s.get(SnmpCustomMetric, mid))
        run(remove())
        assert count(AlertState) == count(SnmpCustomSample) == 0

        async def open_incidents():
            async with D.session_scope() as s:
                return await s.scalar(select(func.count()).select_from(AlertIncident).where(
                    AlertIncident.closed_at.is_(None)))
        assert run(open_incidents()) == 0

    def test_incidents_are_listed_as_snmp(self):
        from spark.web.incident_rows import source_of
        assert source_of("custom:3") == "SNMP"


# --------------------------------------------------------------------------
# History: retention and reading back
# --------------------------------------------------------------------------


class TestHistory:
    def test_retention_folds_average_lowest_and_highest(self, db):
        mid = add()
        old = utcnow() - timedelta(days=30)
        start = old.replace(minute=0, second=0, microsecond=0)

        async def seed():
            async with D.session_scope() as s:
                for k, value in enumerate((10.0, 20.0, 60.0, 30.0, 30.0)):
                    s.add(SnmpCustomSample(metric_id=mid, ts=start + timedelta(minutes=k),
                                           value=value))
        run(seed())
        run(run_retention())

        async def rollups():
            async with D.session_scope() as s:
                return list((await s.execute(select(SnmpCustomRollup)
                                             .order_by(SnmpCustomRollup.bucket_seconds))).scalars())
        rows = run(rollups())
        assert count(SnmpCustomSample) == 0
        five = [r for r in rows if r.bucket_seconds == FIVE_MINUTES]
        assert len(five) == 1
        assert (five[0].samples, five[0].value_avg, five[0].value_min, five[0].value_max) == \
            (5, 30.0, 10.0, 60.0)

    def test_the_wider_fold_weights_by_samples(self, db):
        mid = add()
        start = (utcnow() - timedelta(days=200)).replace(minute=0, second=0, microsecond=0)

        async def seed():
            async with D.session_scope() as s:
                s.add(SnmpCustomRollup(metric_id=mid, bucket_start=start,
                                       bucket_seconds=FIVE_MINUTES, samples=4,
                                       value_avg=10.0, value_min=5.0, value_max=15.0))
                s.add(SnmpCustomRollup(metric_id=mid, bucket_start=start + timedelta(minutes=5),
                                       bucket_seconds=FIVE_MINUTES, samples=1,
                                       value_avg=60.0, value_min=60.0, value_max=60.0))
        run(seed())
        run(run_retention())

        async def hourly():
            async with D.session_scope() as s:
                return await s.scalar(select(SnmpCustomRollup).where(
                    SnmpCustomRollup.bucket_seconds == ONE_HOUR))
        row = run(hourly())
        assert (row.samples, row.value_avg, row.value_min, row.value_max) == (5, 20.0, 5.0, 60.0)

    def test_reading_back_combines_raw_and_rollups_without_averaging_averages(self, db):
        mid = add()
        now = utcnow()
        window = H.Window.ending(now, "7d")
        bucket = window.time_of(window.count - 2)

        async def seed():
            async with D.session_scope() as s:
                s.add(SnmpCustomRollup(metric_id=mid, bucket_start=bucket,
                                       bucket_seconds=FIVE_MINUTES, samples=4,
                                       value_avg=10.0, value_min=2.0, value_max=12.0))
                s.add(SnmpCustomSample(metric_id=mid, ts=bucket + timedelta(minutes=10),
                                       value=60.0))
            async with D.session_scope() as s:
                return await H.custom_history(s, [mid], window)
        got = run(seed())[mid]
        i = window.count - 2
        assert got.series.avg[i] == pytest.approx(20.0), "(4 x 10 + 60) / 5, not (10 + 60) / 2"
        assert got.series.peak[i] == 60.0 and got.low[i] == 2.0
        assert got.overall_low() == 2.0

    def test_no_metrics_no_queries(self, db):
        async def go():
            async with D.session_scope() as s:
                return await H.custom_history(s, [], H.Window.ending(utcnow(), "1h"))
        assert run(go()) == {}


def test_migration_31_from_a_version_30_database():
    from sqlalchemy import inspect

    async def shape(wind_back: bool):  # type: ignore[no-untyped-def]
        config = _config()
        D.init_engine(config)
        await D.init_db(config)
        if wind_back:
            async with D.session_scope() as s:
                for table in ("snmp_custom_rollup", "snmp_custom_sample", "snmp_custom_metric"):
                    await s.execute(text(f"DROP TABLE {table}"))
                await D._set_version(s, 30)
            await D.close_engine()
            D.init_engine(config)
            await D.init_db(config)
        async with D.session_scope() as s:
            connection = await s.connection()
            tables = {}
            for table in ("snmp_custom_metric", "snmp_custom_sample", "snmp_custom_rollup"):
                tables[table] = await connection.run_sync(
                    lambda sync, t=table: (sorted(c["name"] for c in inspect(sync).get_columns(t)),
                                           sorted(i["name"] for i in inspect(sync).get_indexes(t))))
            version = await D._get_version(s)
        await D.close_engine()
        return tables, version

    migrated, fresh = run(shape(True)), run(shape(False))
    assert migrated == fresh and fresh[1] == D.CURRENT_VERSION >= 31


# --------------------------------------------------------------------------
# The device page
# --------------------------------------------------------------------------


@pytest.fixture
def site():
    config = _config()

    async def seed():
        await _seed(config)
        async with D.session_scope() as s:
            s.add(Device(mac="aa:bb:cc:00:00:02", primary_ip="10.0.0.10",
                         friendly_name="not-polled", last_seen=utcnow()))
            m = SnmpCustomMetric(snmp_device_id=1, name="VRAM used", oid=f"{GPU}.1",
                                 unit="GiB", scale=1 / 1024, last_value=28416.0,
                                 last_read_at=utcnow(),
                                 alert_op=CustomAlertOp.ABOVE, alert_value=40.0, alert_minutes=5)
            s.add(m)
            s.add(SnmpCustomMetric(snmp_device_id=1, name="Status", oid=f"{GPU}.6",
                                   last_read_at=utcnow(), last_error="Not a number: 'n/a'."))
            await s.flush()
            now = utcnow()
            for k in range(30):
                s.add(SnmpCustomSample(metric_id=m.id, ts=now - timedelta(minutes=k),
                                       value=20000.0 + k * 100))
        await D.close_engine()

    run(seed())
    with TestClient(create_app(config), follow_redirects=False) as client:
        client.post("/setup", data={"setup_code": client.app.state.setup_code,
                                    "username": "admin", "password": PASSWORD,
                                    "password_confirm": PASSWORD})
        yield client


def _metrics(client) -> list[tuple[str, str, str | None]]:  # type: ignore[no-untyped-def]
    async def go():
        async with D.session_scope() as s:
            rows = (await s.execute(select(SnmpCustomMetric).order_by(SnmpCustomMetric.id))).scalars()
            return [(m.name, m.oid, m.alert_op) for m in rows]
    return run(go())


class TestDevicePage:
    def test_a_metric_is_charted_scaled_with_its_rule_and_an_error_is_shown(self, site):
        page = site.get("/devices/1").text
        assert "<h2>Custom metrics</h2>" in page
        assert "27.8 GiB" in page, "28416 MiB shown through its scale"
        assert "Alerts when above 40 GiB for 5 minutes" in page
        assert "Not a number: &#39;n/a&#39;." in page
        section = page[page.index('id="custom-metrics"'):page.index('id="interfaces"')]
        assert section.count("<figure") == 1, "the metric with history, not the one without"

    def test_a_device_not_polled_has_no_custom_metrics_card(self, site):
        page = site.get("/devices/2").text
        assert "Custom metrics" not in page
        assert site.post("/devices/2/custom", data={"name": "x", "oid": "1.3.6.1.4.1.1"}
                         ).status_code == 303
        assert len(_metrics(site)) == 2

    def test_add_edit_and_remove(self, site):
        r = site.post("/devices/1/custom", data={"name": "GPU temp", "oid": f".{GPU}.4",
                                                 "unit": "°C", "scale": "1"})
        assert r.status_code == 303 and r.headers["location"].endswith("#custom-metrics")
        names = _metrics(site)
        assert names[-1] == ("GPU temp", f"{GPU}.4", None)

        async def last_id():
            async with D.session_scope() as s:
                return await s.scalar(select(func.max(SnmpCustomMetric.id)))
        mid = run(last_id())
        r = site.post(f"/devices/1/custom/{mid}", data={
            "name": "GPU temp", "oid": f"{GPU}.4", "unit": "°C", "scale": "1",
            "alert_op": "above", "alert_value": "85", "alert_minutes": "3"})
        assert r.status_code == 303 and _metrics(site)[-1][2] == "above"
        assert site.post(f"/devices/1/custom/{mid}/delete").status_code == 303
        assert len(_metrics(site)) == 2

    def test_a_mistake_comes_back_with_what_was_typed(self, site):
        r = site.post("/devices/1/custom", data={"name": "GPU temp", "oid": "GPU-MIB::temp",
                                                 "unit": "°C"})
        assert r.status_code == 400
        assert "The OID must be numeric" in r.text
        assert 'value="GPU-MIB::temp"' in r.text and "<details class=\"add-profile\" open>" in r.text
        assert len(_metrics(site)) == 2

    def test_another_devices_metric_cannot_be_reached_through_this_one(self, site):
        assert site.post("/devices/2/custom/1/delete").status_code == 303
        assert len(_metrics(site)) == 2

    def test_reading_an_extend_lists_its_lines_to_name(self, site, monkeypatch):
        async def fake(session, vault, row_id, name, **_):  # type: ignore[no-untyped-def]
            return [C.ExtendLine(1, f"{GPU}.1", "28416", 28416.0),
                    C.ExtendLine(2, f"{GPU}.2", "49140", 49140.0),
                    C.ExtendLine(6, f"{GPU}.6", "n/a", None)]
        monkeypatch.setattr(C, "read_extend", fake)
        page = site.post("/devices/1/custom-extend", data={"extend": "gpu"}).text
        start = page.index('class="extend-import"')
        form = page[start:page.index("</form>", start)]
        assert "Already added." in form, "line 1 is VRAM used already"
        assert form.count('name="oid"') == 1 and f'value="{GPU}.2"' in form
        assert "not a number" not in form.split(f'value="{GPU}.2"')[0]

    def test_an_extend_that_does_not_answer_says_why(self, site, monkeypatch):
        async def fake(*_a, **_k):  # type: ignore[no-untyped-def]
            raise C.MetricError("No output from an extend named 'gpu'.")
        monkeypatch.setattr(C, "read_extend", fake)
        r = site.post("/devices/1/custom-extend", data={"extend": "gpu"})
        assert r.status_code == 400 and "No output from an extend" in r.text

    def test_import_adds_the_named_lines_and_skips_the_rest(self, site):
        r = site.post("/devices/1/custom-import", data={
            "extend": "gpu",
            "oid": [f"{GPU}.2", f"{GPU}.3", f"{GPU}.5"],
            "name": ["VRAM total", "", "Power"],
            "unit": ["MiB", "", "W"],
            "scale": ["1", "1", "1"],
        })
        assert r.status_code == 303
        assert [m[0] for m in _metrics(site)] == ["VRAM used", "Status", "VRAM total", "Power"]

    def test_import_with_nothing_named_says_so(self, site):
        r = site.post("/devices/1/custom-import", data={
            "extend": "gpu", "oid": [f"{GPU}.2"], "name": [""], "unit": [""], "scale": ["1"]})
        assert r.status_code == 400 and "Name at least one line" in r.text


# --------------------------------------------------------------------------
# Against a real agent (tests/local_agent.sh runs an extend called sparkgpu)
# --------------------------------------------------------------------------


def _extend_running() -> bool:
    from test_snmp import AGENT_COMMUNITY, AGENT_HOST, AGENT_PORT

    from spark.collectors import SnmpCollector, SnmpCredential

    async def ask() -> bool:
        collector = SnmpCollector(AGENT_HOST, SnmpCredential(
            community=AGENT_COMMUNITY, port=AGENT_PORT, timeout=1.0, retries=0))
        try:
            return bool(await collector.walk(C.extend_base("sparkgpu")))
        except Exception:  # noqa: BLE001
            return False
        finally:
            await collector.close()
    try:
        return asyncio.run(ask())
    except Exception:  # noqa: BLE001
        return False


needs_extend = pytest.mark.skipif(
    not _extend_running(), reason="no local agent with the sparkgpu extend; tests/local_agent.sh")


@needs_extend
def test_an_extend_read_imported_and_polled_end_to_end():
    from test_snmp import AGENT_COMMUNITY, AGENT_HOST, AGENT_PORT

    from spark.snmp_poll import poll_device

    config = _config()

    async def go():
        D.init_engine(config)
        await D.init_db(config)
        async with D.session_scope() as s:
            s.add(Device(mac="aa:bb:cc:00:00:01", primary_ip=AGENT_HOST,
                         friendly_name="gpu-box", last_seen=utcnow()))
            await s.flush()
            profile = await save_profile(s, vault_for(config), ProfileInput(
                name="lab", version="v2c", community=AGENT_COMMUNITY, port=str(AGENT_PORT)))
            row_id = (await add_device(s, 1, profile.id)).id
        async with D.session_scope() as s:
            lines = await C.read_extend(s, vault_for(config), row_id, "sparkgpu")
        assert [(l.line, l.value) for l in lines] == [
            (1, 28416.0), (2, 49140.0), (3, 7.0), (4, 31.0), (5, 10.5), (6, None)]
        async with D.session_scope() as s:
            await C.import_lines(s, row_id, [
                C.MetricInput(name="VRAM used", oid=lines[0].oid, unit="GiB", scale=str(1 / 1024)),
                C.MetricInput(name="Power", oid=lines[4].oid, unit="W"),
                C.MetricInput(name="Status", oid=lines[5].oid),
            ])
        assert await poll_device(config, row_id) is True
        async with D.session_scope() as s:
            got = {m.name: (m.current, m.last_error) for m in await C.metrics_for(s, row_id)}
            device = await s.get(SnmpDevice, row_id)
            assert device is not None
        await D.close_engine()
        return got

    got = run(go())
    assert got["VRAM used"] == (pytest.approx(27.75), None)
    assert got["Power"] == (10.5, None)
    assert got["Status"][0] is None and "n/a" in got["Status"][1]


@needs_extend
def test_an_extend_that_does_not_exist_says_so():
    from test_snmp import AGENT_COMMUNITY, AGENT_HOST, AGENT_PORT

    config = _config()

    async def go():
        D.init_engine(config)
        await D.init_db(config)
        async with D.session_scope() as s:
            s.add(Device(mac="aa:bb:cc:00:00:01", primary_ip=AGENT_HOST,
                         friendly_name="gpu-box", last_seen=utcnow()))
            await s.flush()
            profile = await save_profile(s, vault_for(config), ProfileInput(
                name="lab", version="v2c", community=AGENT_COMMUNITY, port=str(AGENT_PORT)))
            row_id = (await add_device(s, 1, profile.id)).id
        try:
            async with D.session_scope() as s:
                with pytest.raises(C.MetricError, match="No output from an extend named 'nothere'"):
                    await C.read_extend(s, vault_for(config), row_id, "nothere")
        finally:
            await D.close_engine()
    run(go())
