"""Tests for the editable port list.

The built-in 45 are a good default and a bad answer to "what is on *my*
network". Making the list editable is cheap; the three things that are not:

  * **Switching a port off must not close what is on it.** This was a real bug
    the moment the list stopped being constant. `record_scan` closes any
    SCAN-sourced service it did not find, and it used to do that without asking
    whether it had looked -- so unticking SMB would have marked every SMB
    service on the network closed at the next scan, on the strength of never
    having probed them. The same latent bug was already reachable through the
    control probe, which removes intercepted ports from the list.

  * **The cost has to be stated, and stated correctly.** Every port added is
    scan time and every one removed buys it back. The estimate is asserted
    here against numbers measured from real sockets, because the comment it
    replaced had the right shape, no concurrency term, and was out tenfold.

  * **The stored blob is hand-editable and survives upgrades.** One unusable
    record should cost its own line, not the Settings page.
"""

from __future__ import annotations

import asyncio
import re
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from spark import db as D
from spark import port_catalogue as PC
from spark import subnets as subnet_service
from spark.config import Config
from spark.discovery import ports as P
from spark.discovery.services import CLOSED, OPEN, record_scan
from spark.main import create_app
from spark.models import Device, Service, ServiceSource, utcnow

PASSWORD = "correct horse battery"


def make_config(tmp: Path) -> Config:
    config = Config.model_validate(
        {
            "app": {"data_dir": str(tmp / "data"), "port": 9705, "log_level": "WARNING"},
            "network": {"subnets": [{"name": "LAN", "cidr": "10.1.10.0/24",
                                     "vlan": 10, "attached": True}]},
        }
    )
    config.app.data_dir.mkdir(parents=True, exist_ok=True)
    return config


@pytest.fixture
def db():
    tmp = Path(tempfile.mkdtemp(prefix="spark-catalogue-"))
    cfg = make_config(tmp)

    async def setup():
        D.init_engine(cfg)
        await D.init_db(cfg)
        async with D.session_scope() as s:
            s.add(Device(mac="aa:bb:cc:dd:ee:ff", primary_ip="10.1.10.50",
                         friendly_name="nas", last_seen=utcnow()))

    asyncio.run(setup())
    yield cfg
    asyncio.run(D.close_engine())


@pytest.fixture
def client(db):
    with TestClient(create_app(db), follow_redirects=False) as c:
        c.post("/setup", data={"username": "admin", "password": PASSWORD,
                               "password_confirm": PASSWORD})
        yield c


# --------------------------------------------------------------------------
# Merging
# --------------------------------------------------------------------------


class TestEffectiveList:
    def test_the_default_is_the_built_in_list(self):
        assert PC.Catalogue().ports() == dict(sorted(P.WELL_KNOWN.items()))

    def test_a_custom_port_is_added(self):
        cat = PC.Catalogue(custom=(PC.CustomPort(8112, "deluge"),))
        assert cat.ports()[8112] == "deluge"
        assert len(cat.ports()) == len(P.WELL_KNOWN) + 1

    def test_a_custom_port_may_have_no_name(self):
        assert PC.Catalogue(custom=(PC.CustomPort(8112),)).ports()[8112] is None

    def test_a_disabled_built_in_leaves_the_list(self):
        cat = PC.Catalogue(disabled=frozenset({445, 139}))
        assert 445 not in cat.ports() and 139 not in cat.ports()
        assert len(cat.ports()) == len(P.WELL_KNOWN) - 2

    def test_a_custom_port_on_a_built_in_renames_it(self):
        # 3000 is Grafana to most people and somebody else's app to you. The
        # name is documentation, so the person's wins.
        cat = PC.Catalogue(custom=(PC.CustomPort(3000, "my app"),))
        assert P.WELL_KNOWN[3000] == "grafana"
        assert cat.ports()[3000] == "my app"
        assert len(cat.ports()) == len(P.WELL_KNOWN), "a rename is not an addition"

    def test_a_custom_port_wins_over_the_same_port_disabled(self):
        # Adding it back explicitly is the clearer of the two instructions.
        cat = PC.Catalogue(custom=(PC.CustomPort(445, "file server"),),
                           disabled=frozenset({445}))
        assert cat.ports()[445] == "file server"

    def test_the_list_is_port_ordered(self):
        cat = PC.Catalogue(custom=(PC.CustomPort(9999), PC.CustomPort(1)))
        assert list(cat.ports()) == sorted(cat.ports())


