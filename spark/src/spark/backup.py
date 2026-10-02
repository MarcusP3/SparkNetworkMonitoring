"""Backup and restore: Settings -> Backup, the nightly job, spark-restore.

A backup is one .tar.gz holding everything SPARK cannot rebuild:

  * ``spark.db`` -- copied with SQLite's online backup, so it is consistent
    while SPARK keeps running and writing;
  * ``secret.key`` -- without it every stored credential (SNMP, TrueNAS,
    Proxmox, the Discord webhook) is unreadable after a restore;
  * ``tls/`` -- the self-signed certificate, so browsers and anything that
    pinned it do not see a new one;
  * ``manifest.json`` -- what made it: SPARK's version, the schema version,
    when.

Because it holds ``secret.key``, a backup is a master key to every stored
credential. The nightly ones stay in ``data/backups/`` beside the files they
copy -- 0600, the same protection as the originals -- and the last
``KEEP`` are kept. Anything that leaves the box is encrypted with a
passphrase typed at download: scrypt for the key, then AES-256-GCM in 1 MiB
chunks (so a large database never has to fit in memory), each chunk's nonce
carrying its number and whether it is the last, so a reordered, truncated or
altered file fails to decrypt rather than restoring something else.

Restore is a command (cli.py, ``spark-restore``), run while SPARK is
stopped. It refuses while SPARK holds the data directory's instance lock.
"""

from __future__ import annotations

import fcntl
import io
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import struct
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterator

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from . import __version__

log = logging.getLogger(__name__)

JOB_ID = "backup:nightly"
STATE_KEY = "backup_state"
KEEP = 7
FORMAT = 1
DIR_NAME = "backups"
LOCK_NAME = ".spark.lock"

# spark-backup-20261002-040000.tar.gz -- what the nightly job writes and the
# only names the page will hand out.
NAME = re.compile(r"^spark-backup-(\d{8}-\d{6})\.tar\.gz$")

# Inside the tar, and nothing else is ever read back out of one.
MEMBERS = {"manifest.json", "spark.db", "secret.key", "tls/cert.pem", "tls/key.pem"}
REQUIRED = {"manifest.json", "spark.db", "secret.key"}
MAX_MEMBER = 4 * 1024 ** 3        # 4 GB: far beyond any real SPARK database

# The encrypted file: MAGIC, then the scrypt parameters and salt, then the
# nonce prefix, then the chunks. Everything before the chunks is the AAD of
# every chunk, so a header changed after the fact fails too.
MAGIC = b"SPARKBK1"
CHUNK = 1024 * 1024
TAG = 16
SCRYPT_LOG_N, SCRYPT_R, SCRYPT_P = 15, 8, 1     # 32 MB, ~0.1 s
HEADER = struct.Struct(">8sBBB16s7s")           # magic, log2 n, r, p, salt, nonce prefix
MIN_PASSPHRASE = 12


class BackupError(Exception):
    """Something a person should read: what is wrong with the file, or why not now."""


# --------------------------------------------------------------------------
# Where things are
# --------------------------------------------------------------------------


def backup_dir(data_dir: Path) -> Path:
    return data_dir / DIR_NAME


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


@dataclass(frozen=True)
class Stored:
    name: str
    made: datetime
    size: int


def listing(data_dir: Path) -> list[Stored]:
    """The nightly backups on disk, newest first."""
    folder = backup_dir(data_dir)
    out = []
    for path in folder.glob("spark-backup-*.tar.gz") if folder.is_dir() else []:
        match = NAME.match(path.name)
        if not match or not path.is_file():
            continue
        made = datetime.strptime(match.group(1), "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
        out.append(Stored(path.name, made, path.stat().st_size))
    return sorted(out, key=lambda s: s.made, reverse=True)


def stored_path(data_dir: Path, name: str) -> Path:
    """The path of one nightly backup by name, or BackupError. Only names
    the nightly job would write; never a path."""
    if not NAME.match(name or ""):
        raise BackupError("There is no backup by that name.")
    path = backup_dir(data_dir) / name
    if not path.is_file():
        raise BackupError("There is no backup by that name.")
    return path


# --------------------------------------------------------------------------
# The instance lock: how spark-restore knows SPARK is running
# --------------------------------------------------------------------------


class InstanceLock:
    """An flock on data/.spark.lock, held for the life of the server.

    Works across containers on one host that share the data directory (same
    kernel, same file). The server only warns if it cannot take it -- two
    SPARKs on one data directory is already wrong, but refusing to start
    over it would turn a stale state into an outage. Restore refuses.
    """

    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / LOCK_NAME
        self.fd: int | None = None

    def acquire(self) -> bool:
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self.fd = fd
        return True

    def release(self) -> None:
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None


# --------------------------------------------------------------------------
# Making one
# --------------------------------------------------------------------------


def _schema_version(db: Path) -> int | None:
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as con:
        try:
            row = con.execute("SELECT version FROM schema_version").fetchone()
        except sqlite3.DatabaseError:
            return None
    return int(row[0]) if row else None


def _copy_database(source: Path, target: Path) -> None:
    """SQLite's online backup: a consistent copy while SPARK writes."""
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30)
    dst = sqlite3.connect(target)
    try:
        with dst:
            src.backup(dst, pages=4096)
    finally:
        dst.close()
        src.close()


