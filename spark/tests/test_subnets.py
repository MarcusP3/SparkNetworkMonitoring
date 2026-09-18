"""Tests for subnet management and the device subnet filter.

Subnets moved out of `spark.yaml` and into the database here, which makes this
the riskiest change in the project so far: it runs a migration on a live
database, and it changes where an existing install's configuration comes from.
Two failure modes are worth more than the rest.

The first is the seed running twice. If spark.yaml is merged in on every boot,
deleting a subnet in the UI works until the next restart puts it back, and the
delete button becomes a lie you only catch weeks later.

The second is silent non-matching. A CIDR with a typo, or a subnet renamed
after its devices were found, does not raise anywhere -- the page just shows
fewer devices than the network has, and nothing says why.
"""

from __future__ import annotations

import asyncio
import re
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from spark import db as D
from spark import subnets as S
from spark.config import Config
from spark.main import create_app
from spark.models import Device, Subnet, utcnow

PASSWORD = "correct horse battery"


def make_config(tmp: Path, subnets: list[dict] | None = None) -> Config:
    config = Config.model_validate(
        {
            "app": {"data_dir": str(tmp / "data"), "port": 9702, "log_level": "WARNING"},
            "network": {"subnets": subnets or []},
        }
    )
    config.app.data_dir.mkdir(parents=True, exist_ok=True)
    return config


@pytest.fixture
def db():
    tmp = Path(tempfile.mkdtemp(prefix="spark-subnets-"))
    cfg = make_config(tmp)

    async def setup():
        D.init_engine(cfg)
        await D.init_db(cfg)

    asyncio.run(setup())
    yield cfg
    asyncio.run(D.close_engine())


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


class TestCidrValidation:
    def test_a_host_address_is_accepted_and_canonicalised(self):
        # What people actually type is the address of the box they are on.
        # Rejecting it teaches them to distrust the field.
        assert S.normalise_cidr("10.1.10.7/24") == "10.1.10.0/24"

    def test_whitespace_is_forgiven(self):
        assert S.normalise_cidr("  192.168.1.0/24 ") == "192.168.1.0/24"

    def test_a_missing_prefix_says_what_to_add(self):
        with pytest.raises(S.SubnetError) as caught:
            S.normalise_cidr("10.1.10.0")
        assert "/24" in str(caught.value), "the message should show the fix"

    def test_nonsense_is_refused(self):
        for bad in ("", "not a network", "10.1.10.0/33", "999.1.1.0/24"):
            with pytest.raises(S.SubnetError):
                S.normalise_cidr(bad)

    def test_ipv6_is_accepted(self):
        assert S.normalise_cidr("fd00::/64") == "fd00::/64"


class TestVlanValidation:
    def test_blank_is_not_an_error(self):
        assert S.parse_vlan("") is None
        assert S.parse_vlan(None) is None
        assert S.parse_vlan("   ") is None

    def test_a_tag_is_an_integer(self):
        assert S.parse_vlan("30") == 30
        assert S.parse_vlan(" 4094 ") == 4094

    def test_out_of_range_is_refused(self):
        for bad in ("-1", "4095", "99999"):
            with pytest.raises(S.SubnetError):
                S.parse_vlan(bad)

    def test_words_are_refused(self):
        with pytest.raises(S.SubnetError):
            S.parse_vlan("guest")


class TestOversized:
    def test_a_16_is_flagged_but_not_refused(self):
        # A /16 is a reasonable thing to document and an unreasonable thing to
        # sweep. Refusing it would lose the documentation; saying nothing would
        # leave a subnet that looks configured and never scans.
        assert S.too_large_to_sweep("10.0.0.0/16")

    def test_a_24_is_fine(self):
        assert not S.too_large_to_sweep("10.1.10.0/24")


# --------------------------------------------------------------------------
# Membership
# --------------------------------------------------------------------------


