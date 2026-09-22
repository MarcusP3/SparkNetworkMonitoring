"""Tests for the port scan and the service inventory.

Two things are worth testing here and they are not the same thing.

The scanner is tested against **real sockets** — a listener opened on a free
port on loopback, and a port deliberately left closed. Mocking `open_connection`
would test that the mock was called, which is not the question; the question is
whether a TCP connect distinguishes a service from the absence of one, and only
a socket can answer that.

The store is tested for the thing that does not raise: a scan that runs every
six hours and quietly degrades the record each time. A service disappearing
because a firewall rule changed, a name from a better source being overwritten
by a port number, a closed service vanishing so you cannot see that it used to
be there — none of those fail loudly.
"""

from __future__ import annotations

import asyncio
import socket
import tempfile
from contextlib import closing
from pathlib import Path

import pytest
from sqlalchemy import func, select

from spark import db as D
from spark.config import Config
from spark.discovery import ports as P
from spark.discovery.services import CLOSED, OPEN, record_all, record_scan, services_for
from spark.models import Device, Service, ServiceSource, utcnow


def free_port() -> int:
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def make_config(tmp: Path) -> Config:
    config = Config.model_validate(
        {"app": {"data_dir": str(tmp / "data"), "port": 9705, "log_level": "WARNING"},
         "network": {"subnets": []}}
    )
    config.app.data_dir.mkdir(parents=True, exist_ok=True)
    return config


@pytest.fixture
def db():
    tmp = Path(tempfile.mkdtemp(prefix="spark-ports-"))
    cfg = make_config(tmp)

    async def setup():
        D.init_engine(cfg)
        await D.init_db(cfg)
        async with D.session_scope() as session:
            session.add(Device(mac="aa:bb:cc:dd:ee:ff", primary_ip="10.1.10.50",
                               last_seen=utcnow()))

    asyncio.run(setup())
    yield cfg
    asyncio.run(D.close_engine())


# --------------------------------------------------------------------------
# The catalogue
# --------------------------------------------------------------------------


class TestCatalogue:
    def test_it_is_a_list_not_a_range(self):
        # The whole performance argument rests on this staying small. A
        # filtered port costs a full timeout, so the list is the budget.
        assert 20 < len(P.WELL_KNOWN) < 80

    def test_every_port_is_valid_and_named(self):
        for port, name in P.WELL_KNOWN.items():
            assert 1 <= port <= 65535, port
            assert name and name.strip(), port

    def test_the_obvious_ones_are_present(self):
        for port in (22, 80, 443, 3306, 5432, 8006):
            assert port in P.WELL_KNOWN, port

    def test_noteworthy_ports_are_all_actually_scanned(self):
        # Flagging a port nothing looks for would be a warning that can never
        # fire.
        missing = set(P.NOTEWORTHY) - set(P.WELL_KNOWN)
        assert not missing, f"flagged but never scanned: {sorted(missing)}"

    def test_telnet_and_an_open_docker_socket_are_flagged(self):
        assert P.concern(23) and "clear text" in P.concern(23)
        assert P.concern(2375) and "root" in P.concern(2375)

    def test_an_ordinary_port_is_not_flagged(self):
        assert P.concern(443) is None


# --------------------------------------------------------------------------
# The scanner, against real sockets
# --------------------------------------------------------------------------


class TestProbe:
    def test_an_open_port_answers(self):
        async def go():
            server = await asyncio.start_server(
                lambda r, w: w.close(), "127.0.0.1", 0
            )
            port = server.sockets[0].getsockname()[1]
            async with server:
                return await P.probe("127.0.0.1", port, timeout=2.0)

        assert asyncio.run(go()) is True

    def test_a_closed_port_does_not(self):
        port = free_port()          # bound, then released -- nothing listening
        assert asyncio.run(P.probe("127.0.0.1", port, timeout=2.0)) is False

    def test_an_unroutable_address_is_false_rather_than_an_exception(self):
        # 192.0.2.0/24 is TEST-NET-1: reserved, never routed.
        assert asyncio.run(P.probe("192.0.2.1", 80, timeout=0.3)) is False

    def test_garbage_input_does_not_raise(self):
        # A scan runs unattended every few hours. It must fail closed.
        assert asyncio.run(P.probe("not-a-host", 80, timeout=0.3)) is False
        assert asyncio.run(P.probe("", 80, timeout=0.3)) is False


