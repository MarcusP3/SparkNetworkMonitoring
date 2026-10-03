"""The Devices page in SPARK 2: the tiles, the search box and the Watched
filter.

The tiles count the whole inventory and the filters narrow only the list
under them -- a tile that shrank as you filtered would describe the view
rather than the network. The search and the Watched filter are server-side
query parameters like the others, so they page, bookmark and survive the
live refresh the same way.
"""

from __future__ import annotations

import asyncio
import re
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from spark import db as D
from spark.config import Config
from spark.main import create_app
from spark.models import CheckType, Device, DeviceAddress, Service, Target, utcnow

PASSWORD = "correct horse battery"


@pytest.fixture
def client():
    tmp = Path(tempfile.mkdtemp(prefix="spark-devpage-"))
    config = Config.model_validate({
        "app": {"data_dir": str(tmp / "data"), "log_level": "WARNING"},
        "network": {"subnets": []},
    })
    config.app.data_dir.mkdir(parents=True, exist_ok=True)

    async def seed():
        D.init_engine(config)
        await D.init_db(config)
        async with D.session_scope() as s:
            s.add(Device(id=1, mac="aa:bb:cc:00:00:01", primary_ip="172.16.10.2", hostname="core-switch",
                         vendor="Ubiquiti", last_seen=utcnow(), acknowledged=True))
            s.add(Device(id=2, mac="dc:a6:32:00:00:02", primary_ip="172.16.10.53", friendly_name="dns-01",
                         vendor="Raspberry Pi", last_seen=utcnow(), acknowledged=False))
            s.add(Device(id=3, primary_ip="172.16.20.7", hostname="printer.lan", last_seen=utcnow(),
                         acknowledged=False))
            s.add(Device(id=4, primary_ip="172.16.10.99", hostname="hidden", ignored=True))
            await s.flush()
            s.add(DeviceAddress(device_id=3, ip="172.16.30.7"))
            s.add(Target(name="core-switch", check_type=CheckType.PING, address="172.16.10.2", device_id=1))
            s.add(Service(device_id=1, port=22, name="ssh", state="open"))
            s.add(Service(device_id=2, port=53, name="dns", state="open"))
            s.add(Service(device_id=4, port=80, name="http", state="open"))  # on an ignored device
        await D.close_engine()
    asyncio.run(seed())

    with TestClient(create_app(config), follow_redirects=False) as c:
        c.post("/setup", data={"setup_code": c.app.state.setup_code, "username": "admin",
                               "password": PASSWORD, "password_confirm": PASSWORD})
        yield c


def names(page: str) -> list[str]:
    return re.findall(r'data-original="([^"]*)"\s+placeholder="([^"]*)"', page)


def shown(page: str) -> set[str]:
    return {original or placeholder for original, placeholder in names(page)}


def tile(page: str, name: str) -> str:
    match = re.search(r'<span class="stat-name">' + re.escape(name) +
                      r'</span></span><span class="stat-value">([^<]*)</span>', page)
    assert match, f"no tile {name!r}"
    return match.group(1)


class TestTiles:
    def test_they_count_the_inventory(self, client):
        page = client.get("/devices").text
        assert [tile(page, n) for n in ("Devices", "New", "Watched", "SNMP polled", "Services")] == \
            ["3", "2", "1", "0", "2"], "the ignored device and its service are not counted"
        assert tile(page, "Last sweep") == "—", "no sweep has run"

    def test_filters_do_not_change_them(self, client):
        page = client.get("/devices?watch=yes").text
        assert tile(page, "Devices") == "3" and tile(page, "New") == "2"
        assert shown(page) == {"core-switch"}


class TestSearch:
    @pytest.mark.parametrize("q, expected", [
        ("dns", {"dns-01"}),                       # friendly name
        ("CORE", {"core-switch"}),                 # host name, any case
        ("172.16.10.", {"core-switch", "dns-01"}),  # address
        ("dc:a6", {"dns-01"}),                     # MAC
        ("raspberry", {"dns-01"}),                 # vendor
        ("172.16.30.7", {"printer.lan"}),          # a merged extra address
        ("nothing-like-it", set()),
    ])
    def test_it_matches(self, client, q, expected):
        page = client.get("/devices", params={"q": q}).text
        assert shown(page) == expected
        assert f'name="q" value="{q}"' in page, "the box keeps what was searched"

    def test_no_match_says_so(self, client):
        page = client.get("/devices?q=nothing-like-it").text
        assert "No devices match" in page and '<a href="/devices">Show all</a>' in page

    def test_a_long_search_is_cut(self, client):
        page = client.get("/devices", params={"q": "x" * 500}).text
        assert 'value="' + "x" * 100 + '"' in page and "x" * 101 not in page


class TestWatchedFilter:
    def test_yes_and_no(self, client):
        assert shown(client.get("/devices?watch=yes").text) == {"core-switch"}
        assert shown(client.get("/devices?watch=no").text) == {"dns-01", "printer.lan"}

    def test_nonsense_shows_everything(self, client):
        assert len(shown(client.get("/devices?watch=maybe").text)) == 3

    def test_it_is_a_filter_bar_dropdown(self, client):
        page = client.get("/devices?watch=no").text
        assert '<select name="watch" id="watch-filter" class="auto-submit">' in page
        assert '<option value="no" selected>' in page


def test_paging_keeps_the_search_and_the_watched_filter():
    from spark.web.routes_devices import _query
    assert _query("", 25, 2, "", "", "nas", "no") == "?q=nas&watch=no&per_page=25&page=2"
    assert _query("", 25, 2, "", "", "", "bogus") == "?per_page=25&page=2"


class TestRows:
    def test_a_row_reads_like_the_design(self, client):
        page = " ".join(client.get("/devices").text.split())
        assert '<small class="dev-vendor">Ubiquiti</small>' in page
        assert '<div class="dev-mac">aa:bb:cc:00:00:01</div>' in page
        assert '> ssh 22 </button>' in page
        # Watched first, Details last, and the schedule inside the sweep panel.
        assert re.search(r'is-watched.*?Ignore.*?>Details</a>', page)
        assert page.index('class="card sweep-status"') < page.index('class="row-form scan-schedule"') \
            < page.index('class="card devices-card"')
