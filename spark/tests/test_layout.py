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
