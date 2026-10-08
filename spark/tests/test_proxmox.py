"""Proxmox VE over its API: the pinned HTTPS client, what it reads, the
alerts, and the Proxmox card on the device page.

Against a real TLS HTTP server (a self-signed certificate made per test
run), as for TrueNAS, because the property that matters most can only be
tested for real: the token is never sent until a person has trusted the
certificate, and never to a different one. The answers follow the shapes
in the Proxmox API definitions (pve-manager, pve-storage), names made up.
"""

from __future__ import annotations

import asyncio
import copy
import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from spark import credentials, proxmox, proxmox_health, suppressions
from spark import db as D
from spark.main import create_app
from spark.models import AlertState, ApiCredential, Device
from spark.vault import vault_for

from test_credentials import PASSWORD, _certificate, _config, _sent, _Switch, flat, run

GOOD = "spark@pve!monitor=0b8e3c2a-1111-2222-3333-444455556666"
DENIED = object()


def _nodes() -> list[dict]:
    return [{"node": "pve1", "status": "online", "type": "node", "id": "node/pve1",
             "cpu": 0.0712, "maxcpu": 24, "mem": 30_000_000_000, "maxmem": 64_000_000_000,
             "disk": 9_000_000_000, "maxdisk": 100_000_000_000, "uptime": 1_036_800,
             "level": "", "ssl_fingerprint": "AA:BB"}]


def _guests() -> list[dict]:
    def g(vmid, name, kind, status, **over):  # type: ignore[no-untyped-def]
        return {"id": f"{kind}/{vmid}", "type": kind, "vmid": vmid, "name": name, "node": "pve1",
                "status": status, "template": 0, "cpu": 0.05 if status == "running" else 0,
                "maxcpu": 4, "mem": 2_000_000_000 if status == "running" else 0,
                "maxmem": 8_000_000_000, "uptime": 86_400 if status == "running" else 0,
                "disk": 0, "maxdisk": 32_000_000_000, "diskread": 1, "diskwrite": 1,
                "netin": 1, "netout": 1, **over}
    return [g(101, "web", "qemu", "running"), g(102, "media", "qemu", "running"),
            g(200, "dns", "lxc", "running"), g(103, "sandbox", "qemu", "stopped"),
            g(900, "debian-template", "qemu", "stopped", template=1)]


def _status() -> dict:
    return {"uptime": 1_036_800, "loadavg": ["0.52", "0.61", "0.70"], "cpu": 0.0712, "wait": 0,
            "memory": {"total": 64_000_000_000, "used": 31_000_000_000, "free": 33_000_000_000,
                       "available": 40_000_000_000},
            "swap": {"total": 0, "used": 0, "free": 0},
            "rootfs": {"total": 100_000_000_000, "used": 9_000_000_000, "avail": 91_000_000_000,
                       "free": 91_000_000_000},
            "cpuinfo": {"model": "Example CPU @ 3.00GHz", "cpus": 24, "cores": 24, "sockets": 1},
            "pveversion": "pve-manager/9.1.8/0123456789abcdef",
            "current-kernel": {"sysname": "Linux", "release": "6.14.8-2-pve", "machine": "x86_64"},
            "boot-info": {"mode": "efi"}}


def _storage(**over) -> list[dict]:  # type: ignore[no-untyped-def]
    rows = [
        {"storage": "local", "type": "dir", "content": "iso,vztmpl,backup", "active": 1,
         "enabled": 1, "shared": 0, "total": 100_000_000_000, "used": 9_000_000_000,
         "avail": 91_000_000_000, "used_fraction": 0.09},
        {"storage": "tank", "type": "zfspool", "content": "images,rootdir", "active": 1,
         "enabled": 1, "shared": 0, "total": 4_000_000_000_000, "used": 1_000_000_000_000,
         "avail": 3_000_000_000_000, "used_fraction": 0.25},
        {"storage": "backups", "type": "nfs", "content": "backup", "active": 1, "enabled": 1,
         "shared": 1, "total": 8_000_000_000_000, "used": 2_000_000_000_000,
         "avail": 6_000_000_000_000, "used_fraction": 0.25},
    ]
    for row in rows:
        row.update(over.get(row["storage"], {}))
    return rows


def _zfs(health: str = "ONLINE") -> list[dict]:
    return [{"name": "tank", "size": 4_000_000_000_000, "alloc": 1_000_000_000_000,
             "free": 3_000_000_000_000, "frag": 3, "dedup": 1.0, "health": health}]