class TestMembership:
    def test_an_address_inside_matches(self):
        subnet = Subnet(cidr="10.1.10.0/24")
        assert subnet.contains("10.1.10.50")
        assert not subnet.contains("10.1.20.50")

    def test_a_missing_or_junk_address_is_not_a_match_and_does_not_raise(self):
        subnet = Subnet(cidr="10.1.10.0/24")
        assert not subnet.contains(None)
        assert not subnet.contains("")
        assert not subnet.contains("not-an-ip")

    def test_an_unparseable_subnet_matches_nothing_rather_than_raising(self):
        # One bad row should make a page section look wrong, not take the
        # Devices page down with a 500.
        subnet = Subnet(cidr="garbage")
        assert subnet.network is None
        assert not subnet.contains("10.1.10.50")

    def test_the_most_specific_subnet_wins(self):
        wide = Subnet(id=1, cidr="10.0.0.0/8", name="everything")
        narrow = Subnet(id=2, cidr="10.1.10.0/24", name="LAN")
        chosen = S.subnet_for([wide, narrow], "10.1.10.50")
        assert chosen is narrow, "a documented supernet must not swallow its children"

    def test_an_address_in_no_subnet_returns_none(self):
        assert S.subnet_for([Subnet(cidr="10.1.10.0/24")], "192.168.0.5") is None


# --------------------------------------------------------------------------
# CRUD
# --------------------------------------------------------------------------


class TestCrud:
    def test_create_and_list_sorted_by_address(self, db):
        async def go():
            async with D.session_scope() as s:
                await S.create(s, cidr="10.1.30.0/24", name="Servers")
                await S.create(s, cidr="10.1.10.0/24", name="LAN")
                await S.create(s, cidr="10.1.20.0/24", name="IoT")
            async with D.session_scope() as s:
                return [x.cidr for x in await S.list_subnets(s)]

        # Sorted by address, so the list reads like a network diagram rather
        # than like an insertion log.
        assert asyncio.run(go()) == ["10.1.10.0/24", "10.1.20.0/24", "10.1.30.0/24"]

    def test_a_duplicate_cidr_is_refused_with_the_existing_name(self, db):
        async def go():
            async with D.session_scope() as s:
                await S.create(s, cidr="10.1.10.0/24", name="LAN")
                try:
                    await S.create(s, cidr="10.1.10.7/24", name="LAN again")
                except S.SubnetError as exc:
                    return str(exc)
            return None

        message = asyncio.run(go())
        # Canonicalised first, so the near-duplicate is caught rather than
        # stored as a second row that shadows the first.
        assert message and "LAN" in message

    def test_update_changes_the_vlan_without_touching_anything_else(self, db):
        async def go():
            async with D.session_scope() as s:
                created = await S.create(s, cidr="10.1.10.0/24", name="LAN", vlan=10)
                subnet_id = created.id
            async with D.session_scope() as s:
                await S.update(s, subnet_id, cidr="10.1.10.0/24", name="LAN", vlan="99")
            async with D.session_scope() as s:
                subnet = await s.get(Subnet, subnet_id)
                return subnet.vlan, subnet.name, subnet.cidr

        assert asyncio.run(go()) == (99, "LAN", "10.1.10.0/24")

    def test_a_vlan_can_be_cleared_back_to_nothing(self, db):
        async def go():
            async with D.session_scope() as s:
                created = await S.create(s, cidr="10.1.10.0/24", vlan=10)
                subnet_id = created.id
            async with D.session_scope() as s:
                await S.update(s, subnet_id, cidr="10.1.10.0/24", vlan="")
            async with D.session_scope() as s:
                return (await s.get(Subnet, subnet_id)).vlan

        assert asyncio.run(go()) is None

    def test_update_will_not_collide_with_another_subnet(self, db):
        async def go():
            async with D.session_scope() as s:
                await S.create(s, cidr="10.1.10.0/24", name="LAN")
                other = await S.create(s, cidr="10.1.20.0/24", name="IoT")
                other_id = other.id
            async with D.session_scope() as s:
                try:
                    await S.update(s, other_id, cidr="10.1.10.0/24")
                except S.SubnetError as exc:
                    return str(exc)
            return None

        assert asyncio.run(go()) is not None

    def test_deleting_a_subnet_keeps_the_devices_found_on_it(self, db):
        async def go():
            async with D.session_scope() as s:
                created = await S.create(s, cidr="10.1.10.0/24", name="LAN")
                subnet_id = created.id
                s.add(Device(mac="aa:bb:cc:dd:ee:ff", primary_ip="10.1.10.50"))
            async with D.session_scope() as s:
                await S.delete(s, subnet_id)
            async with D.session_scope() as s:
                return await s.scalar(select(func.count()).select_from(Device))

        # A device is evidence that something was on the network. Deleting the
        # segment you were looking through says nothing about what you saw.
        assert asyncio.run(go()) == 1

    def test_deleting_something_already_gone_is_not_an_error(self, db):
        async def go():
            async with D.session_scope() as s:
                return await S.delete(s, 999)

        assert asyncio.run(go()) is None


