"""UniFi Network over its official Integration API: the pinned HTTPS client,
paging, reading both 9.x and 10.x answers, and the UniFi card.

Against a real TLS HTTP server, as for TrueNAS and Proxmox, because what
matters most can only be tested for real: the key is never sent until a
person has trusted the certificate, and never to a different one. The
answers follow the shapes in Ubiquiti's published OpenAPI documents for
UniFi Network 9.1.120 and 10.6.106 (developer.ui.com); names made up.
"""

from __future__ import annotations

import asyncio
import copy
import json
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from spark import credentials, unifi
from spark import db as D
from spark.main import create_app
from spark.models import ApiCredential, Device
from spark.vault import vault_for

from test_credentials import PASSWORD, _certificate, _config, _Switch, flat, run

KEY = "Ab3dEf6hIj9kLm2nOp5qRs8tUv1wXy4z"
SITE = "88f7af54-98f8-306a-a1c7-c9349722b1f6"
GW, SW, AP, AP2 = (f"00000000-0000-0000-0000-00000000000{i}" for i in range(1, 5))
BASE = "/proxy/network/integration"


def _devices(version: int) -> list[dict]:
    """The list: 10.x adds the firmware fields and `supported`."""
    rows = [
        {"id": GW, "name": "gateway", "model": "UDM Pro", "macAddress": "AA:00:00:00:00:01",
         "ipAddress": "192.168.1.1", "state": "ONLINE", "features": ["gateway", "switching"],
         "interfaces": ["ports"]},
        {"id": SW, "name": "office-switch", "model": "USW 24 PoE", "macAddress": "aa:00:00:00:00:02",
         "ipAddress": "192.168.1.2", "state": "ONLINE", "features": ["switching"],
         "interfaces": ["ports"]},
        {"id": AP, "name": "lounge-ap", "model": "U7 Pro", "macAddress": "aa:00:00:00:00:03",
         "ipAddress": "192.168.1.3", "state": "ONLINE", "features": ["accessPoint"],
         "interfaces": ["radios"]},
        {"id": AP2, "name": "garage-ap", "model": "U6 Lite", "macAddress": "aa:00:00:00:00:04",
         "ipAddress": "192.168.1.4", "state": "OFFLINE", "features": ["accessPoint"],
         "interfaces": ["radios"]},
    ]
    if version >= 10:
        for r in rows:
            r.update(firmwareVersion="4.3.6", firmwareUpdatable=r["id"] == SW, supported=True)
    return rows


def _detail(device: dict, version: int) -> dict:
    """The device's own page: firmware (in every version), uplink, interfaces."""
    freq = (lambda f: str(f)) if version < 10 else (lambda f: f)
    out = {k: v for k, v in device.items() if k not in ("features", "interfaces")}
    out.update(firmwareVersion="4.3.6", firmwareUpdatable=device["id"] == SW, supported=True,
               configurationId="abc", features={f: {} for f in device["features"]},
               interfaces={})
    if device["id"] != GW:
        out["uplink"] = {"deviceId": GW if device["id"] == SW else SW}
    if "ports" in device["interfaces"]:
        out["interfaces"]["ports"] = [
            {"idx": i, "state": "UP" if i <= 3 else "DOWN", "connector": "RJ45",
             "maxSpeedMbps": 1000, "speedMbps": 1000} for i in range(1, 9)]
    if "radios" in device["interfaces"]:
        out["interfaces"]["radios"] = [
            {"frequencyGHz": freq(2.4), "channelWidthMHz": 20, "wlanStandard": "802.11ax"},
            {"frequencyGHz": freq(5), "channelWidthMHz": 80, "wlanStandard": "802.11ax"}]
    return out


def _stats(i: int) -> dict:
    return {"uptimeSec": 86400 * i, "cpuUtilizationPct": 10.5 * i, "memoryUtilizationPct": 40.2,
            "loadAverage1Min": 0.4, "uplink": {"txRateBps": 1_000_000, "rxRateBps": 5_000_000},
            "interfaces": {}}


