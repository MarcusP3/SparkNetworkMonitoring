"""Integrations: the sidebar's menu, and a page for each kind (integrations.py).

The menu has a line for each kind there is one of -- or, with none, the way
to Settings to add the first. Each kind's page lists every one of it, with
the cards its device's page shows. Proxmox's and UniFi's pages are tested
beside their cards, in test_proxmox.py and test_unifi.py.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from spark import db as D
from spark import hermes_scan
from spark.main import create_app
from spark.models import Device

from test_credentials import PASSWORD, FakeTrueNAS, _config, _trusted, flat, run
from test_hermes import TestWatchAService, answer, server, the_target_for  # noqa: F401 - fixtures
from test_truenas_health import real


@pytest.fixture(scope="module")
def fake():
    nas = FakeTrueNAS()
    nas.start()
    return nas


@pytest.fixture(autouse=True)
def _fresh(fake):
    fake.received.clear()
    fake.use("first")
    fake.answers = real()


@pytest.fixture
def db():
    config = _config()

    async def setup():
        D.init_engine(config)
        await D.init_db(config)
        async with D.session_scope() as s:
            s.add(Device(mac="aa:00:00:00:00:01", primary_ip="192.168.1.20", friendly_name="nas"))
    run(setup())
    yield config
    run(D.close_engine())


@pytest.fixture
def site(db):
    with TestClient(create_app(db), follow_redirects=False) as client:
        client.post("/setup", data={"setup_code": client.app.state.setup_code, "username": "admin",
                                    "password": PASSWORD, "password_confirm": PASSWORD,
                                    "timezone": "UTC"})
        yield client


def menu(site, path: str = "/") -> str:  # type: ignore[no-untyped-def]
    """The sidebar's Integrations menu on a page."""
    page = flat(site.get(path).text)
    start = page.index('<details class="nav-group"')
    return page[start:page.index("</details>", start)]


class TestMenu:
    def test_with_none_it_offers_settings(self, site):
        m = menu(site)
        assert ('<div class="nav-sub"> <a href="/settings/credentials?add#credentials" class="nav-add">'
                'Add credentials in Settings</a> </div>') in m
        assert "/integrations/" not in m

    def test_a_line_for_each_kind_there_is(self, site, db, fake):
        run(_trusted(db, fake.host))
        m = menu(site)
        assert '<a href="/integrations/truenas" class="">TrueNAS</a>' in m
        assert "/integrations/proxmox" not in m and "/integrations/hermes" not in m
        assert 'class="nav-add">Add an integration…</a>' in m

    def test_two_of_a_kind_are_one_line(self, site, db, fake):
        run(_trusted(db, fake.host))
        run(_trusted(db, fake.host, name="backup", device_id=None))
        m = menu(site)
        assert m.count("/integrations/truenas") == 1
        assert '<a href="/integrations/truenas" class="">TrueNAS <span class="nav-count">2</span></a>' in m

    def test_open_on_its_own_pages(self, site, db, fake):
        run(_trusted(db, fake.host))
        assert menu(site).startswith('<details class="nav-group">')
        m = menu(site, "/integrations/truenas")
        assert m.startswith('<details class="nav-group" open> <summary class="active">Integrations</summary>')
        assert '<a href="/integrations/truenas" class="active">TrueNAS</a>' in m

    def test_the_link_for_narrow_screens(self, site):
        page = flat(site.get("/").text)
        assert '</details> <a href="/integrations" class="nav-integrations">Integrations</a>' in page


class TestTrueNAS:
    def test_every_one_with_its_cards(self, site, db, fake):
        first = run(_trusted(db, fake.host))
        second = run(_trusted(db, fake.host, name="backup", device_id=None))
        page = flat(site.get("/integrations/truenas").text)
        assert "<h1>TrueNAS</h1>" in page and "2 set up." in page
        assert page.index("<h2>backup</h2>") < page.index("<h2>truenas</h2>"), "by name"
        assert 'on <a href="/devices/1">nas</a> ·' in page
        assert f"at <code>{fake.host}</code> ·" in page, "no device: its address"
        assert f'id="storage-{first}"' in page and f'id="storage-{second}"' in page
        assert page.count("<td><code>sda</code></td>") == 2
        assert "SECRET-SERIAL" not in page

    def test_the_device_page_is_as_it_was(self, site, db, fake):
        run(_trusted(db, fake.host))
        page = flat(site.get("/devices/1").text)
        assert "<h2>TrueNAS API</h2>" in page and '<section class="card" id="storage">' in page
        assert '<a href="/devices/1">nas</a>' not in page, "no link to the page it is on"

    def test_test_comes_back_here(self, site, db, fake):
        ident = run(_trusted(db, fake.host))
        page = flat(site.get("/integrations/truenas").text)
        assert '<input type="hidden" name="back" value="/integrations/truenas">' in page
        response = site.post(f"/settings/credentials/{ident}/test", data={"back": "/integrations/truenas"})
        assert response.headers["location"] == f"/integrations/truenas#api-{ident}"