def _add_file(tar: tarfile.TarFile, name: str, path: Path) -> None:
    info = tar.gettarinfo(str(path), arcname=name)
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mode = 0o600
    with path.open("rb") as fh:
        tar.addfile(info, fh)


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size, info.mode = len(data), 0o600
    info.mtime = int(datetime.now(timezone.utc).timestamp())
    tar.addfile(info, io.BytesIO(data))


def make(data_dir: Path, target: Path, *, instance: str = "SPARK") -> dict:
    """Write a backup of `data_dir` to `target` (0600). Returns its manifest.
    Blocking: run it in a thread."""
    db = data_dir / "spark.db"
    key = data_dir / "secret.key"
    if not db.is_file() or not key.is_file():
        raise BackupError("There is nothing to back up yet: no database or no secret.key.")
    work = Path(tempfile.mkdtemp(prefix=".work-", dir=_private_dir(backup_dir(data_dir))))
    try:
        copy = work / "spark.db"
        _copy_database(db, copy)
        manifest = {
            "format": FORMAT,
            "spark_version": __version__,
            "schema_version": _schema_version(copy),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "instance": instance,
        }
        partial = target.with_name(f".{target.name}.partial")
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz") as tar:
            _add_bytes(tar, "manifest.json", json.dumps(manifest, indent=2).encode())
            _add_file(tar, "spark.db", copy)
            _add_file(tar, "secret.key", key)
            for name in ("cert.pem", "key.pem"):
                path = data_dir / "tls" / name
                if path.is_file():
                    _add_file(tar, f"tls/{name}", path)
        os.replace(partial, target)
        return manifest
    finally:
        shutil.rmtree(work, ignore_errors=True)


def nightly(data_dir: Path, *, instance: str = "SPARK", keep: int = KEEP,
            now: datetime | None = None) -> tuple[Stored, list[str]]:
    """Make tonight's backup in data/backups and prune to the last `keep`.
    Returns it and the names pruned. Blocking."""
    now = now or datetime.now(timezone.utc)
    folder = _private_dir(backup_dir(data_dir))
    name = f"spark-backup-{now:%Y%m%d-%H%M%S}.tar.gz"
    make(data_dir, folder / name, instance=instance)
    pruned = []
    for old in listing(data_dir)[keep:]:
        try:
            (folder / old.name).unlink()
            pruned.append(old.name)
        except OSError as exc:
            log.warning("Could not remove old backup %s (%s)", old.name, exc)
    made = next(s for s in listing(data_dir) if s.name == name)
    return made, pruned


async def run_nightly(config) -> None:  # type: ignore[no-untyped-def]
    """The scheduler's job. Records how it went for the page; never raises."""
    import asyncio

    from .db import save_setting, session_scope

    state: dict = {"at": datetime.now(timezone.utc).isoformat()}
    try:
        made, pruned = await asyncio.to_thread(
            nightly, config.app.data_dir, instance=config.app.instance_name)
        state.update(ok=True, name=made.name, size=made.size, pruned=len(pruned))
        log.info("Nightly backup %s (%d bytes), %d old removed", made.name, made.size, len(pruned))
    except Exception as exc:  # noqa: BLE001 - the scheduler must keep running
        log.exception("Nightly backup failed")
        state.update(ok=False, error=str(exc)[:500])
    try:
        async with session_scope() as session:
            await save_setting(session, STATE_KEY, state)
    except Exception:  # noqa: BLE001
        log.exception("Could not record the nightly backup's result")


# --------------------------------------------------------------------------
# Encryption
# --------------------------------------------------------------------------