# --------------------------------------------------------------------------
# Seeding from spark.yaml
# --------------------------------------------------------------------------


class TestSeeding:
    def _fresh(self, subnets):
        tmp = Path(tempfile.mkdtemp(prefix="spark-seed-"))
        cfg = make_config(tmp, subnets)

        async def setup():
            D.init_engine(cfg)
            await D.init_db(cfg)

        asyncio.run(setup())
        return cfg

    def test_yaml_subnets_are_copied_in_on_the_first_run(self):
        cfg = self._fresh([
            {"name": "LAN", "cidr": "10.1.10.0/24", "vlan": 1, "attached": True},
            {"name": "IoT", "cidr": "10.1.20.0/24", "vlan": 20, "attached": False},
        ])
        try:
            async def go():
                async with D.session_scope() as s:
                    await S.seed_from_config(s, cfg)
                async with D.session_scope() as s:
                    return [(x.cidr, x.name, x.vlan, x.attached)
                            for x in await S.list_subnets(s)]

            assert asyncio.run(go()) == [
                ("10.1.10.0/24", "LAN", 1, True),
                ("10.1.20.0/24", "IoT", 20, False),
            ]
        finally:
            asyncio.run(D.close_engine())

    def test_a_deleted_subnet_stays_deleted_across_restarts(self):
        """The one that would have bitten hardest.

        Seeding on "is the table empty" rather than on a flag means deleting
        every subnet in the UI works right up until the next restart puts them
        all back, and the delete button becomes something you stop trusting.
        """
        cfg = self._fresh([{"name": "LAN", "cidr": "10.1.10.0/24", "attached": True}])
        try:
            async def go():
                async with D.session_scope() as s:
                    await S.seed_from_config(s, cfg)          # first boot
                async with D.session_scope() as s:
                    subnets = await S.list_subnets(s)
                    await S.delete(s, subnets[0].id)          # user removes it
                async with D.session_scope() as s:
                    await S.seed_from_config(s, cfg)          # restart
                async with D.session_scope() as s:
                    return len(await S.list_subnets(s))

            assert asyncio.run(go()) == 0
        finally:
            asyncio.run(D.close_engine())

    def test_seeding_twice_in_a_row_does_not_duplicate(self):
        cfg = self._fresh([{"name": "LAN", "cidr": "10.1.10.0/24", "attached": True}])
        try:
            async def go():
                async with D.session_scope() as s:
                    await S.seed_from_config(s, cfg)
                async with D.session_scope() as s:
                    await S.seed_from_config(s, cfg)
                async with D.session_scope() as s:
                    return len(await S.list_subnets(s))

            assert asyncio.run(go()) == 1
        finally:
            asyncio.run(D.close_engine())

    def test_an_unparseable_yaml_subnet_is_skipped_not_fatal(self):
        cfg = self._fresh([
            {"name": "broken", "cidr": "10.1.10.0/24", "attached": True},
        ])
        # Bypass pydantic's own validation to simulate a row that got in.
        cfg.network.subnets[0].cidr = "not-a-network"
        try:
            async def go():
                async with D.session_scope() as s:
                    await S.seed_from_config(s, cfg)
                async with D.session_scope() as s:
                    return len(await S.list_subnets(s))

            # Startup must not die on a bad line in a config file.
            assert asyncio.run(go()) == 0
        finally:
            asyncio.run(D.close_engine())