class TestPages:
    def test_none_of_a_kind_says_how_to_add_one(self, site):
        page = flat(site.get("/integrations/proxmox").text)
        assert "<h2>No Proxmox yet</h2>" in page
        assert '<a class="btn-quiet" href="/settings/credentials?add=proxmox#credentials">Add a Proxmox</a>' in page
        page = flat(site.get("/integrations/hermes").text)
        assert "<h2>No Hermes yet</h2>" in page and '<a class="btn-quiet" href="/targets/new">' in page

    def test_add_opens_settings_at_that_kind(self, site):
        page = flat(site.get("/settings/credentials?add=unifi").text)
        assert '<details class="add-cred" open>' in page
        assert '<option value="unifi" selected>UniFi</option>' in page
        assert 'class="cred-form add-cred-form is-unifi"' in page
        for query in ("?add", "?add=nonsense"):
            page = flat(site.get("/settings/credentials" + query).text)
            assert '<details class="add-cred" open>' in page, "open, at no kind in particular"
            kinds = page.split('class="cred-kind"')[1]
            assert " selected>" not in kinds[:kinds.index("</select>")]

    def test_an_unknown_kind_goes_to_the_list(self, site):
        response = site.get("/integrations/docker")
        assert response.status_code == 303 and response.headers["location"] == "/integrations"

    def test_the_list(self, site, db, fake):
        page = flat(site.get("/integrations").text)
        assert "<h2>No integrations yet</h2>" in page
        assert '<a class="btn-primary" href="/settings/credentials?add#credentials">Add credentials</a>' in page
        run(_trusted(db, fake.host))
        page = flat(site.get("/integrations").text)
        assert '<a href="/integrations/truenas">' in page and "No integrations yet" not in page
        assert '<span class="pill neutral">1</span>' in page and "not connected" not in page

    def test_signed_out_is_sent_to_sign_in(self, db):
        with TestClient(create_app(db), follow_redirects=False) as client:
            assert client.get("/integrations/truenas").headers["location"].startswith("/setup")


class TestHermes:
    service = TestWatchAService.service

    def test_a_found_dashboard_then_its_check(self, site, server, answer):  # noqa: F811
        sid = self.service(int(server.rsplit(":", 1)[1]), "hermes dashboard")
        run(hermes_scan.read_due())
        assert '<a href="/integrations/hermes" class="">Hermes</a>' in menu(site)
        page = flat(site.get("/integrations/hermes").text)
        assert f'id="hermes-found-{sid}"' in page and "<h2>Hermes on agent-vm</h2>" in page
        assert 'on <a href="/devices/2">agent-vm</a> ·' in page
        assert '<input type="hidden" name="back" value="/integrations/hermes">' in page
        moved = site.post(f"/devices/services/{sid}/watch", data={"back": "/integrations/hermes"})
        tid = the_target_for(sid)
        assert moved.headers["location"] == f"/integrations/hermes#hermes-{tid}"
        page = flat(site.get("/integrations/hermes").text)
        assert f'id="hermes-{tid}"' in page and "hermes-found" not in page, "one card, the check's"
        assert "<h2>agent-vm Hermes</h2>" in page and 'on <a href="/devices/2">agent-vm</a> ·' in page
        assert "nav-count" not in menu(site), "still one Hermes"

    def test_watch_anywhere_else_still_goes_to_targets(self, site, server, answer):  # noqa: F811
        sid = self.service(int(server.rsplit(":", 1)[1]), "hermes dashboard")
        moved = site.post(f"/devices/services/{sid}/watch", data={"back": "/integrations/truenas"})
        assert moved.headers["location"] == "/targets"
