"""Tests for limiting how many devices the Devices page shows at once.

A real network puts a few hundred rows on this page, and the page is one long
table -- so the sweep summary, the filter and the "Mark all reviewed" button
scroll off the top and stay there.

Paging is a scrolling aid, and that is the whole risk in it. Every failure mode
below is the same mistake in a different place: the page number quietly
changing what something *means*.

  * A device that falls between two pages is invisible, and this page is the
    security surface of the project -- a device you never see is a device you
    never review. That is why the ordering has a tiebreaker and why the
    round-trip test below insists every device appears exactly once.
  * A count that silently becomes "how many are on screen" turns "Showing 3 of
    41" and "Mark all 12 reviewed" into lies.
  * A page number that survives a change of filter shows an empty table for a
    subnet that has devices on it.
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
from spark.models import Device, utcnow
from spark.web.routes_devices import (
    DEFAULT_PAGE_SIZE,
    DEVICE_ORDER,
    PAGE_SIZE_CHOICES,
)

PASSWORD = "correct horse battery"

# Enough to need more than one page at the default size, and not a multiple of
# it: an exact multiple hides off-by-one errors in the last page.
ON_LAN = 63
ON_IOT = 4


def make_config(tmp: Path) -> Config:
    config = Config.model_validate(
        {
            "app": {"data_dir": str(tmp / "data"), "port": 9704, "log_level": "WARNING"},
            "network": {
                "subnets": [
                    {"name": "LAN", "cidr": "10.1.10.0/24", "vlan": 10, "attached": True},
                    {"name": "IoT", "cidr": "10.1.20.0/24", "vlan": 20, "attached": True},
                ]
            },
        }
    )
    config.app.data_dir.mkdir(parents=True, exist_ok=True)
    return config


@pytest.fixture
def client():
    tmp = Path(tempfile.mkdtemp(prefix="spark-paging-"))
    cfg = make_config(tmp)

    async def seed():
        D.init_engine(cfg)
        await D.init_db(cfg)
        # Every device carries the *same* last_seen, on purpose. That is the
        # state a real sweep leaves behind -- it records them in one batch --
        # and it is the state in which an unstable sort silently drops rows.
        seen = utcnow()
        async with D.session_scope() as s:
            for n in range(1, ON_LAN + 1):
                s.add(Device(mac=f"aa:bb:cc:00:{n // 256:02x}:{n % 256:02x}",
                             primary_ip=f"10.1.10.{n}",
                             friendly_name=f"lan{n:03d}", last_seen=seen))
            for n in range(1, ON_IOT + 1):
                s.add(Device(mac=f"aa:bb:cc:11:{n // 256:02x}:{n % 256:02x}",
                             primary_ip=f"10.1.20.{n}",
                             friendly_name=f"iot{n:03d}", last_seen=seen))
        await D.close_engine()

    asyncio.run(seed())

    with TestClient(create_app(cfg), follow_redirects=False) as c:
        c.post("/setup", data={"username": "admin", "password": PASSWORD,
                               "password_confirm": PASSWORD})
        yield c


def names(page: str) -> list[str]:
    """The devices on this page, in the order they are rendered."""
    return re.findall(r'data-original="([^"]*)"', page)


def count_text(page: str) -> str:
    match = re.search(r'<span class="muted small filter-count">(.*?)</span>', page, re.S)
    return " ".join(match.group(1).split()) if match else ""


# --------------------------------------------------------------------------
# The slice itself
# --------------------------------------------------------------------------


class TestPageSize:
    def test_the_default_caps_a_long_list(self, client):
        shown = names(client.get("/devices").text)
        assert len(shown) == DEFAULT_PAGE_SIZE

    def test_a_short_list_is_shown_whole(self, client):
        # Paging that kicks in at four devices would be pure obstruction.
        shown = names(client.get("/devices?subnet=2").text)
        assert len(shown) == ON_IOT

    def test_a_chosen_size_is_honoured(self, client):
        assert len(names(client.get("/devices?per_page=25").text)) == 25

    def test_show_all_really_shows_all(self, client):
        assert len(names(client.get("/devices?per_page=0").text)) == ON_LAN + ON_IOT

    def test_a_size_that_was_never_offered_falls_back_to_the_default(self, client):
        # The value travels in a URL people edit, bookmark and paste at each
        # other. `?per_page=100000` should render a page, not a minute of wait.
        page = client.get("/devices?per_page=100000")
        assert page.status_code == 200
        assert len(names(page.text)) == DEFAULT_PAGE_SIZE

    def test_junk_in_the_size_does_not_raise(self, client):
        page = client.get("/devices?per_page=%27%20OR%201%3D1")
        assert page.status_code == 200
        assert len(names(page.text)) == DEFAULT_PAGE_SIZE


class TestPageNumber:
    def test_the_second_page_continues_where_the_first_stopped(self, client):
        first = names(client.get("/devices?per_page=25").text)
        second = names(client.get("/devices?per_page=25&page=2").text)
        assert len(second) == 25
        assert not set(first) & set(second)

    def test_every_device_appears_exactly_once_across_the_pages(self, client):
        # The test this file exists for. A device that falls between two pages
        # is a device nobody reviews.
        seen: list[str] = []
        for number in range(1, 4):
            seen += names(client.get(f"/devices?per_page=25&page={number}").text)
        assert len(seen) == len(set(seen)), "a device was listed on two pages"
        assert set(seen) == set(names(client.get("/devices?per_page=0").text))

    def test_the_last_page_holds_the_remainder(self, client):
        # 67 devices at 25 a page: 25, 25, 17.
        assert len(names(client.get("/devices?per_page=25&page=3").text)) == 17

    def test_a_page_past_the_end_lands_on_the_last_one(self, client):
        # Rows come and go between refreshes, so a stale page number is normal.
        # An empty table here would read as a network that emptied out.
        page = client.get("/devices?per_page=25&page=99").text
        assert names(page) == names(client.get("/devices?per_page=25&page=3").text)
        assert "Page 3 of 3" in page

    def test_page_zero_and_below_land_on_the_first(self, client):
        first = names(client.get("/devices?per_page=25").text)
        assert names(client.get("/devices?per_page=25&page=0").text) == first
        assert names(client.get("/devices?per_page=25&page=-4").text) == first

    def test_junk_in_the_page_number_does_not_raise(self, client):
        page = client.get("/devices?page=banana")
        assert page.status_code == 200
        assert len(names(page.text)) == DEFAULT_PAGE_SIZE

    def test_the_order_is_stable_between_requests(self, client):
        assert (names(client.get("/devices?per_page=25&page=2").text)
                == names(client.get("/devices?per_page=25&page=2").text))

    def test_the_ordering_ends_in_a_unique_column(self):
        """The tiebreaker that keeps a device off a page boundary.

        Asserted on the ORDER BY rather than through the page, and the reason
        is worth writing down. Every device in this fixture shares a last_seen,
        which is what a real sweep produces -- but SQLite returns rows tied on
        the sort column in rowid order anyway, so deleting `Device.id` from the
        ordering changes nothing any request here can see. It would surface on
        another engine, or once the planner serves this from an index.

        A test that passes whether or not the code is there is not protecting
        anything, so this one looks at the ordering itself.
        """
        assert "device.id" in str(DEVICE_ORDER[-1]), (
            "the ordering must end in a unique column, or rows tied on "
            "last_seen can land on a page boundary twice or not at all"
        )


# --------------------------------------------------------------------------
# Composing with the subnet filter
# --------------------------------------------------------------------------


class TestWithTheSubnetFilter:
    def test_paging_applies_within_the_filtered_set(self, client):
        shown = names(client.get("/devices?subnet=1&per_page=25").text)
        assert len(shown) == 25
        assert all(name.startswith("lan") for name in shown)

    def test_the_pager_links_carry_the_filter(self, client):
        page = client.get("/devices?subnet=1&per_page=25").text
        assert 'href="/devices?subnet=1&amp;per_page=25&amp;page=2"' in page

    def test_the_filter_form_carries_no_page_number(self, client):
        # There is no hidden `page` field, which is how changing the subnet or
        # the page size goes back to page 1 without any JavaScript. Landing on
        # page 4 of a subnet with two devices shows an empty table.
        page = client.get("/devices?subnet=1&per_page=25&page=3").text
        form = re.search(r'<form method="get" action="/devices".*?</form>', page, re.S)
        assert form and 'name="page"' not in form.group(0)

    def test_the_counts_keep_meaning_what_they_said(self, client):
        # Two different numbers -- how many the filter matched, and how many of
        # those are on screen -- and paging must not collapse them into one.
        text = count_text(client.get("/devices?subnet=1&per_page=25&page=2").text)
        assert "26–50" in text
        assert str(ON_LAN) in text, "the filtered total"
        assert str(ON_LAN + ON_IOT) in text, "the inventory total"

    def test_the_unfiltered_count_is_of_the_whole_inventory(self, client):
        text = count_text(client.get("/devices?per_page=25").text)
        assert text == f"Showing 1–25 of {ON_LAN + ON_IOT} device(s)."

    def test_a_short_filtered_list_keeps_the_old_wording(self, client):
        assert count_text(client.get("/devices?subnet=2").text) == (
            f"Showing {ON_IOT} of {ON_LAN + ON_IOT} device(s)."
        )


# --------------------------------------------------------------------------
# What the page offers
# --------------------------------------------------------------------------


class TestControls:
    def test_the_pager_appears_only_when_something_is_hidden(self, client):
        assert "Page 1 of" in client.get("/devices").text
        assert "Page 1 of" not in client.get("/devices?per_page=0").text

    def test_the_last_page_has_no_next_link(self, client):
        page = client.get("/devices?per_page=25&page=3").text
        assert 'rel="next"' not in page
        assert 'rel="prev"' in page

    def test_the_first_page_has_no_previous_link(self, client):
        page = client.get("/devices?per_page=25").text
        assert 'rel="prev"' not in page
        assert 'rel="next"' in page

    def test_the_size_dropdown_is_not_offered_on_a_short_list(self, client):
        # Four devices do not need a control explaining that all four are
        # visible.
        assert 'id="page-size"' not in client.get("/devices?subnet=2").text

    def test_the_size_dropdown_stays_once_it_has_been_touched(self, client):
        # Otherwise picking 25 on a 30-device network hides the only control
        # that could put it back.
        page = client.get("/devices?subnet=2&per_page=25").text
        assert 'id="page-size"' in page

    def test_the_dropdown_is_not_disabled_when_showing_everything(self, client):
        # A disabled select submits nothing. This page has been bitten by that
        # twice already -- the sweep interval and the subnet filter.
        page = client.get("/devices?per_page=0").text
        field = re.search(r'<select name="per_page".*?</select>', page, re.S)
        assert field and "disabled" not in field.group(0)

    def test_every_offered_size_is_accepted_back(self, client):
        for size in PAGE_SIZE_CHOICES:
            shown = names(client.get(f"/devices?per_page={size}").text)
            assert len(shown) == min(size, ON_LAN + ON_IOT), size


class TestCountsOutsideTheTable:
    def test_mark_all_reviewed_counts_the_whole_list_not_the_page(self, client):
        # The button acknowledges every unreviewed device, so a number that
        # shrank to what fits on screen would understate what pressing it does.
        page = client.get("/devices?per_page=25").text
        assert f"Mark all {ON_LAN + ON_IOT} reviewed" in page

    def test_it_still_narrows_with_the_subnet_filter(self, client):
        page = client.get("/devices?subnet=2").text
        assert f"Mark all {ON_IOT} reviewed" in page
