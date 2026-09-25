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
        "network": {"subnets": [{"name": "LAN", "cidr": "10.1.10.0/24", "attached": True}]},
    })
    config.app.data_dir.mkdir(parents=True, exist_ok=True)
    with TestClient(create_app(config), follow_redirects=False) as c:
        c.post("/setup", data={"username": "admin", "password": PASSWORD,
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


def test_old_single_page_links_are_forwarded(client):
    page = client.get("/settings").text
    assert "'#snmp': '/settings/snmp'" in page and "'#alerts': '/settings/alerts'" in page