def _clients() -> list[dict]:
    out = []
    for i in range(7):
        out.append({"type": "WIRELESS" if i < 5 else "WIRED", "id": f"c{i}", "name": f"phone-{i}",
                    "macAddress": f"bb:00:00:00:00:{i:02x}", "ipAddress": f"192.168.1.{100 + i}",
                    "uplinkDeviceId": AP if i < 5 else SW,
                    "access": {"type": "GUEST" if i == 0 else "DEFAULT"}})
    out.append({"type": "VPN", "id": "v1", "name": "laptop", "access": {"type": "DEFAULT"}})
    return out


def real(version: int = 10) -> dict:
    devices = _devices(version)
    answers = {
        "/v1/info": {"applicationVersion": "10.6.106" if version >= 10 else "9.1.120"},
        "/v1/sites": [{"id": SITE, "internalReference": "default", "name": "Default"}],
        f"/v1/sites/{SITE}/devices": devices,
        f"/v1/sites/{SITE}/clients": _clients(),
    }
    for i, d in enumerate(devices, 1):
        answers[f"/v1/sites/{SITE}/devices/{d['id']}"] = _detail(d, version)
        if d["state"] == "ONLINE":
            answers[f"/v1/sites/{SITE}/devices/{d['id']}/statistics/latest"] = _stats(i)
    return answers


DENIED = object()
PAGED = ("/v1/sites", f"/v1/sites/{SITE}/devices", f"/v1/sites/{SITE}/clients")


class FakeUniFi:
    """A TLS HTTP/1.1 server on 127.0.0.1 answering like a UniFi console."""

    def __init__(self) -> None:
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="spark-unifi-"))
        self.requests: list[dict] = []
        self.raw: list[bytes] = []
        self.answers: dict = {}
        self.page_size = 200
        self.use("first")

    def use(self, name: str) -> None:
        import ssl
        cert, key, self.fingerprint = _certificate(self.tmp, name)
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(cert, key)

    def _answer(self, target: str) -> tuple[str, object]:
        url = urlsplit(target)
        if not url.path.startswith(BASE):
            return "404 Not Found", {"message": "no such page"}
        path = url.path.removeprefix(BASE)
        if path not in self.answers:
            return "404 Not Found", {"code": "api.err.NotFound", "message": "Not found",
                                     "statusCode": 404, "statusName": "NOT_FOUND"}
        found = self.answers[path]
        if found is DENIED:
            return "403 Forbidden", {"message": "Access denied", "statusCode": 403}
        if path not in PAGED:
            return "200 OK", found
        query = parse_qs(url.query)
        offset = int(query.get("offset", ["0"])[0])
        limit = min(int(query.get("limit", ["25"])[0]), self.page_size)
        data = found[offset:offset + limit]
        return "200 OK", {"offset": offset, "limit": limit, "count": len(data),
                          "totalCount": len(found), "data": data}

    async def _handle(self, reader, writer):  # type: ignore[no-untyped-def]
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            writer.close()
            return
        self.raw.append(head)
        lines = head.decode().split("\r\n")
        method, target, _ = lines[0].split(" ", 2)
        headers = {k.strip().lower(): v.strip() for k, _, v in
                   (line.partition(":") for line in lines[1:] if line)}
        self.requests.append({"method": method, "path": target, "headers": headers})
        if headers.get("x-api-key") != KEY:
            status, body = "401 Unauthorized", {"code": "api.authentication.missing-credentials",
                                                "message": "Unauthorized", "statusCode": 401}
        else:
            status, body = self._answer(target)
        data = json.dumps(body).encode()
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

    def paths(self) -> list[str]:
        return [r["path"].removeprefix(BASE) for r in self.requests]


@pytest.fixture(scope="module")
def fake():
    server = FakeUniFi()
    server.start()
    return server


@pytest.fixture(autouse=True)
def _fresh(fake):
    fake.requests.clear()
    fake.raw.clear()
    fake.page_size = 200
    fake.use("first")
    fake.answers = real()


# --------------------------------------------------------------------------
# The key and the address
# --------------------------------------------------------------------------