class TestScanHost:
    def test_it_finds_the_listener_and_not_the_closed_port(self):
        async def go():
            server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
            open_port = server.sockets[0].getsockname()[1]
            closed_port = free_port()
            async with server:
                return await P.scan_host(
                    "127.0.0.1", [open_port, closed_port], device_id=1, timeout=2.0
                )

        scan = asyncio.run(go())
        assert scan.probed == 2
        assert len(scan.open_ports) == 1
        assert scan.device_id == 1

    def test_results_come_back_in_port_order(self):
        async def go():
            servers = [
                await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
                for _ in range(3)
            ]
            found = [s.sockets[0].getsockname()[1] for s in servers]
            try:
                return await P.scan_host("127.0.0.1", list(reversed(found)), timeout=2.0)
            finally:
                for s in servers:
                    s.close()

        scan = asyncio.run(go())
        listed = [p.port for p in scan.open_ports]
        # Sorted output means the Devices page reads consistently rather than
        # in whatever order the event loop happened to finish.
        assert listed == sorted(listed)

    def test_an_empty_address_scans_nothing(self):
        scan = asyncio.run(P.scan_host("", [80]))
        assert scan.probed == 0 and scan.open_ports == []

    def test_known_ports_get_their_name(self):
        async def go():
            return await P.scan_host("192.0.2.1", [22], timeout=0.2)

        # Nothing answers, but the naming path is the same one used on a hit.
        asyncio.run(go())
        assert P.port_name(22) == "ssh"
        assert P.port_name(64999) is None

    def test_scanning_no_hosts_is_not_an_error(self):
        assert asyncio.run(P.scan_hosts([])) == []


# --------------------------------------------------------------------------
# Recording what was found
# --------------------------------------------------------------------------


def scan_with(*open_ports: int) -> P.HostScan:
    return P.HostScan(
        address="10.1.10.50",
        device_id=1,
        probed=len(P.WELL_KNOWN),
        open_ports=[P.OpenPort(port=p, name=P.port_name(p)) for p in open_ports],
    )