def _disks(**health) -> list[dict]:  # type: ignore[no-untyped-def]
    rows = [
        {"devpath": "/dev/nvme0n1", "type": "nvme", "model": "Example NVMe 1TB", "serial": "SECRET-1",
         "size": 1_000_204_886_016, "health": "PASSED", "wearout": 97, "used": "LVM", "gpt": 1,
         "mounted": 0, "vendor": "unknown", "wwn": "unknown", "osdid": -1, "osdid-list": None,
         "rpm": 0, "by_id_link": "/dev/disk/by-id/nvme-x"},
        {"devpath": "/dev/sda", "type": "hdd", "model": "Example HDD 4TB", "serial": "SECRET-2",
         "size": 4_000_787_030_016, "health": "PASSED", "wearout": "N/A", "used": "ZFS", "gpt": 1,
         "mounted": 0, "rpm": 7200},
        {"devpath": "/dev/sdb", "type": "usb", "model": "Stick", "serial": "SECRET-3",
         "size": 32_000_000_000, "health": "UNKNOWN", "wearout": "N/A", "used": "partitions",
         "gpt": 0, "mounted": 0, "rpm": -1},
    ]
    for row in rows:
        if row["devpath"] in health:
            row["health"] = health[row["devpath"]]
    return rows


def real() -> dict:
    return {
        "/version": {"version": "9.1.8", "release": "9.1", "repoid": "0123456789abcdef"},
        "/nodes": _nodes(),
        "/cluster/resources?type=vm": _guests(),
        "/nodes/pve1/status": _status(),
        "/nodes/pve1/storage?enabled=1": _storage(),
        "/nodes/pve1/disks/zfs": _zfs(),
        "/nodes/pve1/disks/list": _disks(),
    }


class FakeProxmox:
    """A TLS HTTP/1.1 server on 127.0.0.1 answering like pveproxy."""

    def __init__(self) -> None:
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="spark-pve-"))
        self.requests: list[dict] = []
        self.raw: list[bytes] = []
        self.answers: dict = {}
        self.chunked = False
        self.use("first")

    def use(self, name: str) -> None:
        import ssl
        cert, key, self.fingerprint = _certificate(self.tmp, name)
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(cert, key)

    async def _handle(self, reader, writer):  # type: ignore[no-untyped-def]
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            writer.close()
            return
        self.raw.append(head)
        lines = head.decode().split("\r\n")
        method, path, _ = lines[0].split(" ", 2)
        headers = {k.strip().lower(): v.strip() for k, _, v in
                   (line.partition(":") for line in lines[1:] if line)}
        self.requests.append({"method": method, "path": path, "headers": headers})
        path = path.removeprefix("/api2/json")
        if headers.get("authorization") != f"PVEAPIToken={GOOD}":
            status, body = "401 authentication failure", {"data": None}
        elif path in self.answers and self.answers[path] is DENIED:
            status, body = "403 Permission check failed (/, Sys.Audit)", {"data": None}
        elif path in self.answers:
            status, body = "200 OK", {"data": self.answers[path]}
        else:
            status, body = "501 Method not implemented", {"data": None}
        data = json.dumps(body).encode()
        if self.chunked:
            parts = [data[i:i + 100] for i in range(0, len(data), 100)]
            payload = b"".join(b"%x\r\n%s\r\n" % (len(p), p) for p in parts) + b"0\r\n\r\n"
            writer.write(f"HTTP/1.1 {status}\r\nTransfer-Encoding: chunked\r\n"
                         "Content-Type: application/json\r\n\r\n".encode() + payload)
        else:
            writer.write(f"HTTP/1.1 {status}\r\nContent-Length: {len(data)}\r\n"
                         "Content-Type: application/json\r\nConnection: close\r\n\r\n".encode() + data)
        await writer.drain()
        writer.close()

    def start(self) -> None:
        ready = threading.Event()

        def serve_forever() -> None:
            async def main() -> None:
                server = await asyncio.start_server(self._handle, "127.0.0.1", 0, ssl=_Switch(self))
                self.port = server.sockets[0].getsockname()[1]
                ready.set()
                async with server:
                    await server.serve_forever()
            asyncio.run(main())
        threading.Thread(target=serve_forever, daemon=True).start()
        ready.wait(5)

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.port}"

    def tokens(self) -> list[str]:
        return [r["headers"].get("authorization", "") for r in self.requests]


@pytest.fixture(scope="module")
def fake():
    server = FakeProxmox()
    server.start()
    return server


@pytest.fixture(autouse=True)
def _fresh(fake):
    fake.requests.clear()
    fake.raw.clear()
    fake.chunked = False
    fake.use("first")
    fake.answers = real()


# --------------------------------------------------------------------------
# The client
# --------------------------------------------------------------------------


class TestToken:
    @pytest.mark.parametrize("raw", [GOOD, f"  {GOOD} ", f"PVEAPIToken={GOOD}",
                                     "root@pam!x.y_z-1=abc"])
    def test_good(self, raw):
        assert proxmox.check_token(raw) in (GOOD, "root@pam!x.y_z-1=abc")

    @pytest.mark.parametrize("raw", ["", "0b8e3c2a-1111-2222-3333-444455556666", "spark@pve=abc",
                                     "spark@pve!monitor", "spark!monitor=abc", "spark@pve!monitor=a b"])
    def test_refused(self, raw):
        with pytest.raises(ValueError, match="USER@REALM!TOKENID=SECRET"):
            proxmox.check_token(raw)