def check_passphrase(passphrase: str, again: str | None = None) -> str:
    if again is not None and passphrase != again:
        raise BackupError("The two passphrases do not match.")
    if len(passphrase or "") < MIN_PASSPHRASE:
        raise BackupError(f"Use a passphrase of at least {MIN_PASSPHRASE} characters. "
                          "It is the only thing protecting every stored credential in the file.")
    return passphrase


def _key(passphrase: str, salt: bytes, log_n: int, r: int, p: int) -> bytes:
    if not (10 <= log_n <= 20 and 1 <= r <= 16 and 1 <= p <= 4):
        raise BackupError("This is not a SPARK backup, or it is damaged.")
    return Scrypt(salt=salt, length=32, n=1 << log_n, r=r, p=p).derive(passphrase.encode())


def _nonce(prefix: bytes, counter: int, last: bool) -> bytes:
    return prefix + struct.pack(">I", counter) + (b"\x01" if last else b"\x00")


def encrypt(source: BinaryIO, target: BinaryIO, passphrase: str) -> None:
    salt, prefix = secrets.token_bytes(16), secrets.token_bytes(7)
    header = HEADER.pack(MAGIC, SCRYPT_LOG_N, SCRYPT_R, SCRYPT_P, salt, prefix)
    aead = AESGCM(_key(passphrase, salt, SCRYPT_LOG_N, SCRYPT_R, SCRYPT_P))
    target.write(header)
    counter = 0
    chunk = source.read(CHUNK)
    while True:
        following = source.read(CHUNK)
        last = not following
        target.write(aead.encrypt(_nonce(prefix, counter, last), chunk, header))
        if last:
            return
        counter += 1
        if counter >= 2 ** 32:
            raise BackupError("Too large to encrypt.")
        chunk = following


def is_encrypted(path: Path) -> bool:
    with path.open("rb") as fh:
        return fh.read(len(MAGIC)) == MAGIC


def decrypt(source: BinaryIO, target: BinaryIO, passphrase: str) -> None:
    header = source.read(HEADER.size)
    if len(header) != HEADER.size:
        raise BackupError("This is not a SPARK backup, or it is damaged.")
    magic, log_n, r, p, salt, prefix = HEADER.unpack(header)
    if magic != MAGIC:
        raise BackupError("This is not an encrypted SPARK backup.")
    aead = AESGCM(_key(passphrase, salt, log_n, r, p))
    counter = 0
    block = source.read(CHUNK + TAG)
    while True:
        if len(block) < TAG:
            raise BackupError("The backup is cut short: it ends before its last part.")
        following = source.read(CHUNK + TAG)
        last = not following
        try:
            plain = aead.decrypt(_nonce(prefix, counter, last), block, header)
        except InvalidTag:
            if counter == 0:
                raise BackupError("Wrong passphrase, or the file is damaged.") from None
            raise BackupError("The backup is damaged or cut short (part "
                              f"{counter + 1} does not check out).") from None
        target.write(plain)
        if last:
            return
        counter += 1
        block = following


def encrypted_copy(source: Path, data_dir: Path, passphrase: str) -> Path:
    """An encrypted copy of `source` in a private temporary file under the
    data directory (the container's root may be read-only). The caller
    deletes it. Blocking."""
    folder = _private_dir(backup_dir(data_dir))
    fd, name = tempfile.mkstemp(prefix=".download-", suffix=".sparkbackup", dir=folder)
    try:
        with os.fdopen(fd, "wb") as out, source.open("rb") as src:
            encrypt(src, out, passphrase)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise
    return Path(name)


def fresh_encrypted(data_dir: Path, passphrase: str, *, instance: str = "SPARK") -> Path:
    """A backup made now, encrypted. Blocking; the caller deletes it."""
    folder = _private_dir(backup_dir(data_dir))
    fd, plain = tempfile.mkstemp(prefix=".fresh-", suffix=".tar.gz", dir=folder)
    os.close(fd)
    try:
        make(data_dir, Path(plain), instance=instance)
        return encrypted_copy(Path(plain), data_dir, passphrase)
    finally:
        Path(plain).unlink(missing_ok=True)


def sweep_temporary(data_dir: Path) -> None:
    """Leftovers from a download or nightly that was interrupted."""
    folder = backup_dir(data_dir)
    for pattern in (".download-*", ".fresh-*", ".*.partial"):
        for path in folder.glob(pattern) if folder.is_dir() else []:
            path.unlink(missing_ok=True)
    for path in folder.glob(".work-*") if folder.is_dir() else []:
        shutil.rmtree(path, ignore_errors=True)