# --------------------------------------------------------------------------
# Validation and storage
# --------------------------------------------------------------------------


class TestParsing:
    @pytest.mark.parametrize("value,expected", [
        ("8112", 8112), (8112, 8112), ("  443 ", 443), ("1", 1), ("65535", 65535),
    ])
    def test_accepted(self, value, expected):
        assert PC.parse_port(value) == expected

    @pytest.mark.parametrize("value", [
        "0", "65536", "-1", "", "  ", "http", "80/tcp", "8000-8010", None, 1.5, True,
    ])
    def test_rejected(self, value):
        # `True` is in there deliberately: bool is an int in Python, so a naive
        # int() makes it port 1.
        assert PC.parse_port(value) is None

    def test_a_name_is_trimmed_and_bounded(self):
        assert PC.clean_name("  deluge  ") == "deluge"
        assert PC.clean_name("a  b\n c") == "a b c"
        assert len(PC.clean_name("x" * 100)) == PC.MAX_NAME
        assert PC.clean_name("   ") is None
        assert PC.clean_name(None) is None


class TestStoredBlob:
    def test_a_round_trip_survives(self):
        cat = PC.Catalogue(custom=(PC.CustomPort(8112, "deluge"),),
                           disabled=frozenset({445}))
        assert PC.from_dict(cat.as_dict()) == cat

    def test_junk_is_discarded_a_line_at_a_time(self):
        # The blob is hand-editable and survives upgrades. One bad record
        # should not take the Settings page with it.
        cat = PC.from_dict({
            "custom": [
                {"port": 8112, "name": "deluge"},
                {"port": 70000, "name": "impossible"},
                {"port": "nonsense"},
                "not a dict",
                {"port": 8112, "name": "duplicate"},
                {"port": 1880},
            ],
            "disabled": [445, "junk", 99999],
        })
        assert [e.port for e in cat.custom] == [8112, 1880]
        assert cat.custom[0].name == "deluge", "the first entry wins a duplicate"
        assert cat.disabled == frozenset({445})

    def test_a_disabled_port_that_is_not_a_built_in_is_dropped(self):
        # Otherwise it sits there suppressing nothing, and switches a port off
        # by surprise if that number is ever added to WELL_KNOWN.
        assert 8112 not in P.WELL_KNOWN
        assert PC.from_dict({"disabled": [8112]}).disabled == frozenset()

    @pytest.mark.parametrize("raw", [None, {}, [], "nonsense", 3])
    def test_nothing_usable_means_the_defaults(self, raw):
        assert PC.from_dict(raw) == PC.Catalogue()


# --------------------------------------------------------------------------
# What it costs
# --------------------------------------------------------------------------


class TestCostEstimate:
    @pytest.mark.parametrize("ports,devices,measured", [
        (45, 1, 4),
        (85, 1, 8),
        (45, 10, 4),
        (85, 10, 8),
        (45, 67, 12),
        (85, 67, 23),
    ])
    def test_it_matches_what_real_sockets_did(self, ports, devices, measured):
        """Checked against a run over black-holed addresses, where every probe
        pays the full timeout. Within a second, because the measurement has
        scheduling noise in it and the estimate is deliberately whole seconds.
        """
        assert abs(PC.worst_case_seconds(ports, devices) - measured) <= 1

    def test_removing_ports_buys_time_back(self):
        # The only claim the Settings page makes that is worth checking: that
        # unticking built-ins makes the scan faster, not just shorter to read.
        before = PC.worst_case_seconds(45, 200)
        after = PC.worst_case_seconds(41, 200)
        assert after < before

    def test_nothing_to_scan_costs_nothing(self):
        assert PC.worst_case_seconds(45, 0) == 0
        assert PC.worst_case_seconds(0, 45) == 0