class TestKey:
    @pytest.mark.parametrize("raw", [KEY, f"  {KEY}\n", f"X-API-KEY: {KEY}"])
    def test_good(self, raw):
        assert unifi.check_key(raw) == KEY

    @pytest.mark.parametrize("raw, why", [
        ("", "Paste the API key"),
        ("abc def ghi jkl mno pqr", "has spaces"),
        ("short", "too short"),
        ("x" * 600, "too long"),
        ("ключключключключключ", "has spaces or other characters"),
    ])
    def test_refused(self, raw, why):
        with pytest.raises(ValueError, match=why):
            unifi.check_key(raw)


class TestHosts:
    def test_default_port_443(self):
        assert unifi.split_host("192.168.1.1") == ("192.168.1.1", 443)
        assert unifi.split_host("unifi.lan:8443") == ("unifi.lan", 8443)
        assert unifi.split_host("[fe80::1]") == ("fe80::1", 443)
        assert unifi.url_for("unifi.lan", "/v1/info") == \
            "https://unifi.lan/proxy/network/integration/v1/info"
        assert unifi.url_for("unifi.lan:8443") == "https://unifi.lan:8443/proxy/network/integration"

    def test_only_ever_https_and_only_get(self):
        source = Path(unifi.__file__).read_text()
        assert "http://" not in source, "no plain-HTTP path at all"
        for verb in ("POST ", "PUT ", "PATCH ", "DELETE "):
            assert f'"{verb}' not in source and f"f\"{verb}" not in source, "read only"


# --------------------------------------------------------------------------
# The client
# --------------------------------------------------------------------------


class TestClientPinning:
    def test_no_pin_means_nothing_is_sent(self, fake):
        with pytest.raises(unifi.CertificateNotTrusted) as caught:
            run(unifi.test(fake.host, KEY, None))
        assert caught.value.fingerprint == fake.fingerprint and not caught.value.changed
        assert fake.requests == [] and fake.raw == [], "not the key, not anything"

    def test_the_pinned_certificate_reads_everything(self, fake):
        info = run(unifi.test(fake.host, KEY, fake.fingerprint))
        assert (info.version, info.hostname) == ("10.6.106", "Default")
        paths = fake.paths()
        assert paths[:4] == ["/v1/info", "/v1/sites?offset=0&limit=200",
                             f"/v1/sites/{SITE}/devices?offset=0&limit=200",
                             f"/v1/sites/{SITE}/clients?offset=0&limit=200"]
        assert f"/v1/sites/{SITE}/devices/{AP}/statistics/latest" in paths
        assert f"/v1/sites/{SITE}/devices/{AP2}" in paths
        assert f"/v1/sites/{SITE}/devices/{AP2}/statistics/latest" not in paths, \
            "no statistics asked of an offline device"
        assert all(r["method"] == "GET" for r in fake.requests), "read only"
        assert {r["headers"]["x-api-key"] for r in fake.requests} == {KEY}
        assert fake.requests[0]["headers"]["host"] == fake.host

    def test_a_different_certificate_gets_nothing(self, fake):
        trusted = fake.fingerprint
        fake.use("impostor")
        with pytest.raises(unifi.CertificateNotTrusted) as caught:
            run(unifi.test(fake.host, KEY, trusted))
        assert caught.value.changed and caught.value.fingerprint == fake.fingerprint
        assert fake.requests == []

    def test_a_refused_key(self, fake):
        with pytest.raises(unifi.LoginFailed, match="refused the API key.*UniFi says: Unauthorized"):
            run(unifi.test(fake.host, "Zz" * 16, fake.fingerprint))

    def test_not_a_unifi_console(self, fake):
        del fake.answers["/v1/info"]
        with pytest.raises(unifi.UniFiError, match="There is no UniFi Network API at"):
            run(unifi.test(fake.host, KEY, fake.fingerprint))

    def test_nothing_listening(self):
        with pytest.raises(unifi.UniFiError, match="Could not open the UniFi API"):
            run(unifi.test("127.0.0.1:9", KEY, None))

    def test_a_part_it_may_not_read_is_left_out(self, fake):
        fake.answers[f"/v1/sites/{SITE}/clients"] = DENIED
        fake.answers[f"/v1/sites/{SITE}/devices/{AP}/statistics/latest"] = DENIED
        info = run(unifi.test(fake.host, KEY, fake.fingerprint))
        assert info.readings["clients"] is None
        ap = next(d for d in info.readings["devices"] if d["id"] == AP)
        assert ap["cpu"] is None and ap["firmware"] == "4.3.6", "the rest of it still read"

    def test_every_page_is_read(self, fake):
        fake.page_size = 3          # a console that pages smaller than asked
        info = run(unifi.test(fake.host, KEY, fake.fingerprint))
        assert len(info.readings["devices"]) == 4
        assert info.readings["clients"]["total"] == 8
        assert f"/v1/sites/{SITE}/clients?offset=6&limit=200" in fake.paths()