TOKEN_ID, SECRET = GOOD.split("=")


class TestTwoFields:
    """The Token ID and Secret, as Proxmox shows them when a token is made."""

    def test_joined(self):
        assert proxmox.join_token(TOKEN_ID, SECRET) == GOOD
        assert proxmox.join_token(f" {TOKEN_ID} ", f" {SECRET}\n") == GOOD
        assert proxmox.split_token(GOOD) == (TOKEN_ID, SECRET)

    def test_a_whole_token_in_the_secret_field_is_fine(self):
        assert proxmox.join_token("", GOOD) == GOOD
        assert proxmox.join_token(TOKEN_ID, GOOD) == GOOD
        with pytest.raises(ValueError, match="different token ID"):
            proxmox.join_token("root@pam!other", GOOD)

    @pytest.mark.parametrize("token_id, secret, why", [
        ("", SECRET, "Enter the token ID as well"),
        ("spark@pve", SECRET, "is not a token ID"),
        ("monitor", SECRET, "is not a token ID"),
        (TOKEN_ID, "", "Paste the token's secret"),
        (TOKEN_ID, "a b", "not a token secret"),
    ])
    def test_refused(self, token_id, secret, why):
        with pytest.raises(ValueError, match=why):
            proxmox.join_token(token_id, secret)


class TestHosts:
    def test_default_port_8006(self):
        assert proxmox.split_host("192.168.1.30") == ("192.168.1.30", 8006)
        assert proxmox.split_host("pve.lan:443") == ("pve.lan", 443)
        assert proxmox.split_host("[fe80::1]") == ("fe80::1", 8006)
        assert proxmox.split_host("[fe80::1]:8443") == ("fe80::1", 8443)
        assert proxmox.url_for("pve.lan", "/version") == "https://pve.lan:8006/api2/json/version"

    def test_only_ever_https(self):
        source = Path(proxmox.__file__).read_text()
        assert "http://" not in source, "no plain-HTTP path at all"


class TestClientPinning:
    def test_no_pin_means_nothing_is_sent(self, fake):
        with pytest.raises(proxmox.CertificateNotTrusted) as caught:
            run(proxmox.test(fake.host, GOOD, None))
        assert caught.value.fingerprint == fake.fingerprint and not caught.value.changed
        assert fake.requests == [] and fake.raw == [], "not the token, not anything"

    def test_the_pinned_certificate_reads_everything(self, fake):
        info = run(proxmox.test(fake.host, GOOD, fake.fingerprint))
        assert (info.version, info.hostname) == ("9.1.8", "pve1")
        assert [r["path"] for r in fake.requests] == [
            "/api2/json/version", "/api2/json/nodes", "/api2/json/cluster/resources?type=vm",
            "/api2/json/nodes/pve1/status", "/api2/json/nodes/pve1/storage?enabled=1",
            "/api2/json/nodes/pve1/disks/zfs", "/api2/json/nodes/pve1/disks/list"]
        assert set(fake.tokens()) == {f"PVEAPIToken={GOOD}"}
        assert all(r["method"] == "GET" for r in fake.requests), "read only"
        assert fake.requests[0]["headers"]["host"] == fake.host

    def test_a_different_certificate_gets_nothing(self, fake):
        trusted = fake.fingerprint
        fake.use("impostor")
        with pytest.raises(proxmox.CertificateNotTrusted) as caught:
            run(proxmox.test(fake.host, GOOD, trusted))
        assert caught.value.changed and caught.value.fingerprint == fake.fingerprint
        assert fake.requests == []

    def test_a_refused_token(self, fake):
        with pytest.raises(proxmox.LoginFailed, match="refused the token"):
            run(proxmox.test(fake.host, "spark@pve!monitor=wrong", fake.fingerprint))

    def test_nothing_listening(self):
        with pytest.raises(proxmox.ProxmoxError, match="Could not open the Proxmox API"):
            run(proxmox.test("127.0.0.1:9", GOOD, None))

    def test_a_part_it_may_not_read_is_left_out(self, fake):
        fake.answers["/nodes/pve1/disks/list"] = DENIED
        fake.answers["/cluster/resources?type=vm"] = DENIED
        info = run(proxmox.test(fake.host, GOOD, fake.fingerprint))
        assert info.readings["disks"] is None and info.readings["guests"] is None
        assert info.readings["zfs"][0]["name"] == "tank"

    def test_chunked_answers(self, fake):
        fake.chunked = True
        info = run(proxmox.test(fake.host, GOOD, fake.fingerprint))
        assert len(info.readings["guests"]) == 4


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def _parse(**over):  # type: ignore[no-untyped-def]
    a = real()
    args = {"nodes": a["/nodes"], "guests": a["/cluster/resources?type=vm"],
            "status": {"pve1": a["/nodes/pve1/status"]},
            "storage": {"pve1": a["/nodes/pve1/storage?enabled=1"]},
            "zfs": {"pve1": a["/nodes/pve1/disks/zfs"]}, "disks": {"pve1": a["/nodes/pve1/disks/list"]}}
    args.update(over)
    return proxmox.parse(**args)