# --------------------------------------------------------------------------
# The bug this feature would otherwise have introduced
# --------------------------------------------------------------------------


class TestUnscannedPortsAreNotClosed:
    """A port leaving the scan is not evidence the service stopped."""

    def _service_state(self, port: int) -> str:
        async def go():
            async with D.session_scope() as s:
                row = (await s.execute(
                    Service.__table__.select().where(Service.port == port)
                )).first()
                return row.state if row else None
        return asyncio.run(go())

    def test_a_port_no_longer_scanned_keeps_its_last_known_state(self, db):
        async def go():
            async with D.session_scope() as s:
                # Found on 445 and 22 by a scan of the full list...
                await record_scan(s, P.HostScan(
                    address="10.1.10.50", device_id=1, probed=len(P.WELL_KNOWN),
                    covered=frozenset(P.WELL_KNOWN),
                    open_ports=[P.OpenPort(445, "smb"), P.OpenPort(22, "ssh")],
                ))
            async with D.session_scope() as s:
                # ...then 445 is switched off, so the next scan never looks.
                await record_scan(s, P.HostScan(
                    address="10.1.10.50", device_id=1,
                    probed=len(P.WELL_KNOWN) - 1,
                    covered=frozenset(P.WELL_KNOWN) - {445},
                    open_ports=[P.OpenPort(22, "ssh")],
                ))
        asyncio.run(go())
        assert self._service_state(445) == OPEN, (
            "unticking a port marked a live service closed without probing it"
        )
        assert self._service_state(22) == OPEN

    def test_a_port_still_scanned_and_not_found_does_close(self, db):
        # The other half: the close path still has to work, or this "fix"
        # would just be a way of never closing anything.
        async def go():
            async with D.session_scope() as s:
                await record_scan(s, P.HostScan(
                    address="10.1.10.50", device_id=1, probed=len(P.WELL_KNOWN),
                    covered=frozenset(P.WELL_KNOWN),
                    open_ports=[P.OpenPort(445, "smb")],
                ))
            async with D.session_scope() as s:
                await record_scan(s, P.HostScan(
                    address="10.1.10.50", device_id=1, probed=len(P.WELL_KNOWN),
                    covered=frozenset(P.WELL_KNOWN),
                    open_ports=[],
                ))
        asyncio.run(go())
        assert self._service_state(445) == CLOSED

    def test_a_scan_that_covered_nothing_closes_nothing(self, db):
        # scan_host returns this for an address it could not use. It must not
        # read as "every service on this device is gone".
        async def go():
            async with D.session_scope() as s:
                await record_scan(s, P.HostScan(
                    address="10.1.10.50", device_id=1, probed=len(P.WELL_KNOWN),
                    covered=frozenset(P.WELL_KNOWN),
                    open_ports=[P.OpenPort(22, "ssh")],
                ))
            async with D.session_scope() as s:
                await record_scan(s, P.HostScan(address="", device_id=1))
        asyncio.run(go())
        assert self._service_state(22) == OPEN

    def test_an_intercepted_port_does_not_close_what_was_found_before(self, db):
        # The same bug by the other route, and it was already reachable: the
        # control probe removes intercepted ports from the scan list.
        async def go():
            async with D.session_scope() as s:
                await record_scan(s, P.HostScan(
                    address="10.1.10.50", device_id=1, probed=len(P.WELL_KNOWN),
                    covered=frozenset(P.WELL_KNOWN),
                    open_ports=[P.OpenPort(53, "dns")],
                ))
            async with D.session_scope() as s:
                await record_scan(s, P.HostScan(
                    address="10.1.10.50", device_id=1,
                    probed=len(P.WELL_KNOWN) - 1,
                    covered=frozenset(P.WELL_KNOWN) - {53},
                    open_ports=[],
                ))
        asyncio.run(go())
        assert self._service_state(53) == OPEN

    def test_a_service_from_another_source_is_still_never_closed(self, db):
        # Unchanged by any of this, and worth pinning: a scan is not evidence
        # about a service a container inventory told us about.
        async def go():
            async with D.session_scope() as s:
                s.add(Service(device_id=1, port=5432, protocol="tcp",
                              name="postgres", source=ServiceSource.DOCKER,
                              state=OPEN, first_seen=utcnow(), last_seen=utcnow()))
            async with D.session_scope() as s:
                await record_scan(s, P.HostScan(
                    address="10.1.10.50", device_id=1, probed=len(P.WELL_KNOWN),
                    covered=frozenset(P.WELL_KNOWN), open_ports=[],
                ))
        asyncio.run(go())
        assert self._service_state(5432) == OPEN