# --------------------------------------------------------------------------
# The migration
# --------------------------------------------------------------------------


class TestMigration:
    def test_a_version_2_database_gains_the_subnet_table(self):
        """Run the real migration against a database that predates it.

        Migrations are append-only and there is no going back, so the only
        honest test is one that starts from the old schema rather than from
        `create_all`.
        """
        tmp = Path(tempfile.mkdtemp(prefix="spark-migrate-"))
        cfg = make_config(tmp)

        async def build_old():
            D.init_engine(cfg)
            await D.init_db(cfg)
            async with D.session_scope() as s:
                # Wind the database back: drop the table and claim version 2.
                await s.execute(text("DROP TABLE IF EXISTS subnet"))
                await D._set_version(s, 2)
            await D.close_engine()

        async def upgrade():
            D.init_engine(cfg)
            await D.init_db(cfg)
            async with D.session_scope() as s:
                version = await D._get_version(s)
                has_table = (
                    await s.execute(
                        text("SELECT name FROM sqlite_master "
                             "WHERE type='table' AND name='subnet'")
                    )
                ).first()
                # Writing through the ORM proves the columns match the model,
                # not merely that a table with that name exists.
                await S.create(s, cidr="10.1.10.0/24", name="LAN", vlan=10)
            await D.close_engine()
            return version, has_table is not None

        asyncio.run(build_old())
        version, has_table = asyncio.run(upgrade())
        assert version == D.CURRENT_VERSION == 3
        assert has_table


class TestUpgradeEndToEnd:
    def test_an_existing_install_comes_up_with_its_yaml_subnets_in_the_ui(self):
        """The path a real upgrade takes, start to finish.

        A database at version 2 with devices already in it, a spark.yaml with
        subnets, and then the app started normally. Everything else in this
        file tests a piece of that; this tests that the pieces are wired to
        each other, which is the part that has actually gone wrong before.
        """
        tmp = Path(tempfile.mkdtemp(prefix="spark-upgrade-"))
        cfg = make_config(tmp, [
            {"name": "LAN", "cidr": "10.1.10.0/24", "vlan": 1, "attached": True},
            {"name": "IoT", "cidr": "10.1.20.0/24", "vlan": 20, "attached": False},
        ])

        async def build_old():
            D.init_engine(cfg)
            await D.init_db(cfg)
            async with D.session_scope() as s:
                s.add(Device(mac="aa:bb:cc:dd:ee:ff", primary_ip="10.1.20.60",
                             friendly_name="sensor", last_seen=utcnow()))
            async with D.session_scope() as s:
                await s.execute(text("DROP TABLE IF EXISTS subnet"))
                await s.execute(text("DELETE FROM setting WHERE key = 'network'"))
                await D._set_version(s, 2)
            await D.close_engine()

        asyncio.run(build_old())

        with TestClient(create_app(cfg), follow_redirects=False) as client:
            client.post("/setup", data={"username": "admin", "password": PASSWORD,
                                        "password_confirm": PASSWORD})
            settings = client.get("/settings").text
            assert "10.1.10.0/24" in settings and "10.1.20.0/24" in settings

            # And the device that predates the subnet table is classified by it.
            devices = client.get("/devices?subnet=2").text
            assert 'data-original="sensor"' in devices