class TestParse:
    def test_the_shape(self):
        out = _parse()
        assert out["nodes"] == [{
            "name": "pve1", "status": "online", "cpu": 0.0712, "cores": 24,
            "mem": 31_000_000_000, "maxmem": 64_000_000_000, "uptime": 1_036_800,
            "version": "9.1.8", "kernel": "6.14.8-2-pve", "cpu_model": "Example CPU @ 3.00GHz",
            "load": [0.52, 0.61, 0.7]}]
        assert [(g["vmid"], g["name"], g["kind"], g["status"]) for g in out["guests"]] == [
            (101, "web", "VM", "running"), (102, "media", "VM", "running"),
            (103, "sandbox", "VM", "stopped"), (200, "dns", "CT", "running")], "no templates"
        assert out["storages"][1] == {
            "node": "pve1", "name": "tank", "type": "zfspool", "content": "images,rootdir",
            "active": True, "shared": False, "total": 4_000_000_000_000,
            "used": 1_000_000_000_000, "pct": 25.0}
        assert out["zfs"] == [{"node": "pve1", "name": "tank", "health": "ONLINE",
                               "size": 4_000_000_000_000, "alloc": 1_000_000_000_000,
                               "free": 3_000_000_000_000, "frag": 3, "pct": 25.0}]
        disks = {d["devpath"]: d for d in out["disks"]}
        assert disks["/dev/nvme0n1"]["wearout"] == 97 and disks["/dev/sda"]["wearout"] is None
        assert disks["/dev/sdb"]["health"] == "UNKNOWN"
        assert "SECRET" not in repr(out), "serials are not kept"
        assert out["answered"] == {"storages": ["pve1"], "zfs": ["pve1"], "disks": ["pve1"]}

    def test_unanswered_parts_are_none_not_empty(self):
        out = proxmox.parse(nodes=None, guests=None, status={}, storage={}, zfs={}, disks={})
        assert all(out[k] is None for k in ("nodes", "guests", "storages", "zfs", "disks"))
        out = _parse(zfs={"pve1": []}, disks={"pve1": None})
        assert out["zfs"] == [] and out["disks"] is None

    def test_odd_values(self):
        out = _parse(guests=[None, {"type": "qemu"}, {"type": "storage", "vmid": 1},
                             {"type": "lxc", "vmid": 5, "cpu": True, "mem": -1}],
                     disks={"pve1": [{"devpath": "/dev/sdz", "wearout": "12"}, {"x": 1}]})
        assert out["guests"] == [{"vmid": 5, "name": "5", "kind": "CT", "node": None,
                                  "status": "unknown", "cpu": None, "cores": None, "mem": None,
                                  "maxmem": None, "uptime": None}]
        assert out["disks"] == [{"node": "pve1", "devpath": "/dev/sdz", "model": None, "type": None,
                                 "size": None, "health": "UNKNOWN", "wearout": 12, "used": None}]


# --------------------------------------------------------------------------
# The credential, the checks, and the alerts
# --------------------------------------------------------------------------


@pytest.fixture
def db():
    config = _config()

    async def setup():
        D.init_engine(config)
        await D.init_db(config)
        async with D.session_scope() as s:
            s.add(Device(mac="aa:00:00:00:00:01", primary_ip="192.168.1.30", friendly_name="hv"))
    run(setup())
    yield config
    run(D.close_engine())


async def _trusted(config, host, **over):  # type: ignore[no-untyped-def]
    async with D.session_scope() as s:
        args = {"kind": "proxmox", "name": "pve", "device_id": 1, "host": host, "api_key": GOOD,
                **over}
        ident = (await credentials.add(s, vault_for(config), **args)).id
    async with D.session_scope() as s:
        row = await s.get(ApiCredential, ident)
        await credentials.test(s, vault_for(config), row)
    async with D.session_scope() as s:
        row = await s.get(ApiCredential, ident)
        assert await credentials.trust(s, vault_for(config), row, row.pending_sha256)
    return ident


async def _row(ident):  # type: ignore[no-untyped-def]
    async with D.session_scope() as s:
        return await s.get(ApiCredential, ident)


def check(db, ident):  # type: ignore[no-untyped-def]
    return run(credentials.check_one(db, ident))


def watch(ident, vmid, on=True):  # type: ignore[no-untyped-def]
    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            await credentials.watch(s, await s.get(ApiCredential, ident), vmid, on)
    run(go())