# --------------------------------------------------------------------------
# Parsing: 9.x and 10.x the same
# --------------------------------------------------------------------------


@pytest.mark.parametrize("version", [9, 10])
def test_both_versions_read_alike(fake, version):
    fake.answers = real(version)
    readings = run(unifi.test(fake.host, KEY, fake.fingerprint)).readings
    by = {d["name"]: d for d in readings["devices"]}
    assert list(by) == ["garage-ap", "gateway", "lounge-ap", "office-switch"]
    ap = by["lounge-ap"]
    assert ap["bands"] == ["2.4", "5"], "a band is text in 9.x and a number in 10.x"
    assert (ap["firmware"], ap["state"], ap["uplink"], ap["roles"]) == \
        ("4.3.6", "ONLINE", SW, ["accessPoint"])
    assert ap["mac"] == "aa:00:00:00:00:03" and by["gateway"]["mac"] == "aa:00:00:00:00:01"
    assert (ap["cpu"], ap["mem"], ap["uptime"]) == (31.5, 40.2, 3 * 86400)
    assert by["office-switch"]["updatable"] is True and by["office-switch"]["ports_up"] == 3
    assert by["garage-ap"]["state"] == "OFFLINE" and by["garage-ap"]["uptime"] is None
    assert readings["clients"] == {"total": 8, "wired": 2, "wireless": 5, "vpn": 1,
                                   "teleport": 0, "other": 0, "guest": 1,
                                   "by_device": {AP: 5, SW: 2}}
    assert "phone-1" not in json.dumps(readings) and "bb:00:00" not in json.dumps(readings), \
        "clients are counted, not kept"


def _parse(**over):  # type: ignore[no-untyped-def]
    a = real()
    devices = a[f"/v1/sites/{SITE}/devices"]
    args = {"version": "10.6.106", "sites": a["/v1/sites"], "devices": {SITE: devices},
            "clients": {SITE: a[f"/v1/sites/{SITE}/clients"]},
            "details": {d["id"]: a[f"/v1/sites/{SITE}/devices/{d['id']}"] for d in devices},
            "stats": {}}
    args.update(over)
    return unifi.parse(**args)


class TestTolerant:
    def test_what_it_does_not_know_is_kept_or_ignored(self):
        devices = copy.deepcopy(_devices(9))
        devices[0].update(state="some_new_state", newField={"x": 1}, name=None)
        devices.append({"name": "no id"})                      # no id: skipped
        devices.append("not an object")
        out = _parse(devices={SITE: devices}, details={})
        assert len(out["devices"]) == 4
        gw = next(d for d in out["devices"] if d["id"] == GW)
        assert gw["state"] == "SOME_NEW_STATE", "a state it has not seen is kept as written"
        assert gw["name"] == "UDM Pro", "no name: its model"
        assert gw["firmware"] is None and gw["uplink"] is None, "9.x list, no device page"
        assert "newField" not in gw

    @pytest.mark.parametrize("raw, want", [(5, "5"), ("5", "5"), (2.4, "2.4"), ("2.4", "2.4"),
                                           (6.0, "6"), (None, None), ("x", None), (True, None)])
    def test_band(self, raw, want):
        assert unifi.band(raw) == want

    def test_numbers_as_text(self):
        out = _parse(stats={AP: {"cpuUtilizationPct": "12.5", "uptimeSec": "600",
                                 "memoryUtilizationPct": 140}})
        ap = next(d for d in out["devices"] if d["id"] == AP)
        assert (ap["cpu"], ap["uptime"], ap["mem"]) == (12.5, 600, 100.0)

    def test_an_unanswered_part_is_none_an_empty_one_is_empty(self):
        out = _parse(devices={SITE: None}, clients={SITE: []})
        assert out["devices"] is None and out["clients"]["total"] == 0
        assert out["answered"] == {"clients": [SITE]}


