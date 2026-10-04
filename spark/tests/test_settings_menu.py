"""Settings as separate pages with a menu, instead of one long page.

What has to hold: each page shows its own card and no other; the menu is on
every page and marks the one you are on; a form sends you back to the page it
was on, including when it fails; and the old single-page links still land.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from spark.config import Config
from spark.main import create_app

PASSWORD = "correct horse battery"
CARDS = {"subnets": "<h2>Subnets</h2>", "ports": "<h2>Port scanning</h2>",
         "snmp": "<h2>SNMP</h2>", "alerts": "<h2>Alerts</h2>"}
URLS = {"subnets": "/settings", "ports": "/settings/ports",
        "snmp": "/settings/snmp", "alerts": "/settings/alerts"}


@pytest.fixture
def client():
    tmp = Path(tempfile.mkdtemp(prefix="spark-menu-"))
    config = Config.model_validate({
        "app": {"data_dir": str(tmp / "data"), "log_level": "WARNING"},
        "network": {"subnets": [{"name": "LAN", "cidr": "172.16.10.0/24", "attached": True}]},
    })
    config.app.data_dir.mkdir(parents=True, exist_ok=True)
    with TestClient(create_app(config), follow_redirects=False) as c:
        c.post("/setup", data={"setup_code": c.app.state.setup_code, "username": "admin", "password": PASSWORD,
                               "password_confirm": PASSWORD})
        yield c


def active(page: str) -> str:
    match = re.search(r'<a href="([^"]+)" class="active"\s+aria-current="page"', page)
    return match.group(1) if match else ""


@pytest.mark.parametrize("section", list(URLS))
def test_each_page_shows_only_its_own_card_and_marks_itself(client, section):
    response = client.get(URLS[section])
    assert response.status_code == 200
    page = response.text
    for other, heading in CARDS.items():
        assert (heading in page) == (other == section), f"{other} on the {section} page"
    assert active(page) == URLS[section]
    # The whole menu, every time.
    for url in URLS.values():
        assert f'<a href="{url}"' in page


def test_unknown_or_duplicate_pages_go_to_the_first_one(client):
    for path in ("/settings/nope", "/settings/subnets"):
        response = client.get(path)
        assert response.status_code in (302, 303) and response.headers["location"] == "/settings"


@pytest.mark.parametrize("path,data,back", [
    ("/settings/ports", {"port": "8112", "name": "deluge"}, "/settings/ports"),
    ("/settings/port-scan", {"enabled": "1", "interval_hours": "6"}, "/settings/ports"),
    ("/settings/snmp/polling", {"interval_seconds": "60"}, "/settings/snmp"),
    ("/settings/alerts", {"enabled": "1"}, "/settings/alerts"),
    ("/settings/subnets", {"cidr": "10.9.9.0/24", "name": "Lab"}, "/settings"),
])
def test_a_form_goes_back_to_its_own_page(client, path, data, back):
    response = client.post(path, data=data)
    assert response.status_code == 303 and response.headers["location"] == back


def test_an_error_is_shown_on_the_page_it_came_from(client):
    page = client.post("/settings/ports", data={"port": "70000", "name": "x"}).text
    assert active(page) == "/settings/ports"
    assert CARDS["ports"] in page and CARDS["subnets"] not in page
    page = client.post("/settings/alerts", data={"quiet_start": "22:00"}).text
    assert active(page) == "/settings/alerts" and "both a start and an end" in page


def test_the_menu_says_what_is_set_up(client):
    page = client.get("/settings").text
    menu = page[page.index('class="subnav"'):page.index("</nav>", page.index('class="subnav"'))]
    assert re.search(r"Subnets</span>\s*<span class=\"subnav-hint\">1<", menu)
    assert re.search(r"SNMP</span>\s*<span class=\"subnav-hint\">0<", menu)
    assert 'subnav-hint warn">no webhook<' in menu


def test_the_tiles_on_every_settings_page(client):
    """SPARK 2's tiles: how much is set up, the same on every Settings page."""
    for path in ("/settings", "/settings/backup"):
        page = " ".join(client.get(path).text.split())
        tiles = dict(re.findall(r'<span class="stat-name">([^<]+)</span></span>'
                                r'<span class="stat-value">(.*?)</span></a>', page))
        assert tiles["Subnets"] == "1" and tiles["SNMP profiles"] == "0"
        assert tiles["Polled devices"] == "0" and tiles["Credentials"] == "0"
        assert tiles["Last backup"] == "—", "no nightly backup has run"
        on, of = re.fullmatch(r'(\d+)<span class="stat-of"> of (\d+)</span>', tiles["Alert rules on"]).groups()
        assert 0 < int(on) <= int(of), "the rules count what is switched on"
    page = " ".join(client.get("/settings").text.split())
    assert '<small class="subnav-about">Discord, rules, muted</small>' in page


def test_the_last_backup_tile(client):
    import asyncio
    from spark import db as D
    from spark.db import save_setting

    async def record(state):  # type: ignore[no-untyped-def]
        async with D.session_scope() as s:
            await save_setting(s, "backup_state", state)

    asyncio.run(record({"at": "2026-10-03T04:00:00+00:00", "ok": True}))
    page = " ".join(client.get("/settings").text.split())
    assert re.search(r'Last backup</span></span><span class="stat-value">\d\d:\d\d</span>', page)
    asyncio.run(record({"at": "2026-10-03T04:00:00+00:00", "ok": False, "error": "disk full"}))
    page = " ".join(client.get("/settings").text.split())
    assert 'class="stat s-down is-bad" href="/settings/backup"' in page
    assert 'Last backup</span></span><span class="stat-value">failed</span>' in page


def test_old_single_page_links_are_forwarded(client):
    page = client.get("/settings").text
    assert "'#snmp': '/settings/snmp'" in page and "'#alerts': '/settings/alerts'" in page