def subjects():  # type: ignore[no-untyped-def]
    return [(kind, subject) for kind, subject, _ in run(_sent())]


def incidents():  # type: ignore[no-untyped-def]
    from spark.models import AlertIncident

    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            return [(r.key, r.title, r.closed_at is None, r.resolution) for r in
                    (await s.execute(select(AlertIncident).order_by(AlertIncident.id))).scalars()]
    return run(go())


def states():  # type: ignore[no-untyped-def]
    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            return sorted((await s.execute(select(AlertState.key))).scalars())
    return run(go())


class TestCredential:
    def test_a_secret_without_its_id_is_refused_on_add(self, db):
        async def go():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                await credentials.add(s, vault_for(db), kind="proxmox", name="pve", device_id=1,
                                      host="", api_key=SECRET)
        with pytest.raises(credentials.CredentialError, match="Enter the token ID as well"):
            run(go())

    def test_two_fields_are_stored_as_one_token(self, db, fake):
        ident = run(_trusted(db, fake.host, api_key=SECRET, token_id=TOKEN_ID))
        assert vault_for(db).open(run(_row(ident)).key_sealed) == GOOD
        assert credentials.state(run(_row(ident))) == "ok"

    def test_a_token_id_on_truenas_is_ignored(self, db):
        async def go():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                row = await credentials.add(s, vault_for(db), kind="truenas", name="nas",
                                            device_id=1, host="", api_key="1-key",
                                            token_id="x@y!z")
                return vault_for(db).open(row.key_sealed)
        assert run(go()) == "1-key"

    def test_add_trust_and_read(self, db, fake):
        ident = run(_trusted(db, fake.host))
        row = run(_row(ident))
        assert credentials.state(row) == "ok" and row.last_info == {"version": "9.1.8",
                                                                     "hostname": "pve1"}
        assert [g["vmid"] for g in row.readings["guests"]] == [101, 102, 103, 200]
        assert row.readings["read_at"]
        # Added and tested once without the token, then trusted and read.
        assert fake.requests[0]["path"] == "/api2/json/version"

    def test_the_token_is_sealed(self, db, fake):
        ident = run(_trusted(db, fake.host))
        row = run(_row(ident))
        assert GOOD not in row.key_sealed and vault_for(db).open(row.key_sealed) == GOOD

    def test_a_blank_key_on_edit_keeps_it_a_bad_one_is_refused(self, db, fake):
        ident = run(_trusted(db, fake.host))

        async def edit(key, token_id=""):  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                row = await s.get(ApiCredential, ident)
                await credentials.update(s, vault_for(db), row, name="pve", device_id=1,
                                         host=fake.host, api_key=key, token_id=token_id)
        run(edit(""))
        assert run(_row(ident)).cert_sha256 == fake.fingerprint
        with pytest.raises(credentials.CredentialError):
            run(edit("a b"))
        # Either half alone keeps the other from the saved token.
        run(edit("new-secret"))
        assert vault_for(db).open(run(_row(ident)).key_sealed) == f"{TOKEN_ID}=new-secret"
        run(edit("", token_id="root@pam!spark"))
        assert vault_for(db).open(run(_row(ident)).key_sealed) == "root@pam!spark=new-secret"
        with pytest.raises(credentials.CredentialError, match="is not a token ID"):
            run(edit("", token_id="nonsense"))