# --------------------------------------------------------------------------
# The pages
# --------------------------------------------------------------------------


@pytest.fixture
def client():
    tmp = Path(tempfile.mkdtemp(prefix="spark-subnets-web-"))
    cfg = make_config(tmp, [{"name": "LAN", "cidr": "10.1.10.0/24", "vlan": 1,
                             "attached": True}])

    async def seed_devices():
        D.init_engine(cfg)
        await D.init_db(cfg)
        async with D.session_scope() as s:
            s.add(Device(mac="aa:bb:cc:dd:ee:ff", primary_ip="10.1.10.50",
                         friendly_name="nas", last_seen=utcnow()))
            s.add(Device(mac="b8:27:eb:11:22:33", primary_ip="10.1.20.60",
                         friendly_name="sensor", last_seen=utcnow()))
            s.add(Device(mac="52:54:00:aa:bb:cc", primary_ip="172.16.5.5",
                         friendly_name="stray", last_seen=utcnow()))
        await D.close_engine()

    asyncio.run(seed_devices())

    with TestClient(create_app(cfg), follow_redirects=False) as c:
        c.post("/setup", data={"username": "admin", "password": PASSWORD,
                               "password_confirm": PASSWORD})
        yield c


def device_names(page: str) -> set[str]:
    return set(re.findall(r'data-original="([^"]*)"', page))


class TestSettingsPage:
    def test_the_seeded_subnet_is_listed(self, client):
        page = client.get("/settings").text
        assert "10.1.10.0/24" in page and "LAN" in page

    def test_adding_a_subnet(self, client):
        client.post("/settings/subnets",
                    data={"cidr": "10.1.20.0/24", "name": "IoT", "vlan": "20",
                          "attached": "1"})
        page = client.get("/settings").text
        assert "10.1.20.0/24" in page and "IoT" in page

    def test_a_bad_cidr_comes_back_on_the_page_with_the_typing_intact(self, client):
        response = client.post("/settings/subnets",
                               data={"cidr": "10.1.99", "name": "Typo"})
        assert response.status_code == 400
        # Clearing the form on a validation error is a worse outcome than the
        # typo was.
        assert "10.1.99" in response.text and "Typo" in response.text

    def test_editing_a_vlan_tag(self, client):
        client.post("/settings/subnets/1",
                    data={"cidr": "10.1.10.0/24", "name": "LAN", "vlan": "42",
                          "attached": "1", "enabled": "1"})
        assert 'value="42"' in client.get("/settings").text

    def test_unticking_attached_actually_unticks_it(self, client):
        client.post("/settings/subnets/1",
                    data={"cidr": "10.1.10.0/24", "name": "LAN", "enabled": "1"})
        page = client.get("/settings").text
        block = re.search(r'name="attached".*?>', page, re.S)
        assert block and "checked" not in block.group(0)

    def test_removing_a_subnet(self, client):
        client.post("/settings/subnets/1/delete")
        page = client.get("/settings").text
        # Scoped to the table: the add form's placeholder is also a CIDR, so a
        # page-wide search would keep matching after the row was gone.
        assert 'name="cidr" value="10.1.10.0/24"' not in page
        assert "No subnets configured" in page

    def test_settings_needs_a_login(self, client):
        client.post("/logout")
        response = client.get("/settings")
        assert response.status_code == 303
        assert "/login" in response.headers.get("location", "")


