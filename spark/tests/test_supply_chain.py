"""Tests that the dependency lock has not quietly rotted.

A hash-pinned lock protects the build right up until someone adds a dependency
to `pyproject.toml` and forgets to regenerate it. What happens then is not a
clean failure: `pip install .` in a developer's venv picks the new package up
fine and every test passes, while the Docker build -- the only place that reads
the lock -- fails on a missing requirement, or worse, silently keeps running the
old image. The gap between "works on my machine" and "what actually ships" is
exactly what a lock exists to close, so it is worth a test.

These assertions are about the shape of the files rather than their contents.
None of them needs a network.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
LOCK = ROOT / "requirements.lock"
BUILD_LOCK = ROOT / "requirements-build.lock"
DOCKERFILE = ROOT / "Dockerfile"

# PEP 508: "fastapi>=0.115" -> "fastapi"; "uvicorn[standard]>=0.32" -> "uvicorn"
_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def normalise(name: str) -> str:
    """PyPI treats '-', '_' and case as equivalent; comparisons must too."""
    return re.sub(r"[-_.]+", "-", name).lower()


def direct_dependencies() -> set[str]:
    data = tomllib.loads(PYPROJECT.read_text())
    return {
        normalise(_NAME.match(spec).group(1))
        for spec in data["project"]["dependencies"]
        if _NAME.match(spec)
    }


def locked_packages(path: Path) -> dict[str, str]:
    """{name: version} for every pinned entry in a lock file."""
    found = {}
    for line in path.read_text().splitlines():
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\\]+)", line)
        if match:
            found[normalise(match.group(1))] = match.group(2)
    return found


class TestLockCoversPyproject:
    def test_every_declared_dependency_is_locked(self):
        missing = direct_dependencies() - set(locked_packages(LOCK))
        assert not missing, (
            f"{sorted(missing)} are in pyproject.toml but not in requirements.lock. "
            "Regenerate it: uv pip compile pyproject.toml --generate-hashes "
            "--python-version 3.12 -o requirements.lock"
        )

    def test_the_lock_is_not_empty(self):
        # A truncated or half-written lock would make every other test here
        # pass vacuously.
        assert len(locked_packages(LOCK)) > 20

    def test_the_build_toolchain_is_locked_too(self):
        # Without this, `pip install .` fetches hatchling unverified at build
        # time and the runtime lock guards a door with no wall around it.
        assert "hatchling" in locked_packages(BUILD_LOCK)


class TestEverythingIsPinnedAndHashed:
    def _entries(self, path: Path) -> list[str]:
        # One logical requirement per entry; continuations end with a backslash.
        text = path.read_text().replace("\\\n", " ")
        return [
            line for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

    def test_no_entry_is_left_unpinned(self):
        for path in (LOCK, BUILD_LOCK):
            for entry in self._entries(path):
                name = entry.split()[0]
                assert "==" in name, f"{name} in {path.name} is not pinned to a version"

    def test_every_entry_carries_a_hash(self):
        for path in (LOCK, BUILD_LOCK):
            for entry in self._entries(path):
                assert "--hash=sha256:" in entry, (
                    f"{entry.split()[0]} in {path.name} has no hash. "
                    "pip --require-hashes refuses a file where any entry lacks one, "
                    "so this breaks the build rather than weakening it."
                )

    def test_hashes_are_the_right_shape(self):
        for path in (LOCK, BUILD_LOCK):
            for digest in re.findall(r"--hash=sha256:(\S+)", path.read_text()):
                assert re.fullmatch(r"[0-9a-f]{64}", digest), f"malformed hash {digest}"


class TestDockerfileUsesTheLock:
    def test_the_build_requires_hashes(self):
        body = DOCKERFILE.read_text()
        assert body.count("--require-hashes") >= 2, (
            "both the runtime and build locks must be installed with --require-hashes"
        )

    def test_the_app_is_installed_without_re_resolving_its_dependencies(self):
        body = DOCKERFILE.read_text()
        # A bare `pip install .` would resolve from PyPI again and undo the
        # lock; --no-build-isolation keeps the build backend from being fetched
        # unverified.
        assert "--no-deps" in body and "--no-build-isolation" in body
        assert not re.search(r"^RUN pip install --no-cache-dir \.$", body, re.M)


class TestRemovedDependencies:
    def test_itsdangerous_is_gone(self):
        """It was declared, never imported, and shipped in every image.

        Sessions are database-backed tokens rather than signed cookies, so
        nothing ever used it. A dependency that does nothing is pure attack
        surface.
        """
        assert "itsdangerous" not in direct_dependencies()
        assert "itsdangerous" not in locked_packages(LOCK)