class TestAlerts:
    def test_nothing_fires_when_all_is_well(self, db, fake):
        ident = run(_trusted(db, fake.host))
        check(db, ident)
        assert subjects() == [] and incidents() == []

    def test_only_a_watched_guest_alerts_and_after_two_checks(self, db, fake):
        ident = run(_trusted(db, fake.host))
        check(db, ident)
        assert subjects() == [], "sandbox is stopped, but nobody is watching it"
        watch(ident, 103)
        check(db, ident)
        assert subjects() == [], "one check is not enough: it may be restarting"
        check(db, ident)
        assert subjects() == [("guest_down_bad", "hv: VM 103 (sandbox) is stopped")]
        assert incidents() == [(f"pveguest:{ident}:103", "hv: VM 103 (sandbox) is stopped", True, None)]
        fake.answers["/cluster/resources?type=vm"][3]["status"] = "running"
        check(db, ident)
        assert subjects()[-1] == ("guest_down_ok", "hv: VM 103 (sandbox) is running again")
        assert incidents()[-1][2] is False

    def test_unwatching_drops_the_alert(self, db, fake):
        ident = run(_trusted(db, fake.host))
        watch(ident, 103)
        check(db, ident)
        check(db, ident)
        assert incidents()[-1][2] is True
        watch(ident, 103, on=False)
        assert f"pveguest:{ident}:103" not in states()
        assert incidents()[-1][2:] == (False, "no longer watched")
        assert run(_row(ident)).options == {"watched": [], "labels": {}}

    def test_a_watched_guest_that_disappears(self, db, fake):
        ident = run(_trusted(db, fake.host))
        watch(ident, 101)
        check(db, ident)
        fake.answers["/cluster/resources?type=vm"] = [
            g for g in fake.answers["/cluster/resources?type=vm"] if g["vmid"] != 101]
        check(db, ident)
        check(db, ident)
        assert subjects() == [("guest_down_bad", "hv: VM 101 (web) is not listed any more")]

    def test_guests_it_could_not_read_are_not_decided(self, db, fake):
        ident = run(_trusted(db, fake.host))
        watch(ident, 101)
        fake.answers["/cluster/resources?type=vm"] = DENIED
        check(db, ident)
        check(db, ident)
        check(db, ident)
        assert subjects() == []

    def test_a_degraded_pool_a_lost_share_and_a_full_storage(self, db, fake):
        ident = run(_trusted(db, fake.host))
        fake.answers["/nodes/pve1/disks/zfs"] = _zfs("DEGRADED")
        fake.answers["/nodes/pve1/storage?enabled=1"] = _storage(
            backups={"active": 0, "total": 0, "used": 0},
            local={"used": 90_000_000_000})
        check(db, ident)
        assert subjects() == [("pool_health_bad", "hv: pool tank is DEGRADED"),
                              ("pool_health_bad", "hv: storage backups is not available")]
        check(db, ident)
        assert ("pool_space_bad", "hv: storage local is 90% full") in subjects(), "two reads"
        fake.answers.update(real())
        check(db, ident)
        assert {s for k, s in subjects() if k.endswith("_ok")} == {
            "hv: pool tank is ONLINE again", "hv: storage backups is available again",
            "hv: storage local is back to 9% full"}

    def test_smart(self, db, fake):
        ident = run(_trusted(db, fake.host))
        fake.answers["/nodes/pve1/disks/list"] = _disks(**{"/dev/sda": "FAILED"})
        check(db, ident)
        assert subjects() == [("drive_errors_bad", "hv: drive /dev/sda SMART says FAILED")]
        # UNKNOWN is not decided on either way: the alert stands.
        fake.answers["/nodes/pve1/disks/list"] = _disks(**{"/dev/sda": "UNKNOWN"})
        check(db, ident)
        assert incidents()[-1][2] is True and len(subjects()) == 1
        fake.answers["/nodes/pve1/disks/list"] = _disks()
        check(db, ident)
        assert subjects()[-1] == ("drive_errors_ok", "hv: drive /dev/sda passes SMART again")

    def test_a_node_that_did_not_answer_keeps_its_drive_alerts(self, db, fake):
        ident = run(_trusted(db, fake.host))
        fake.answers["/nodes/pve1/disks/list"] = _disks(**{"/dev/sda": "FAILED"})
        check(db, ident)
        fake.answers["/nodes/pve1/disks/list"] = DENIED
        check(db, ident)
        assert f"pvedisk:{ident}:pve1:/dev/sda" in states()
        fake.answers["/nodes/pve1/disks/list"] = [d for d in _disks() if d["devpath"] != "/dev/sda"]
        check(db, ident)
        assert f"pvedisk:{ident}:pve1:/dev/sda" not in states(), "gone from a node that answered"

    def test_rules_off_and_suppressed(self, db, fake):
        from spark import snmp_alerts
        from spark.models import AlertSuppression
        ident = run(_trusted(db, fake.host))

        async def setup():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                rules = await snmp_alerts.load(s)
                form = {k: ("1" if v is True else str(v)) for k, v in rules.items()
                        if v is not False}
                form.pop("pool_health")
                await snmp_alerts.save(s, form)
                s.add(AlertSuppression(device_id=1, rule="guest_down", threshold=None))
        run(setup())
        watch(ident, 103)
        fake.answers["/nodes/pve1/disks/zfs"] = _zfs("FAULTED")
        check(db, ident)
        check(db, ident)
        assert subjects() == [] and incidents() == []

    def test_the_suppression_rules_know_the_keys(self):
        assert suppressions.rule_of("pveguest:1:103") == "guest_down"
        assert suppressions.rule_of("pvezfs:1:pve1:tank") == "pool_health"
        assert suppressions.rule_of("pvestore:1:pve1:nfs") == "pool_health"
        assert suppressions.rule_of("pvespace:1:pve1:local") == "pool_space"
        assert suppressions.rule_of("pvedisk:1:pve1:/dev/sda") == "drive_errors"
        assert "guest_down" in suppressions.RULES

    def test_a_suppression_closes_the_standing_alert(self, db, fake):
        ident = run(_trusted(db, fake.host))
        watch(ident, 103)
        check(db, ident)
        check(db, ident)

        async def suppress():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                await suppressions.save(s, device_id=1, rule="guest_down", mode="off",
                                        threshold="", detail="")
        run(suppress())
        assert incidents()[-1][2:] == (False, "suppressed")

    def test_removing_the_credential_forgets_its_alerts(self, db, fake):
        ident = run(_trusted(db, fake.host))
        watch(ident, 103)
        check(db, ident)
        check(db, ident)
        assert any(k.startswith("pveguest:") for k in states())

        async def remove():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                await credentials.forget_alert(s, ident)
        run(remove())
        assert not any(k.startswith("pve") for k in states())


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------