# --------------------------------------------------------------------------
# As a credential, and the card
# --------------------------------------------------------------------------


@pytest.fixture
def db():
    config = _config()

    async def setup():
        D.init_engine(config)
        await D.init_db(config)
        async with D.session_scope() as s:
            s.add(Device(mac="aa:00:00:00:00:01", primary_ip="192.168.1.1", friendly_name="gw"))
            s.add(Device(mac="aa:00:00:00:00:03", primary_ip="192.168.1.3", friendly_name="ap"))
    run(setup())
    yield config
    run(D.close_engine())


class TestCredential:
    def test_a_bad_key_is_refused_on_add(self, db):
        async def go():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                with pytest.raises(credentials.CredentialError, match="has spaces"):
                    await credentials.add(s, vault_for(db), kind="unifi", name="u", device_id=1,
                                          host="", api_key="not a real key at all")
        run(go())

    def test_test_trust_check(self, db, fake):
        async def go():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                row = await credentials.add(s, vault_for(db), kind="unifi", name="unifi",
                                            device_id=1, host=fake.host, api_key=KEY)
                assert not await credentials.test(s, vault_for(db), row)
                assert row.pending_sha256 == fake.fingerprint and fake.requests == []
                assert await credentials.trust(s, vault_for(db), row, row.pending_sha256)
                return row.id
        ident = run(go())
        assert run(credentials.check_one(db, ident))

        async def read():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                return await s.get(ApiCredential, ident)
        row = run(read())
        assert row.last_info == {"version": "10.6.106", "hostname": "Default"}
        assert len(row.readings["devices"]) == 4 and row.readings["read_at"]
        assert vault_for(db).open(row.key_sealed) == KEY


@pytest.fixture
def site(db):
    with TestClient(create_app(db), follow_redirects=False) as client:
        client.post("/setup", data={"setup_code": client.app.state.setup_code, "username": "admin",
                                    "password": PASSWORD, "password_confirm": PASSWORD,
                                    "timezone": "UTC"})
        yield client


class TestPages:
    def test_add_trust_and_the_card(self, site, fake):
        page = flat(site.get("/settings/credentials").text)
        assert '<option value="unifi" >UniFi</option>' in page or 'value="unifi"' in page
        assert "UniFi Network's <strong>Integrations</strong> page" in page
        response = site.post("/settings/credentials", data={
            "kind": "unifi", "name": "unifi", "device_id": "1", "host": fake.host, "api_key": KEY})
        assert response.status_code == 303 and fake.requests == []
        page = flat(site.get("/settings/credentials").text)
        assert '<span class="pill neutral">UniFi</span>' in page
        assert "the padlock → the certificate" in page
        site.post("/settings/credentials/1/trust", data={"fingerprint": fake.fingerprint})
        page = flat(site.get("/settings/credentials").text)
        assert '<span class="pill ok dot">connected</span> UniFi 10.6.106 · Default' in page
        assert KEY not in page

        page = flat(site.get("/devices/1").text)
        assert "<h2>UniFi</h2>" in page
        assert "3 of 4 devices online" in page
        assert "8 clients (2 wired, 5 wireless, 1 VPN, 1 guest)" in page
        assert "UniFi Network 10.6.106" in page
        assert '<a href="/devices/2">lounge-ap</a>' in page, "linked to its SPARK page by MAC"
        assert '<a href="/devices/1">gateway</a>' in page, "MAC case does not matter"
        assert "garage-ap" in page and "garage-ap</a>" not in page, "no SPARK device, no link"
        assert "via office-switch" in page and "via gateway" in page
        assert '<span class="pill bad">offline</span>' in page
        assert '<span class="pill warn" title="UniFi has a newer firmware for it">update</span>' in page
        assert KEY not in page

    def test_a_bad_key_says_why_and_keeps_what_was_typed(self, site):
        response = site.post("/settings/credentials", data={
            "kind": "unifi", "name": "unifi", "device_id": "1", "host": "", "api_key": "a b"})
        page = flat(response.text)
        assert response.status_code == 400 and "That is not an API key" in page
        assert 'class="cred-form add-cred-form is-unifi"' in page