class TestRecording:
    def test_first_scan_creates_services(self, db):
        async def go():
            async with D.session_scope() as s:
                seen, new = await record_scan(s, scan_with(22, 443))
            async with D.session_scope() as s:
                rows = (await s.execute(select(Service).order_by(Service.port))).scalars().all()
                return seen, new, [(r.port, r.name, r.state) for r in rows]

        seen, new, rows = asyncio.run(go())
        assert (seen, new) == (2, 2)
        assert rows == [(22, "ssh", OPEN), (443, "https", OPEN)]

    def test_rescanning_does_not_duplicate(self, db):
        async def go():
            async with D.session_scope() as s:
                await record_scan(s, scan_with(22))
            async with D.session_scope() as s:
                seen, new = await record_scan(s, scan_with(22))
            async with D.session_scope() as s:
                total = await s.scalar(select(func.count()).select_from(Service))
            return seen, new, total

        seen, new, total = asyncio.run(go())
        # Seen every time, new only once -- "new" is what an alert would fire on.
        assert (seen, new, total) == (1, 0, 1)

    def test_a_service_that_stops_answering_is_closed_not_deleted(self, db):
        async def go():
            async with D.session_scope() as s:
                await record_scan(s, scan_with(22, 443))
            async with D.session_scope() as s:
                await record_scan(s, scan_with(22))
            async with D.session_scope() as s:
                rows = (await s.execute(select(Service).order_by(Service.port))).scalars().all()
                return [(r.port, r.state) for r in rows]

        # "This host used to run Postgres" is exactly what an inventory should
        # be able to tell you, and a DELETE cannot.
        assert asyncio.run(go()) == [(22, OPEN), (443, CLOSED)]

    def test_a_closed_service_reopens_on_a_later_scan(self, db):
        async def go():
            async with D.session_scope() as s:
                await record_scan(s, scan_with(443))
            async with D.session_scope() as s:
                await record_scan(s, scan_with())
            async with D.session_scope() as s:
                await record_scan(s, scan_with(443))
            async with D.session_scope() as s:
                row = (await s.execute(select(Service))).scalars().first()
                return row.state

        assert asyncio.run(go()) == OPEN

    def test_a_scan_never_overwrites_a_better_name(self, db):
        async def go():
            async with D.session_scope() as s:
                s.add(Service(device_id=1, port=8080, protocol="tcp",
                              name="paperless-ngx", source=ServiceSource.DOCKER,
                              state=OPEN))
            async with D.session_scope() as s:
                await record_scan(s, scan_with(8080))
            async with D.session_scope() as s:
                row = (await s.execute(select(Service))).scalars().first()
                return row.name, row.source

        name, source = asyncio.run(go())
        # A container inventory knows the name; a scan knows a port answered.
        assert name == "paperless-ngx"
        assert source is ServiceSource.DOCKER

    def test_a_scan_does_not_close_a_service_it_did_not_own(self, db):
        async def go():
            async with D.session_scope() as s:
                s.add(Service(device_id=1, port=8080, protocol="tcp",
                              name="from-docker", source=ServiceSource.DOCKER,
                              state=OPEN))
            async with D.session_scope() as s:
                await record_scan(s, scan_with(22))   # 8080 not found
            async with D.session_scope() as s:
                rows = {r.port: r.state for r in
                        (await s.execute(select(Service))).scalars().all()}
            return rows

        rows = asyncio.run(go())
        # A container is not absent just because its port was shut to us.
        assert rows[8080] == OPEN
        assert rows[22] == OPEN

    def test_a_scan_with_no_device_records_nothing(self, db):
        async def go():
            scan = P.HostScan(address="10.1.10.50", device_id=None,
                              open_ports=[P.OpenPort(port=22)])
            async with D.session_scope() as s:
                return await record_scan(s, scan)

        assert asyncio.run(go()) == (0, 0)

    def test_record_all_totals_across_devices(self, db):
        async def go():
            async with D.session_scope() as s:
                s.add(Device(mac="11:22:33:44:55:66", primary_ip="10.1.10.51",
                             last_seen=utcnow()))
            async with D.session_scope() as s:
                one = scan_with(22)
                two = P.HostScan(address="10.1.10.51", device_id=2,
                                 open_ports=[P.OpenPort(port=80), P.OpenPort(port=443)])
                return await record_all(s, [one, two])

        assert asyncio.run(go()) == (3, 3)


class TestServicesForThePage:
    def test_grouped_by_device_and_port_ordered(self, db):
        async def go():
            async with D.session_scope() as s:
                await record_scan(s, scan_with(443, 22, 8080))
            async with D.session_scope() as s:
                return {k: [x.port for x in v] for k, v in
                        (await services_for(s, [1])).items()}

        assert asyncio.run(go()) == {1: [22, 443, 8080]}

    def test_closed_and_ignored_services_are_left_out(self, db):
        async def go():
            async with D.session_scope() as s:
                await record_scan(s, scan_with(22, 443))
            async with D.session_scope() as s:
                await record_scan(s, scan_with(22))       # 443 closes
                rows = (await s.execute(select(Service).where(Service.port == 22))).scalars().all()
                rows[0].ignored = True
            async with D.session_scope() as s:
                return await services_for(s, [1])

        # Both excluded, so the device drops out of the mapping entirely.
        assert asyncio.run(go()) == {}

    def test_no_devices_means_no_query(self, db):
        assert asyncio.run(_call_services_for([])) == {}


async def _call_services_for(ids):
    async with D.session_scope() as s:
        return await services_for(s, ids)