@pytest.fixture
def site(db):
    with TestClient(create_app(db), follow_redirects=False) as client:
        client.post("/setup", data={"setup_code": client.app.state.setup_code, "username": "admin", "password": PASSWORD,
                                    "password_confirm": PASSWORD, "timezone": "UTC"})
        yield client


class TestPages:
    def test_add_from_settings(self, site, fake):
        response = site.post("/settings/credentials", data={
            "kind": "proxmox", "name": "pve", "device_id": "1", "host": fake.host, "api_key": GOOD})
        assert response.status_code == 303
        assert fake.requests == [], "saving tested without sending the token"
        page = flat(site.get("/settings/credentials").text)
        assert '<span class="pill neutral">Proxmox</span>' in page
        assert "the node → System → Certificates" in page and "pvenode cert info" in page
        site.post("/settings/credentials/1/trust", data={"fingerprint": fake.fingerprint})
        page = flat(site.get("/settings/credentials").text)
        assert '<span class="pill ok dot">connected</span> Proxmox 9.1.8 · pve1' in page
        assert GOOD not in page and GOOD.split("=")[1] not in page

    def test_a_missing_token_id_says_why_and_keeps_what_was_typed(self, site):
        response = site.post("/settings/credentials", data={
            "kind": "proxmox", "name": "pve", "device_id": "1", "host": "", "api_key": SECRET})
        assert response.status_code == 400 and "Enter the token ID as well" in response.text
        assert SECRET not in response.text
        response = site.post("/settings/credentials", data={
            "kind": "proxmox", "name": "pve", "device_id": "1", "host": "",
            "token_id": "spark@pve", "api_key": SECRET})
        page = flat(response.text)
        assert "is not a token ID" in page and 'value="spark@pve"' in page and SECRET not in page
        assert 'class="cred-form add-cred-form is-proxmox"' in page

    def test_the_two_fields_from_the_page(self, site, fake):
        page = flat(site.get("/settings/credentials").text)
        assert ('<label class="stack-field for-proxmox"><span class="muted small">Token ID</span>'
                in page)
        assert '<span class="for-key">API key</span><span class="for-proxmox">Secret</span>' in page
        response = site.post("/settings/credentials", data={
            "kind": "proxmox", "name": "pve", "device_id": "1", "host": fake.host,
            "token_id": TOKEN_ID, "api_key": SECRET})
        assert response.status_code == 303
        site.post("/settings/credentials/1/trust", data={"fingerprint": fake.fingerprint})
        page = flat(site.get("/settings/credentials").text)
        assert '<span class="pill ok dot">connected</span> Proxmox 9.1.8' in page
        assert '<span class="muted small">Token ID</span>' in page, "on the edit form too"
        assert SECRET not in page

    def test_the_card_and_watching(self, site, db, fake):
        fake.answers["/nodes/pve1/disks/list"] = _disks(**{"/dev/sda": "FAILED"})
        ident = run(_trusted(db, fake.host))
        page = flat(site.get("/devices/1").text)
        assert f'<section class="card pve-card" id="guests-{ident}">' in page
        assert "3 of 4 guests running" in page
        assert ('<td>pve1<div class="muted small">Example CPU @ 3.00GHz</div></td> '
                '<td><span class="pill ok">online</span></td> '
                '<td class="small">PVE 9.1.8<div class="muted small">6.14.8-2-pve</div></td> '
                '<td class="num">7% <span class="muted small">of 24</span></td> '
                '<td class="small">31.0 GB of 64.0 GB <span class="muted">(48%)</span></td>') in page
        assert ('<tr class="is-quiet"> <td class="num">103</td> <td>sandbox</td> '
                '<td class="muted small">VM</td> <td><span class="pill neutral">stopped</span></td>') in page
        assert '<td>tank</td> <td><span class="pill ok">ONLINE</span></td>' in page
        assert '<td><span class="pill bad">failed</span></td>' in page
        assert '<td><span class="pill neutral">unknown</span></td>' in page
        assert "SECRET-" not in page
        response = site.post(f"/settings/credentials/{ident}/watch",
                             data={"vmid": "103", "on": "1", "back": "/devices/1"})
        assert response.headers["location"] == f"/devices/1#guests-{ident}"
        page = flat(site.get("/devices/1").text)
        assert ('<td class="num">103</td> <td>sandbox</td> <td class="muted small">VM</td> '
                '<td><span class="pill bad">stopped</span></td>') in page
        assert 'class="btn-quiet is-watched is-toggle"' in page
        site.post(f"/settings/credentials/{ident}/watch", data={"vmid": "103", "on": "0"})
        assert run(_row(ident)).options == {"watched": [], "labels": {}}

    def test_watching_something_gone_can_be_undone(self, site, db, fake):
        ident = run(_trusted(db, fake.host))
        watch(ident, 555)
        page = flat(site.get("/devices/1").text)
        assert ('<td class="num">555</td> <td>—</td> <td colspan="5"><span class="pill bad">not listed</span>'
                in page)
        watch(ident, 101)
        check(db, ident)
        fake.answers["/cluster/resources?type=vm"] = [
            g for g in fake.answers["/cluster/resources?type=vm"] if g["vmid"] != 101]
        check(db, ident)
        page = flat(site.get("/devices/1").text)
        assert '<td class="num">101</td> <td>web</td> <td colspan="5"><span class="pill bad">not listed</span>' in page

    def test_watched_guests_are_on_the_targets_page(self, site, db, fake):
        """Watched guests alert, so Targets lists them with the rest of what
        SPARK watches -- marked as coming from the Proxmox integration, linked
        to the host's card, and with Unwatch working from there too."""
        ident = run(_trusted(db, fake.host))
        assert 'id="pve-guests"' not in site.get("/targets").text, "nothing watched, no card"
        watch(ident, 101)
        watch(ident, 103)
        watch(ident, 555)
        page = flat(site.get("/targets").text)
        assert '<section class="card pve-watch-card" id="pve-guests">' in page
        assert "Watched through the <strong>Proxmox integration</strong> (API)" in page
        assert (f'<a class="target-link" href="/devices/1#guests-{ident}" title="Open its host\'s '
                'Proxmox card"><strong>web</strong></a> <small class="muted">101</small>') in page
        assert '<span class="pill ok dot">running</span>' in page
        assert '<span class="pill bad dot">stopped</span>' in page and ">sandbox<" in page
        assert '<span class="pill bad dot">not listed</span>' in page, "watched, then gone"
        assert page.count('name="back" value="/targets"') == 3
        response = site.post(f"/settings/credentials/{ident}/watch",
                             data={"vmid": "103", "on": "0", "back": "/targets"})
        assert response.headers["location"] == "/targets#pve-guests"
        page = flat(site.get("/targets").text)
        assert ">sandbox<" not in page and ">web<" in page
        fake.use("impostor")
        check(db, ident)
        page = flat(site.get("/targets").text)
        assert "last read" in page, "the host stopped answering: the state shown is the last read"

    def test_bad_watch_posts_do_nothing(self, site, db, fake):
        ident = run(_trusted(db, fake.host))
        for vmid in ("", "x", "-1", "0"):
            site.post(f"/settings/credentials/{ident}/watch", data={"vmid": vmid, "on": "1"})
        assert run(_row(ident)).options is None
        assert site.post("/settings/credentials/99/watch", data={"vmid": "1", "on": "1"}).status_code == 303

    def test_no_card_without_readings(self, site):
        assert "pve-card" not in site.get("/devices/1").text

    def test_the_rule_is_on_the_form(self, site):
        page = flat(site.get("/alerts/rules").text)
        assert 'name="guest_down" value="1" checked' in page
        assert "<span>A drive is failing</span>" in page


