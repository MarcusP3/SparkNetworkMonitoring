"""The device page: history read back correctly, and drawn without lying.

Two kinds of mistake matter here. Reading history back wrong -- averaging
averages, bridging a gap, counting a bucket twice where raw samples and a
rollup meet -- produces a chart that looks fine and is not. And drawing it
wrong -- a line joined across an outage, a scale that clips the peak, a red
pill on every empty switch port -- teaches the reader something untrue. Each
test here pins one of those.
"""

from __future__ import annotations

import asyncio
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from spark import charts
from spark import db as D
from spark import scheduler as scheduler_module
from spark import snmp_history as H
from spark.config import Config
from spark.main import create_app
from spark.models import (
    FIVE_MINUTES,
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
from spark.snmp_config import ProfileInput, save_profile
from spark.vault import vault_for

PASSWORD = "correct horse battery"
NOW = datetime(2026, 9, 24, 12, 7, 30, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Windows
# --------------------------------------------------------------------------


class TestWindow:
    @pytest.mark.parametrize("name,span,width", [
        ("1h", 3600, 60), ("24h", 86400, 300), ("7d", 604800, 1800), ("30d", 2592000, 7200),
    ])
    def test_buckets_are_aligned_and_the_last_one_holds_now(self, name, span, width):
        w = H.Window.ending(NOW, name)
        assert w.width == width and w.count * width == span
        assert w.start_epoch % width == 0
        last_start = w.time_of(w.count - 1)
        assert last_start <= NOW < last_start + timedelta(seconds=width)

    def test_an_unknown_range_falls_back_to_a_day(self):
        assert H.parse_range("forever") == "24h"
        assert H.parse_range(None) == "24h"

    def test_the_start_is_compared_in_the_stored_text_format(self):
        w = H.Window.ending(NOW, "1h")
        assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{6}", w.since)


# --------------------------------------------------------------------------
# Reading history back
# --------------------------------------------------------------------------


def _config() -> Config:
    tmp = Path(tempfile.mkdtemp(prefix="spark-devpage-"))
    config = Config.model_validate({
        "app": {"data_dir": str(tmp / "data"), "log_level": "WARNING"},
        "network": {"subnets": []},
    })
    config.app.data_dir.mkdir(parents=True, exist_ok=True)
    return config


async def _base(config: Config, *, devices: int = 1) -> None:
    D.init_engine(config)
    await D.init_db(config)
    async with D.session_scope() as s:
        for n in range(1, devices + 2):
            s.add(Device(mac=f"aa:bb:cc:00:00:0{n}", primary_ip=f"10.0.0.{n}",
                         friendly_name=f"dev{n}", last_seen=utcnow()))
        await s.flush()
        profile = await save_profile(s, vault_for(config),
                                     ProfileInput(name="lab", version="v2c", community="x"))
        for n in range(1, devices + 1):
            s.add(SnmpDevice(device_id=n, profile_id=profile.id, enabled=True))
        await s.flush()
        for n in range(1, devices + 1):
            s.add(SnmpInterface(snmp_device_id=n, if_index=1, name=f"eth0-{n}",
                                oper_status="up", admin_status="up", speed_mbps=1000,
                                last_seen=utcnow()))


@pytest.fixture
def db():
    config = _config()
    asyncio.run(_base(config, devices=2))
    yield config
    asyncio.run(D.close_engine())


def _run(coro):
    return asyncio.run(coro)


async def _health(window):
    async with D.session_scope() as s:
        return await H.health_history(s, 1, window)


async def _traffic(window, ids=(1,)):
    async with D.session_scope() as s:
        return await H.traffic_history(s, list(ids), window)


class TestHealthHistory:
    def test_raw_samples_in_a_bucket_are_averaged_and_peaked(self, db):
        w = H.Window.ending(NOW, "24h")
        b = w.time_of(10)

        async def go():
            async with D.session_scope() as s:
                for minute, cpu in enumerate((10.0, 20.0, 60.0)):
                    s.add(SnmpHealthSample(snmp_device_id=1, ts=b + timedelta(minutes=minute),
                                           reachable=True, cpu_percent=cpu))
            return await _health(w)

        h = _run(go())
        assert h.cpu.avg[10] == pytest.approx(30.0)
        assert h.cpu.peak[10] == 60.0
        assert h.cpu.avg[9] is None and h.cpu.avg[11] is None

    def test_rollups_combine_weighted_not_as_an_average_of_averages(self, db):
        w = H.Window.ending(NOW, "30d")
        b = w.time_of(5)

        async def go():
            async with D.session_scope() as s:
                s.add(SnmpHealthRollup(snmp_device_id=1, bucket_start=b,
                                       bucket_seconds=FIVE_MINUTES, samples=5, reachable=5,
                                       cpu_avg=10.0, cpu_max=12.0))
                s.add(SnmpHealthRollup(snmp_device_id=1, bucket_start=b + timedelta(minutes=5),
                                       bucket_seconds=FIVE_MINUTES, samples=1, reachable=1,
                                       cpu_avg=70.0, cpu_max=70.0))
            return await _health(w)

        h = _run(go())
        # (5*10 + 1*70) / 6 = 20. Averaging the two buckets would say 40.
        assert h.cpu.avg[5] == pytest.approx(20.0)
        assert h.cpu.peak[5] == 70.0

    def test_raw_and_rolled_up_history_meet_in_one_bucket(self, db):
        """The 7-day boundary lands inside a 30-day chart's 2-hour bucket."""
        w = H.Window.ending(NOW, "30d")
        b = w.time_of(40)

        async def go():
            async with D.session_scope() as s:
                s.add(SnmpHealthRollup(snmp_device_id=1, bucket_start=b,
                                       bucket_seconds=FIVE_MINUTES, samples=5, reachable=5,
                                       cpu_avg=10.0, cpu_max=10.0))
                s.add(SnmpHealthSample(snmp_device_id=1, ts=b + timedelta(minutes=30),
                                       reachable=True, cpu_percent=70.0))
            return await _health(w)

        h = _run(go())
        assert h.cpu.avg[40] == pytest.approx(20.0)
        assert h.cpu.peak[40] == 70.0, "the raw peak must survive the rollup read after it"
        assert h.polls == 6 and h.answered == 6

    def test_an_unanswered_stretch_is_a_gap_not_a_zero(self, db):
        w = H.Window.ending(NOW, "1h")

        async def go():
            async with D.session_scope() as s:
                for i in range(3):
                    s.add(SnmpHealthSample(snmp_device_id=1, ts=w.time_of(i), reachable=True,
                                           cpu_percent=50.0))
                for i in range(3, 6):
                    s.add(SnmpHealthSample(snmp_device_id=1, ts=w.time_of(i), reachable=False))
            return await _health(w)

        h = _run(go())
        assert h.cpu.avg[:6] == [50.0, 50.0, 50.0, None, None, None]
        assert h.answered_fraction == pytest.approx(0.5)

    def test_samples_before_the_window_are_left_out(self, db):
        w = H.Window.ending(NOW, "1h")

        async def go():
            async with D.session_scope() as s:
                s.add(SnmpHealthSample(snmp_device_id=1, ts=w.start - timedelta(microseconds=1),
                                       reachable=True, cpu_percent=99.0))
                s.add(SnmpHealthSample(snmp_device_id=1, ts=w.start, reachable=True,
                                       cpu_percent=5.0))
            return await _health(w)

        h = _run(go())
        assert h.cpu.avg[0] == 5.0 and h.polls == 1

    def test_another_devices_history_is_not_mixed_in(self, db):
        w = H.Window.ending(NOW, "1h")

        async def go():
            async with D.session_scope() as s:
                s.add(SnmpHealthSample(snmp_device_id=2, ts=w.time_of(3), reachable=True,
                                       cpu_percent=80.0))
            return await _health(w)

        assert not _run(go()).cpu.has_data


class TestTrafficHistory:
    def test_rates_errors_and_peaks_per_interface(self, db):
        w = H.Window.ending(NOW, "24h")

        async def go():
            async with D.session_scope() as s:
                for minute, (rate, err) in enumerate(((1e6, 1), (3e6, 0), (2e6, 2))):
                    s.add(SnmpInterfaceSample(interface_id=1, ts=w.time_of(4) + timedelta(minutes=minute),
                                              in_bps=rate, out_bps=rate / 2,
                                              in_errors=err, out_errors=0))
                s.add(SnmpInterfaceRollup(interface_id=1, bucket_start=w.time_of(2),
                                          bucket_seconds=FIVE_MINUTES, samples=5,
                                          in_avg=4e6, in_max=9e6, out_avg=1e6, out_max=1e6,
                                          in_errors=4, out_errors=1))
                s.add(SnmpInterfaceSample(interface_id=2, ts=w.time_of(4), in_bps=5e9,
                                          out_bps=5e9, in_errors=100, out_errors=100))
            return await _traffic(w, ids=(1,))

        t = _run(go())[1]
        assert t.inbound.avg[4] == pytest.approx(2e6)
        assert t.inbound.peak[4] == 3e6
        assert t.inbound.avg[2] == pytest.approx(4e6) and t.inbound.peak[2] == 9e6
        assert (t.in_errors, t.out_errors) == (7, 1)
        assert t.inbound.overall_peak() == 9e6, "interface 2's 5 Gbps must not leak in"

    def test_no_interfaces_is_no_query(self, db):
        assert _run(_traffic(H.Window.ending(NOW, "1h"), ids=())) == {}


# --------------------------------------------------------------------------
# Drawing
# --------------------------------------------------------------------------


class TestScale:
    @pytest.mark.parametrize("value,expected", [
        (3.4e6, 4e6), (7e6, 8e6), (9e6, 1e7), (100, 100), (101, 200),
        (0.18, 1.0), (None, 1.0), (55, 60),
    ])
    def test_the_maximum_divides_into_readable_quarters(self, value, expected):
        assert charts.nice_max(value) == pytest.approx(expected)

    def test_the_scale_never_clips_the_value(self):
        for value in (1.1, 3.9, 4.0001, 61, 799, 801, 12345):
            assert charts.nice_max(value) >= value


class TestPaths:
    def test_a_gap_breaks_the_line(self):
        path = charts.line_path([1, 2, None, None, 3, 4], 10)
        assert path.count("M") == 2, "a line drawn across a gap invents data"

    def test_a_lone_point_is_still_drawn(self):
        path = charts.line_path([None, 5, None], 10)
        assert path.startswith("M") and "H" in path

    def test_nothing_draws_nothing(self):
        assert charts.line_path([None, None], 10) == ""

    def test_values_above_the_scale_stay_inside_the_plot(self):
        ys = [float(y) for y in re.findall(r",(-?[\d.]+)", charts.line_path([0, 500], 100))]
        assert all(0 <= y <= charts.PLOT_H for y in ys)


class TestFormatting:
    @pytest.mark.parametrize("value,text", [
        (None, "—"), (0, "0 bps"), (999, "999 bps"), (1500, "1.5 Kbps"),
        (52e6, "52 Mbps"), (140e6, "140 Mbps"), (1.02e9, "1.02 Gbps"),
    ])
    def test_bits_per_second(self, value, text):
        assert charts.fmt_bps(value) == text


class TestSparkline:
    def test_thinning_keeps_gaps_and_the_point_budget(self):
        values = [1.0] * 100 + [None] * 100 + [2.0] * 88
        thin = charts._thin(values, charts.SPARK_POINTS)
        assert len(thin) <= charts.SPARK_POINTS
        assert None in thin and thin[0] == 1.0 and thin[-1] == 2.0

    def test_an_idle_port_gets_a_dash_not_a_flat_line(self):
        assert "svg" not in charts.sparkline([0, 0, None], [0, None, 0])


# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------


@pytest.fixture
def site(monkeypatch):
    """A running app with one polled device (dev1) and one that is not (dev2)."""
    async def _no_jobs(*a, **k):
        return 0
    # Keep the seeded history as seeded: no real polls against 10.0.0.1.
    monkeypatch.setattr(scheduler_module, "sync_snmp_jobs", _no_jobs)

    config = _config()

    async def seed():
        await _base(config, devices=1)
        now = utcnow()
        async with D.session_scope() as s:
            s.add(SnmpPoll(snmp_device_id=1, last_polled_at=now, last_ok_at=now,
                           uptime_seconds=90000, cpu_percent=12.0, memory_percent=40.0,
                           interfaces_total=3, interfaces_up=2))
            s.add(SnmpInterface(snmp_device_id=1, if_index=2, name="eth1", alias="NAS",
                                oper_status="up", admin_status="up", speed_mbps=1000,
                                in_bps=5e6, out_bps=1e6, last_seen=now))
            s.add(SnmpInterface(snmp_device_id=1, if_index=3, name="eth2",
                                oper_status="down", admin_status="up", last_seen=now))
            for k in range(30):
                ts = now - timedelta(minutes=k)
                s.add(SnmpHealthSample(snmp_device_id=1, ts=ts, reachable=True,
                                       cpu_percent=10.0 + k, memory_percent=40.0))
                s.add(SnmpInterfaceSample(interface_id=1, ts=ts, in_bps=1e6, out_bps=1e5))
                s.add(SnmpInterfaceSample(interface_id=2, ts=ts, in_bps=9e7, out_bps=2e7))
        await D.close_engine()

    asyncio.run(seed())
    with TestClient(create_app(config), follow_redirects=False) as client:
        client.post("/setup", data={"username": "admin", "password": PASSWORD,
                                    "password_confirm": PASSWORD})
        yield client


class TestDevicePage:
    def test_a_polled_device_shows_health_and_its_busiest_port(self, site):
        page = site.get("/devices/1").text
        assert "<h2>Health</h2>" in page
        assert "CPU" in page and "Memory" in page
        assert "Temperature" not in page, "no sensor, no empty chart"
        # NAS carried far more than eth0-1, so it is the one charted first.
        assert re.search(r'class="chart-title">\s*<span>NAS', page)
        assert page.count("<figure") == 3   # CPU, memory, traffic

    def test_a_port_can_be_chosen(self, site):
        page = site.get("/devices/1?port=1").text
        assert re.search(r'class="chart-title">\s*<span>eth0-1', page)

    def test_an_empty_port_is_not_painted_as_a_fault(self, site):
        page = site.get("/devices/1").text
        table = page[page.index('class="table compact interface-table"'):]
        assert 'pill neutral">down' in table
        assert "pill bad" not in table

    def test_the_range_picker_marks_the_chosen_range(self, site):
        page = site.get("/devices/1?range=7d").text
        assert re.search(r'href="/devices/1\?range=7d"\s+class="active"', page)
        assert re.search(r'href="/devices/1\?range=24h"\s+class=""', page)
        # Nonsense falls back rather than failing.
        assert site.get("/devices/1?range=forever").status_code == 200

    def test_no_inline_styles_for_the_csp_to_refuse(self, site):
        # style-src is 'self' only; an inline style attribute would be
        # silently ignored and the chart would lay out wrong.
        page = site.get("/devices/1").text
        assert not re.search(r'\sstyle\s*=', page)
        assert re.search(r'<script src="/static/charts\.js\?v=\w+"', page)

    def test_a_device_that_is_not_polled_says_how_to_start(self, site):
        page = site.get("/devices/2").text
        assert "Not polled over SNMP" in page and 'href="/settings/snmp"' in page
        assert "<figure" not in page

    def test_an_unknown_device_goes_back_to_the_list(self, site):
        response = site.get("/devices/999")
        assert response.status_code in (302, 303)
        assert response.headers["location"].endswith("/devices")

    def test_the_devices_list_and_settings_link_here(self, site):
        assert 'href="/devices/1"' in site.get("/devices").text
        assert 'href="/devices/1"' in site.get("/settings/snmp").text


def test_the_asset_version_follows_charts_js(tmp_path, monkeypatch):
    from spark.web import deps

    (tmp_path / "app.css").write_text("body{}")
    (tmp_path / "charts.js").write_text("// one")
    monkeypatch.setattr(deps, "STATIC_DIR", tmp_path)
    first = deps._asset_version()
    (tmp_path / "charts.js").write_text("// two")
    assert deps._asset_version() != first, "a changed script would be served stale"


def test_mac_and_vendor_live_on_the_device_page_not_the_list(site):
    """The list lost its MAC and Vendor columns; the device page keeps both,
    and the randomised-MAC warning that used to sit in the list moved with them."""
    listing = site.get("/devices").text
    assert "<th>MAC</th>" not in listing and "<th>Vendor</th>" not in listing
    assert "aa:bb:cc:00:00:01" not in listing
    page = site.get("/devices/1").text
    assert "aa:bb:cc:00:00:01" in page
    # 0xaa has the locally-administered bit set: a randomised address.
    assert "random MAC" in page