class TestDashboard:
    """The dashboard's subnet table reads the same source as everything else.

    It did not, at first. Devices, the scheduler and the sweep were moved onto
    the database and the dashboard was left reading `config.network.subnets`,
    so it kept showing spark.yaml's idea of the network while Settings edited
    the real one. Nothing raised; the two pages just disagreed.
    """

    def test_a_subnet_added_in_settings_appears_on_the_dashboard(self, client):
        client.post("/settings/subnets",
                    data={"cidr": "10.9.9.0/24", "name": "Guest", "vlan": "90",
                          "attached": "1"})
        page = client.get("/").text
        assert "10.9.9.0/24" in page and "Guest" in page

    def test_a_subnet_removed_in_settings_leaves_the_dashboard(self, client):
        assert "10.1.10.0/24" in client.get("/").text
        client.post("/settings/subnets/1/delete")
        assert "10.1.10.0/24" not in client.get("/").text

    def test_an_edited_vlan_tag_shows_on_the_dashboard(self, client):
        client.post("/settings/subnets/1",
                    data={"cidr": "10.1.10.0/24", "name": "LAN", "vlan": "55",
                          "attached": "1", "enabled": "1"})
        assert "55" in client.get("/").text

    def test_the_empty_warning_points_at_settings_not_the_yaml_file(self, client):
        client.post("/settings/subnets/1/delete")
        page = client.get("/").text
        assert "No subnets configured" in page
        assert "spark.yaml" not in page, "the file is no longer where subnets live"

    def test_a_subnet_with_sweep_unticked_is_called_out(self, client):
        client.post("/settings/subnets/1",
                    data={"cidr": "10.1.10.0/24", "name": "LAN", "attached": "1"})
        page = client.get("/").text
        # Listed but not swept is a real state and silently looks like a
        # working subnet that never finds anything.
        assert "not swept" in page


class TestDeviceFilter:
    def test_unfiltered_shows_everything(self, client):
        assert device_names(client.get("/devices").text) == {"nas", "sensor", "stray"}

    def test_filtering_to_a_subnet_shows_only_its_devices(self, client):
        page = client.get("/devices?subnet=1").text
        assert device_names(page) == {"nas"}

    def test_the_unassigned_filter_finds_devices_on_no_configured_segment(self, client):
        # This is how you notice a segment you forgot to configure, so it has
        # to be selectable rather than merely implied.
        page = client.get("/devices?subnet=none").text
        assert device_names(page) == {"sensor", "stray"}

    def test_adding_a_subnet_reclassifies_devices_already_discovered(self, client):
        client.post("/settings/subnets",
                    data={"cidr": "10.1.20.0/24", "name": "IoT", "attached": "1"})
        page = client.get("/devices?subnet=2").text
        # Membership is computed from the address, so a subnet added today
        # picks up a device found last week.
        assert device_names(page) == {"sensor"}

    def test_renaming_a_subnet_does_not_orphan_its_devices(self, client):
        client.post("/settings/subnets/1",
                    data={"cidr": "10.1.10.0/24", "name": "Renamed", "attached": "1",
                          "enabled": "1"})
        assert device_names(client.get("/devices?subnet=1").text) == {"nas"}

    def test_a_stale_filter_shows_everything_rather_than_nothing(self, client):
        page = client.get("/devices?subnet=999").text
        # A bookmark pointing at a deleted subnet should not look like a
        # network that emptied out.
        assert device_names(page) == {"nas", "sensor", "stray"}

    def test_junk_in_the_filter_does_not_raise(self, client):
        response = client.get("/devices?subnet=%27%20OR%201%3D1")
        assert response.status_code == 200
        assert device_names(response.text) == {"nas", "sensor", "stray"}

    def test_the_count_is_of_the_whole_inventory_not_the_filtered_slice(self, client):
        page = client.get("/devices?subnet=1").text
        assert "Showing 1 of 3" in page

    def test_the_vlan_tag_shows_against_a_device(self, client):
        client.post("/settings/subnets/1",
                    data={"cidr": "10.1.10.0/24", "name": "LAN", "vlan": "77",
                          "attached": "1", "enabled": "1"})
        page = client.get("/devices?subnet=1").text
        assert "77" in page

    def test_the_filter_survives_having_no_subnets_at_all(self, client):
        client.post("/settings/subnets/1/delete")
        response = client.get("/devices")
        assert response.status_code == 200
        assert device_names(response.text) == {"nas", "sensor", "stray"}
