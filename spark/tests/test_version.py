"""The version is in three places and shown on every page: they agree.

The footer said 0.1.0 through months of work because nothing tied the number
to anything. CLAUDE.md (*Versioning*) says when to bump; this says the
places it lives stay in step.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import spark

SPARK = Path(__file__).resolve().parents[1]
REPO = SPARK.parent


def _pyproject() -> str:
    return tomllib.loads((SPARK / "pyproject.toml").read_text())["project"]["version"]


def test_the_package_and_pyproject_agree():
    assert spark.__version__ == _pyproject()


def test_the_readme_says_the_same():
    match = re.search(r"\*\*Version\*\* (\S+)", (REPO / "README.md").read_text())
    assert match and match.group(1) == _pyproject()


def test_claude_md_says_the_same():
    match = re.search(r"\*\*Current: (\S+)\*\*", (REPO / "CLAUDE.md").read_text())
    assert match and match.group(1) == _pyproject()


def test_the_changelog_has_a_heading_for_it():
    assert f"## {_pyproject()} — " in (SPARK / "CHANGELOG.md").read_text()


def test_it_is_a_plain_version_number():
    assert re.fullmatch(r"\d+\.\d+\.\d+", _pyproject())
