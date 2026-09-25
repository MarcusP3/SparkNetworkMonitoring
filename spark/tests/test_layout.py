"""Layout rules that a browser enforces silently and a test has to enforce instead.

A table wider than the window used to widen the whole page: the cards stayed
the width of the window while the rows ran on past their edge, and the page
scrolled sideways. Every table now sits in a `.table-scroll` wrapper, which
scrolls inside its card instead. A new table without one brings the bug back,
so this checks every template.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parent.parent / "src" / "spark" / "templates"


@pytest.mark.parametrize("template", sorted(p.name for p in TEMPLATES.glob("*.html")))
def test_every_table_scrolls_inside_its_card(template):
    text = (TEMPLATES / template).read_text()
    for match in re.finditer(r"<table\b", text):
        before = text[: match.start()].rstrip()
        assert before.endswith('<div class="table-scroll">'), (
            f"{template}: a <table> at offset {match.start()} is not wrapped in "
            '<div class="table-scroll"> -- at narrow widths it will widen the page'
        )


CSS = (Path(__file__).resolve().parent.parent / "src" / "spark" / "static" / "app.css").read_text()


def _media_blocks(max_width: str) -> list[tuple[int, str]]:
    """(offset, body) of every `@media (max-width: <max_width>)` block."""
    blocks = []
    for match in re.finditer(r"@media \(max-width: " + re.escape(max_width) + r"\) \{", CSS):
        depth, i = 1, match.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(CSS[i], 0)
            i += 1
        blocks.append((match.start(), CSS[match.end(): i - 1]))
    return blocks


def test_phones_keep_the_navigation():
    """Below 640px the nav was `display: none` with nothing in its place, so a
    phone could not leave the page it was on. It now gets its own row."""
    phone = _media_blocks("640px")
    assert phone, "no phone rules for the top bar"
    for _, body in phone:
        assert not re.search(r"\.nav\s*\{[^}]*display:\s*none", body)
    assert any(".nav {" in body and "order: 3" in body for _, body in phone)


def test_phone_rules_come_after_the_tablet_rules():
    """Both apply on a phone and set the same properties at the same
    specificity, so whichever comes later wins. The phone row must."""
    tablet_nav = [start for start, body in _media_blocks("62rem") if ".nav a" in body]
    phone_nav = [start for start, body in _media_blocks("640px") if ".nav a" in body]
    assert tablet_nav and phone_nav and min(phone_nav) > max(tablet_nav)