# --------------------------------------------------------------------------
# Naming
# --------------------------------------------------------------------------


class TestScanUsesTheCatalogueNames:
    def test_a_custom_name_reaches_the_recorded_service(self):
        scan = asyncio.run(P.scan_host(
            "127.0.0.1", [1], names={1: "my thing"}, timeout=0.05
        ))
        # Nothing is listening on port 1, so this asserts on the covered set
        # rather than on a result -- the naming path is exercised below.
        assert scan.covered == frozenset({1})

    def test_names_override_the_built_in_catalogue(self):
        got = asyncio.run(_named_scan({3000: "my app"}))
        assert got == "my app"

    def test_without_an_override_the_built_in_name_is_used(self):
        assert asyncio.run(_named_scan(None)) == "grafana"


async def _named_scan(names):
    """Scan a real listening socket on an ephemeral port pretending to be 3000."""
    import socket

    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(4)
    port = server.getsockname()[1]
    try:
        # Remap the catalogue onto the port we actually got, so this exercises
        # the real lookup rather than a stub.
        mapped = None if names is None else {port: names[3000]}
        monkey = dict(P.WELL_KNOWN)
        monkey[port] = "grafana"
        original = P.WELL_KNOWN.copy()
        P.WELL_KNOWN.clear()
        P.WELL_KNOWN.update(monkey)
        try:
            scan = await P.scan_host("127.0.0.1", [port], names=mapped, timeout=1.0)
        finally:
            P.WELL_KNOWN.clear()
            P.WELL_KNOWN.update(original)
        assert scan.open_ports, "the test socket did not answer"
        return scan.open_ports[0].name
    finally:
        server.close()


# --------------------------------------------------------------------------
# The Settings page
# --------------------------------------------------------------------------


def custom_rows(page: str) -> list[str]:
    table = re.search(r'<table class="table compact port-table".*?</table>', page, re.S)
    return re.findall(r"<code>(\d+)</code>", table.group(0)) if table else []


def budget(page: str) -> str:
    match = re.search(r'<p class="muted small port-budget">(.*?)</p>', page, re.S)
    return " ".join(re.sub(r"<[^>]+>", "", match.group(1)).split()) if match else ""