def test_migration_19_from_a_version_18_database():
    from sqlalchemy import inspect, text

    async def shape(wind_back: bool):  # type: ignore[no-untyped-def]
        config = _config()
        D.init_engine(config)
        await D.init_db(config)
        if wind_back:
            async with D.session_scope() as s:
                await s.execute(text("ALTER TABLE api_credential DROP COLUMN options"))
                await D._set_version(s, 18)
            await D.close_engine()
            D.init_engine(config)
            await D.init_db(config)
        async with D.session_scope() as s:
            connection = await s.connection()
            columns = await connection.run_sync(
                lambda sync: sorted(c["name"] for c in inspect(sync).get_columns("api_credential")))
            version = await D._get_version(s)
        await D.close_engine()
        return columns, version

    assert run(shape(True)) == run(shape(False))
    assert "options" in run(shape(False))[0] and run(shape(False))[1] == D.CURRENT_VERSION


def test_smart_and_watched_helpers():
    assert proxmox_health.smart_ok({"health": "PASSED"}) is True
    assert proxmox_health.smart_ok({"health": "OK"}) is True
    assert proxmox_health.smart_ok({"health": "FAILED"}) is False
    assert proxmox_health.smart_ok({"health": "UNKNOWN"}) is None
    assert proxmox_health.smart_ok({}) is None
    row = ApiCredential(kind="proxmox", options={"watched": [103, 101, True, "x", 101]})
    assert proxmox_health.watched(row) == [101, 103]
    assert copy.copy(row.kind) == "proxmox"