def download_name(instance: str, made: datetime | None = None) -> str:
    made = made or datetime.now(timezone.utc)
    slug = re.sub(r"[^a-z0-9]+", "-", (instance or "spark").lower()).strip("-") or "spark"
    return f"{slug}-backup-{made:%Y%m%d-%H%M%S}.sparkbackup"


# --------------------------------------------------------------------------
# Reading one back (spark-restore)
# --------------------------------------------------------------------------


@dataclass
class Opened:
    manifest: dict
    folder: Path        # spark.db, secret.key, tls/... extracted here


def _members(tar: tarfile.TarFile) -> Iterator[tarfile.TarInfo]:
    seen = set()
    for info in tar:
        name = info.name
        if name not in MEMBERS or not info.isfile():
            raise BackupError(f"The backup holds something it should not ({name!r}); "
                              "it was not made by SPARK, or it was altered.")
        if name in seen:
            raise BackupError(f"The backup holds {name!r} twice.")
        if info.size > MAX_MEMBER:
            raise BackupError(f"{name!r} in the backup is impossibly large.")
        seen.add(name)
        yield info
    missing = REQUIRED - seen
    if missing:
        raise BackupError(f"The backup is missing {', '.join(sorted(missing))}.")


def open_backup(path: Path, into: Path, *, passphrase: str | None,
                newest_schema: int) -> Opened:
    """Check a backup and unpack it into `into`. Raises BackupError with the
    reason. Never writes outside `into`: only the known member names are
    accepted, each written by name."""
    into.mkdir(mode=0o700, parents=True, exist_ok=True)
    tar_path = path
    if is_encrypted(path):
        if passphrase is None:
            raise BackupError("This backup is encrypted: it needs its passphrase.")
        tar_path = into / ".backup.tar.gz"
        with path.open("rb") as src, tar_path.open("wb") as out:
            decrypt(src, out, passphrase)
    try:
        with tarfile.open(tar_path, mode="r:gz") as tar:
            for info in _members(tar):
                target = into / info.name
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with tar.extractfile(info) as src, open(target, "wb") as out:  # type: ignore[union-attr]
                    shutil.copyfileobj(src, out)
                target.chmod(0o600)
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise BackupError(f"This is not a SPARK backup, or it is damaged ({exc}).") from None
    finally:
        if tar_path != path:
            tar_path.unlink(missing_ok=True)

    try:
        manifest = json.loads((into / "manifest.json").read_text())
    except ValueError:
        raise BackupError("The backup's manifest is not readable.") from None
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise BackupError("This backup was made by a SPARK this one does not understand.")
    db = into / "spark.db"
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as con:
        try:
            ok = con.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.DatabaseError as exc:
            raise BackupError(f"The database in the backup is damaged ({exc}).") from None
    if not ok or ok[0] != "ok":
        raise BackupError(f"The database in the backup is damaged ({ok[0] if ok else '?'}).")
    schema = _schema_version(db)
    if schema is None:
        raise BackupError("The database in the backup has no schema version.")
    if schema > newest_schema:
        raise BackupError(
            f"This backup is from a newer SPARK (database version {schema}; this one "
            f"understands up to {newest_schema}). Update SPARK first, then restore.")
    if len((into / "secret.key").read_bytes().strip()) < 16:
        raise BackupError("The secret.key in the backup is not a SPARK key.")
    return Opened(manifest=manifest, folder=into)


RESTORED = ("spark.db", "secret.key", "tls/cert.pem", "tls/key.pem")
SET_ASIDE = ("spark.db", "spark.db-wal", "spark.db-shm", "secret.key", "tls")


def install(opened: Opened, data_dir: Path, *, now: datetime | None = None) -> Path:
    """Move the current data aside to data/pre-restore-<when>/ and put the
    backup's files in its place. Returns where the old data went."""
    now = now or datetime.now(timezone.utc)
    aside = data_dir / f"pre-restore-{now:%Y%m%d-%H%M%S}"
    aside.mkdir(mode=0o700)
    for name in SET_ASIDE:
        path = data_dir / name
        if path.exists():
            os.replace(path, aside / name)
    for name in RESTORED:
        src = opened.folder / name
        if src.is_file():
            target = data_dir / name
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.move(str(src), target)
            target.chmod(0o600)
    return aside