class TestSettingsPage:
    def test_a_port_can_be_added_and_named(self, client):
        client.post("/settings/ports", data={"port": "8112", "name": "deluge"})
        page = client.get("/settings/ports").text
        assert "8112" in custom_rows(page)
        assert "deluge" in page

    def test_a_port_can_be_added_without_a_name(self, client):
        client.post("/settings/ports", data={"port": "1880", "name": ""})
        assert "1880" in custom_rows(client.get("/settings/ports").text)

    def test_a_port_can_be_removed(self, client):
        client.post("/settings/ports", data={"port": "8112", "name": "deluge"})
        client.post("/settings/ports/8112/delete")
        assert custom_rows(client.get("/settings/ports").text) == []

    def test_a_bad_port_comes_back_with_the_typing_intact(self, client):
        response = client.post("/settings/ports",
                               data={"port": "70000", "name": "nope"})
        assert response.status_code == 400
        # Clearing the form on a validation error is a worse outcome than the
        # typo was.
        assert "70000" in response.text and "nope" in response.text
        assert custom_rows(response.text) == []

    def test_a_duplicate_is_refused_and_says_so(self, client):
        client.post("/settings/ports", data={"port": "8112"})
        response = client.post("/settings/ports", data={"port": "8112"})
        assert response.status_code == 400
        assert "already on the list" in response.text
        assert custom_rows(response.text) == ["8112"], "not added twice"

    def test_the_limit_is_enforced(self, client):
        for n in range(PC.MAX_CUSTOM):
            assert client.post("/settings/ports",
                               data={"port": str(20000 + n)}).status_code == 303
        response = client.post("/settings/ports", data={"port": "20999"})
        assert response.status_code == 400
        assert str(PC.MAX_CUSTOM) in response.text

    def test_built_ins_can_be_switched_off(self, client):
        keep = [str(p) for p in sorted(P.WELL_KNOWN) if p not in (135, 139, 445, 3389)]
        # A dict of a list, not a list of pairs: httpx reads the latter as a
        # raw body and posts no fields at all, which looks exactly like
        # unticking everything and made this test pass for the wrong reason.
        client.post("/settings/ports/builtins", data={"keep": keep})
        page = client.get("/settings/ports").text
        assert f"{len(P.WELL_KNOWN) - 4} of {len(P.WELL_KNOWN)} built-in" in budget(page)

    def test_switching_them_all_off_is_allowed_and_says_so(self, client):
        # Perverse but legitimate -- it is how you scan only your own ports.
        client.post("/settings/ports", data={"port": "8112"})
        client.post("/settings/ports/builtins", data={})
        page = client.get("/settings/ports").text
        assert "Scanning 1 port(s)" in budget(page)

    def test_the_budget_counts_the_merged_list_not_the_two_lists(self, client):
        # A custom entry on a built-in's port renames it rather than adding to
        # the scan, so the total must come from the merged set.
        client.post("/settings/ports", data={"port": "3000", "name": "my app"})
        assert f"Scanning {len(P.WELL_KNOWN)} port(s)" in budget(client.get("/settings/ports").text)

    def test_the_budget_mentions_the_devices_it_is_based_on(self, client):
        assert "1 device(s)" in budget(client.get("/settings/ports").text)

    def test_the_checkboxes_are_not_disabled(self, client):
        # An unticked box submits nothing, and a disabled one submits nothing
        # either -- so a disabled box reads to the server as "switch this off".
        page = client.get("/settings/ports").text
        grid = re.search(r'<div class="port-grid">.*?</div>', page, re.S)
        assert grid and "disabled" not in grid.group(0)

    def test_the_page_needs_a_login(self, client):
        client.post("/logout")
        assert client.post("/settings/ports", data={"port": "8112"}).status_code == 303
        assert custom_rows(client.get("/settings/ports").text) == []


# --------------------------------------------------------------------------
# The scan actually using it
# --------------------------------------------------------------------------


class TestTheScanReadsTheCatalogue:
    """The join everything else depends on, and the one nothing was checking.

    Every test above this point passed with `run_port_scan` hard-coded to
    `WELL_KNOWN` -- a mutation check proved it. The catalogue could have been
    editable, storable, mergeable and entirely ignored by the thing it exists
    to configure, and the page would have looked right while the scan carried
    on scanning the defaults.
    """

    def _run_and_summarise(self, cfg) -> dict:
        from spark.discovery.runner import PORT_SCAN_STATE_KEY, run_port_scan

        async def go():
            await run_port_scan(cfg)
            async with D.session_scope() as s:
                return await D.get_setting(s, PORT_SCAN_STATE_KEY)

        return asyncio.run(go())

    def test_a_custom_port_is_added_to_the_scan(self, db):
        async def add():
            async with D.session_scope() as s:
                await PC.save(s, PC.Catalogue(custom=(PC.CustomPort(8112, "deluge"),)))
        asyncio.run(add())

        summary = self._run_and_summarise(db)
        assert summary["ports_per_device"] == len(P.WELL_KNOWN) + 1
        assert summary["custom_ports"] == 1

    def test_a_disabled_built_in_is_left_out_of_the_scan(self, db):
        async def disable():
            async with D.session_scope() as s:
                await PC.save(s, PC.Catalogue(disabled=frozenset({135, 139, 445, 3389})))
        asyncio.run(disable())

        summary = self._run_and_summarise(db)
        assert summary["ports_per_device"] == len(P.WELL_KNOWN) - 4
        assert summary["disabled_builtins"] == 4

    def test_a_custom_port_is_really_probed_and_recorded_under_its_name(self, db):
        """End to end against a real socket, not a count.

        A port can be in the list and never reach the socket -- the name map
        and the port list are passed separately, and a scan that probed the
        right port and recorded it as a bare number would still satisfy the
        counts above.
        """
        import socket

        server = socket.socket()
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(4)
        port = server.getsockname()[1]

        async def prepare():
            async with D.session_scope() as s:
                device = await s.get(Device, 1)
                device.primary_ip = "127.0.0.1"
                await PC.save(s, PC.Catalogue(
                    custom=(PC.CustomPort(port, "my thing"),),
                    # Everything else off, so this asserts on the one port and
                    # takes no time doing it.
                    disabled=frozenset(P.WELL_KNOWN),
                ))
        try:
            asyncio.run(prepare())
            summary = self._run_and_summarise(db)
            assert summary["ports_per_device"] == 1

            async def recorded():
                async with D.session_scope() as s:
                    row = (await s.execute(
                        Service.__table__.select().where(Service.port == port)
                    )).first()
                    return (row.state, row.name) if row else None

            assert asyncio.run(recorded()) == (OPEN, "my thing")
        finally:
            server.close()


