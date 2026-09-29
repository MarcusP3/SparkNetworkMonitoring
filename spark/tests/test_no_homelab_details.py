"""No real homelab details in anything shipped: host names, device names,
pool names, or the owner's address scheme, in code, pages, docs or tests.

Examples use generic names (truenas, nas, office-switch) and generic ranges
(192.168.1.0/24, 172.16.x.0/24). What must never appear is listed only as
SHA-256 hashes, so this file does not repeat what it guards against.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

SPARK = Path(__file__).resolve().parents[1]
REPO = SPARK.parent
# What .gitignore keeps out of the repo is local, not shipped.
SKIP_DIRS = {".git", ".ruff_cache", ".pytest_cache", "__pycache__", "data", "secrets", ".venv",
             "venv", "build", "dist", ".idea", ".vscode"}
SKIP_SUFFIXES = {".png", ".ico", ".woff2", ".woff", ".ttf", ".db", ".db-wal", ".db-shm", ".pyc",
                 ".key", ".zip"}

# Lower-cased words and names.
WORDS = {
    "037bca645c48bb9eb901716f85b94d7722f6c4fd26ec8e7e5f98a7f04a2ebe36",
    "0765a6419cde1681a77b2387ee9f0272829c4721154226a34680dd1b371ec974",
    "092136e376e17ab5843512b0633bcbf7ded1e5053db3335ac42a13b256075c65",
    "0c397f8edd793e5aed487b566f168bbb4b8e35ba18371bf4b7dd02c52c29bc69",
    "0f2303079b727820c3a1363b4172e12b79d4d72ade31b462a6a25a819a8240b6",
    "4b9f3c41c21afa2718e9706db9f5c31c24046cc7d02ff48c6a234038f2ea3e49",
    "6ded2d470bf38fc167d52960059ff33a42beb5f719715a9d4bbb30ad2c9a4359",
    "76e3e31d2fe3507586e45e30641b8737dbe183ce04198999bbaae141bf71e4fe",
    "8cfde6efdfc4ed5ab1f6acbbd1ba49bf31932f84d0a4c090eb41c7d151e8b180",
    "d7f6206b1e79c7fd228eb4f76dfa79452df192713a31196bf1b120d171a8aa0e",
    "e758f4dd98405bcd8b165413f19915d92cef2e7ecf1334e6e6376872049d5b9e",
}
# The first two octets of an address scheme, e.g. "10.20".
PREFIXES = {
    "5ad3a5738540131c55f9c7d97ddf64440c889ab683af7294dfb3d2558f111263",
}
# Whole addresses.
ADDRESSES = {
    "b5650a9fc2b8bde975b910207b0faffda99da53d1530b4b6773ff71e5cca265c",
    "bed4cb96812bfa9536c072d4eecf921891b053a090af6235ffd5151b9a2ddc0c",
}


def _h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*")
# An IPv4 address or a prefix like "a.b.c." -- not part of a longer dotted
# run, so OIDs (1.3.6.1...) and OID indexes are left alone.
_ADDR = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})?(?!\.?\d)")


def _files():
    roots = [SPARK, *(p for p in (REPO / "README.md", REPO / "DESIGN.md", REPO / "CLAUDE.md")
                      if p.is_file())]
    for root in roots:
        if root.is_file():
            yield root
            continue
        for path in root.rglob("*"):
            if path.is_file() and not SKIP_DIRS & set(path.parts) \
                    and path.suffix.lower() not in SKIP_SUFFIXES:
                yield path


def _findings(text: str) -> list[str]:
    found = []
    for word in set(_WORD.findall(text)):
        if _h(word.lower()) in WORDS:
            found.append(f"a blocked name ({_h(word.lower())[:12]})")
    for match in _ADDR.finditer(text):
        a, b, c, d = match.groups()
        if _h(f"{a}.{b}") in PREFIXES:
            found.append(f"a blocked address scheme at offset {match.start()}")
        if d is not None and _h(f"{a}.{b}.{c}.{d}") in ADDRESSES:
            found.append(f"a blocked address at offset {match.start()}")
    return found


def test_the_lists_are_not_empty():
    assert WORDS and PREFIXES and ADDRESSES


def test_the_address_pattern_skips_oids():
    assert not list(_ADDR.finditer("1.3.6.1.4.1.2021.10.1.3.1"))
    assert not list(_ADDR.finditer('"20.1.0.94.0.0.1"'))
    assert [m.group(0) for m in _ADDR.finditer('"172.16.10.7/24", "172.16.10."')] == \
        ["172.16.10.7", "172.16.10."]


@pytest.mark.parametrize("path", sorted(_files()), ids=lambda p: str(p.relative_to(REPO)))
def test_nothing_shipped_names_the_homelab(path: Path):
    try:
        text = path.read_text()
    except UnicodeDecodeError:
        return
    assert not _findings(text), f"{path.relative_to(REPO)}: {_findings(text)}"