# --------------------------------------------------------------------------
# Identity: a device SPARK knows by IP alone gains the MAC UniFi reports
# --------------------------------------------------------------------------


def _add_devices(*rows):  # type: ignore[no-untyped-def]
    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            for mac, ip in rows:
                s.add(Device(mac=mac, primary_ip=ip))
    run(go())


def _macs():  # type: ignore[no-untyped-def]
    from sqlalchemy import select

    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            return {d.primary_ip: d.mac for d in (await s.execute(select(Device))).scalars()}
    return run(go())


def _fill(readings):  # type: ignore[no-untyped-def]
    async def go():  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            return [d.primary_ip for d in await unifi.fill_in_macs(s, readings)]
    return run(go())


def _readings(*rows):  # type: ignore[no-untyped-def]
    return {"devices": [{"ip": ip, "mac": mac} for mac, ip in rows]}


class TestFillInMacs:
    def test_a_routed_device_gains_its_mac(self, db):
        _add_devices((None, "192.168.1.2"))
        assert _fill(_readings(("aa:00:00:00:00:02", "192.168.1.2"))) == ["192.168.1.2"]
        assert _macs()["192.168.1.2"] == "aa:00:00:00:00:02"

    def test_not_a_mac_another_device_has(self, db):
        _add_devices((None, "192.168.1.9"))
        assert _fill(_readings(("aa:00:00:00:00:03", "192.168.1.9"))) == [], \
            "the AP already has it: that is a merge for a person to make"
        assert _macs()["192.168.1.9"] is None

    def test_not_when_two_devices_share_the_address(self, db):
        _add_devices((None, "192.168.1.5"), (None, "192.168.1.5"))
        assert _fill(_readings(("aa:00:00:00:00:05", "192.168.1.5"))) == []

    def test_not_when_unifi_has_two_macs_at_the_address(self, db):
        _add_devices((None, "192.168.1.6"))
        assert _fill(_readings(("aa:00:00:00:00:06", "192.168.1.6"),
                               ("aa:00:00:00:00:07", "192.168.1.6"))) == []

    def test_not_an_address_a_polled_device_says_is_its_own(self, db):
        from spark.models import SnmpAddress, SnmpDevice
        from spark.snmp_config import ProfileInput, save_profile

        _add_devices((None, "192.168.1.7"))

        async def own():  # type: ignore[no-untyped-def]
            async with D.session_scope() as s:
                profile = await save_profile(s, vault_for(db), ProfileInput(
                    name="p", version="v2c", community="x"))
                snmp = SnmpDevice(device_id=1, profile_id=profile.id)
                s.add(snmp)
                await s.flush()
                s.add(SnmpAddress(snmp_device_id=snmp.id, kind="own", ip="192.168.1.7"))
        run(own())
        assert _fill(_readings(("aa:00:00:00:00:08", "192.168.1.7"))) == []

    def test_nothing_odd_is_written(self, db):
        _add_devices((None, "192.168.1.8"))
        assert _fill(_readings(("not-a-mac", "192.168.1.8"), (None, "192.168.1.8"))) == []

    def test_the_check_fills_it_in_and_the_card_links_by_address(self, db, site, fake):
        _add_devices((None, "192.168.1.2"), (None, "192.168.1.4"))   # switch; garage AP
        _add_devices(("aa:00:00:00:00:99", "192.168.1.250"))
        site.post("/settings/credentials", data={
            "kind": "unifi", "name": "unifi", "device_id": "1", "host": fake.host, "api_key": KEY})
        site.post("/settings/credentials/1/trust", data={"fingerprint": fake.fingerprint})
        page = flat(site.get("/devices/1").text)
        assert '<a href="/devices/3">office-switch</a>' in page, "by address, before any check"
        assert run(credentials.check_one(db, 1))
        macs = _macs()
        assert macs["192.168.1.2"] == "aa:00:00:00:00:02"
        assert macs["192.168.1.4"] == "aa:00:00:00:00:04"
        page = flat(site.get("/devices/1").text)
        assert '<a href="/devices/3">office-switch</a>' in page and \
            '<a href="/devices/4">garage-ap</a>' in page