class TestTheControlProbeCoversCustomPorts:
    """A port you added is a port the network can answer for on your behalf.

    The control probe exists because a TCP connect scan cannot tell a service
    apart from a firewall answering for every address. That reasoning does not
    stop at the built-in list -- 8080 redirected to a captive portal is the
    same trap as 80 -- but the probe took no port list, so it checked the
    defaults while the scan checked the catalogue. Nothing failed; a custom
    port was simply never eligible to be caught.
    """

    def test_an_intercepted_custom_port_is_excluded(self):
        import socket

        tmp = Path(tempfile.mkdtemp(prefix="spark-intercept-"))
        cfg = Config.model_validate({
            "app": {"data_dir": str(tmp / "data"), "port": 9706, "log_level": "WARNING"},
            # Loopback as the subnet, so the "empty" control addresses are
            # 127.0.0.x -- a listener on 0.0.0.0 answers on all of them, which
            # is a faithful model of a redirect rather than a mock.
            "network": {"subnets": [{"name": "Loop", "cidr": "127.0.0.0/24",
                                     "attached": True}]},
        })
        cfg.app.data_dir.mkdir(parents=True, exist_ok=True)

        server = socket.socket()
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("0.0.0.0", 0))
        server.listen(8)
        port = server.getsockname()[1]

        from spark.discovery.runner import PORT_SCAN_STATE_KEY, run_port_scan

        async def go():
            D.init_engine(cfg)
            await D.init_db(cfg)
            async with D.session_scope() as s:
                # Seeded explicitly: the app does this at startup, and the
                # control probe has nowhere to look without it.
                await subnet_service.seed_from_config(s, cfg)
            async with D.session_scope() as s:
                s.add(Device(mac="aa:bb:cc:00:00:01", primary_ip="127.0.0.1",
                             friendly_name="lab", last_seen=utcnow()))
                await PC.save(s, PC.Catalogue(
                    custom=(PC.CustomPort(port, "mine"),),
                    disabled=frozenset(P.WELL_KNOWN),
                ))
            await run_port_scan(cfg)
            async with D.session_scope() as s:
                summary = await D.get_setting(s, PORT_SCAN_STATE_KEY)
                row = (await s.execute(
                    Service.__table__.select().where(Service.port == port)
                )).first()
            await D.close_engine()
            return summary, row

        try:
            summary, row = asyncio.run(go())
        finally:
            server.close()

        assert summary["controls"] >= PC_MIN_CONTROLS, "not enough controls to judge"
        assert port in summary["intercepted"], (
            "a custom port answering on empty addresses was not caught"
        )
        assert summary["ports_per_device"] == 0, "it should have been excluded"
        assert row is None, "nothing should be recorded for a port nothing is on"


PC_MIN_CONTROLS = P.MIN_CONTROLS