# --------------------------------------------------------------------------
# Devices: the API pill and filter
# --------------------------------------------------------------------------


class TestDevicesList:
    def test_no_filter_until_there_is_a_credential(self, site):
        page = flat(site.get("/devices").text)
        assert 'id="api-filter"' not in page and "api-pill" not in page

    def test_the_pill_and_the_filter(self, site, fake):
        site.post("/settings/credentials", data={
            "kind": "unifi", "name": "unifi", "device_id": "1", "host": fake.host, "api_key": KEY})
        page = flat(site.get("/devices").text)
        assert '<select name="api" id="api-filter" class="auto-submit">' in page
        assert '<option value="unifi" >UniFi</option>' in page
        assert ('<a class="pill neutral api-pill" href="/devices/1#api-1" '
                'title="UniFi API — open its card">UniFi</a>') in page
        assert page.count("api-pill") == 1, "only on the device the credential is for"

        page = flat(site.get("/devices?api=unifi").text)
        assert 'value="192.168.1.1"' in page or "<code>192.168.1.1</code>" in page
        assert "<code>192.168.1.3</code>" not in page, "the AP has no credential"
        assert "Showing 1 of 2 device(s)." in page
        assert flat(site.get("/devices?api=any").text).count("api-pill") == 1

        page = flat(site.get("/devices?api=proxmox").text)
        assert "No devices match" in page and "has a Proxmox credential" in page
        assert flat(site.get("/devices?api=nonsense").text).count("api-pill") == 1, \
            "an unknown value shows everything"

    def test_every_filter_submits_itself(self, site, fake):
        """The API filter once did nothing in a browser: the script that
        submits a filter on change listed the others by id. Now every
        dropdown in the filter bar carries the class the script looks for."""
        import re
        site.post("/settings/credentials", data={
            "kind": "unifi", "name": "unifi", "device_id": "1", "host": fake.host, "api_key": KEY})
        page = site.get("/devices?per_page=25").text
        bar = page[page.index('<section class="card filter-bar">'):]
        bar = bar[:bar.index("</section>")]
        selects = re.findall(r"<select[^>]*>", bar)
        assert len(selects) >= 2 and any('name="api"' in t for t in selects)
        assert all('class="auto-submit"' in t for t in selects), selects
        assert "el.matches('select.auto-submit')" in page


def test_paging_keeps_the_api_filter():
    from spark.web.routes_devices import _query
    assert _query("", 25, 2, "", "unifi") == "?api=unifi&per_page=25&page=2"
    assert _query("", 25, 2, "on", "nonsense") == "?snmp=on&per_page=25&page=2"


class TestNoDevice:
    """Connected, and seen nowhere: a credential on no device (left by a
    merge before merges carried credentials) has no card and no pill. Its
    row says so, and so does the API filter, rather than it looking gone."""

    def add(self, site, fake, device_id):  # type: ignore[no-untyped-def]
        site.post("/settings/credentials", data={
            "kind": "unifi", "name": "unifi", "device_id": device_id, "host": fake.host,
            "api_key": KEY})

    def test_the_credentials_row_says_so(self, site, fake):
        self.add(site, fake, "")
        page = flat(site.get("/settings/credentials").text)
        assert '<span class="pill warn">No device</span>' in page
        assert "Its card shows on no device page" in page
        assert "Choose its device under <strong>Edit unifi</strong>." in page

    def test_not_when_it_has_one(self, site, fake):
        self.add(site, fake, "1")
        assert "No device</span>" not in site.get("/settings/credentials").text
        assert "filter-note" not in site.get("/devices?api=unifi").text

    def test_the_api_filter_says_what_it_leaves_out(self, site, fake):
        self.add(site, fake, "")
        page = flat(site.get("/devices?api=unifi").text)
        assert "1 UniFi credential is on no device, so not listed here." in page
        assert 'Choose its device in <a href="/settings/credentials">' in page
        assert "1 UniFi credential is on no device" in flat(site.get("/devices?api=any").text)
        assert "filter-note" not in site.get("/devices?api=truenas").text, "another kind"
        assert "filter-note" not in site.get("/devices").text, "only when filtering by API"
