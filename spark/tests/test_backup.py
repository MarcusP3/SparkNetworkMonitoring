"""Backup and restore (backup.py, Settings -> Backup, spark-restore).

What matters most, and is tested for real rather than by mocks: a backup
made while SPARK runs restores to a working SPARK whose stored credentials
still open; a download can only be opened with its passphrase; and a file
that is wrong in any way -- wrong passphrase, cut short, altered, from a
newer SPARK, holding anything but SPARK's own files -- changes nothing.
"""

from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from spark import backup
from spark.config import Config
from spark.main import create_app

PASSWORD = "correct horse battery"
PHRASE = "a long backup passphrase"


def _config(data: Path | None = None) -> Config:
    data = data or Path(tempfile.mkdtemp(prefix="spark-backup-")) / "data"
    config = Config.model_validate({"app": {"data_dir": str(data), "log_level": "WARNING"},
                                    "network": {"subnets": []}})
    config.app.data_dir.mkdir(parents=True, exist_ok=True)
    return config


def _site(config: Config) -> TestClient:
    client = TestClient(create_app(config), follow_redirects=False)
    client.__enter__()
    client.post("/setup", data={"setup_code": client.app.state.setup_code, "username": "admin",
                                "password": PASSWORD, "password_confirm": PASSWORD})
    return client


def _close(client: TestClient) -> None:
    client.__exit__(None, None, None)


def _seal_something(client: TestClient) -> None:
    """A stored, sealed credential, to prove secret.key came along."""
    client.post("/settings/credentials", data={"kind": "truenas", "name": "nas",
                                               "device_id": "", "host": "127.0.0.1:9",
                                               "api_key": "1-secret-api-key"})


def _sealed_key(config: Config) -> str:
    from spark.vault import vault_for
    with sqlite3.connect(config.app.db_path) as con:
        sealed = con.execute("select key_sealed from api_credential").fetchone()[0]
    return vault_for(config).open(sealed)


@pytest.fixture
def running():
    config = _config()
    client = _site(config)
    _seal_something(client)
    yield config, client
    _close(client)


# --------------------------------------------------------------------------
# Making one
# --------------------------------------------------------------------------


class TestMake:
    def test_what_is_in_it(self, running):
        config, _ = running
        made, pruned = backup.nightly(config.app.data_dir, instance="Lab")
        assert pruned == [] and backup.NAME.match(made.name)
        path = backup.backup_dir(config.app.data_dir) / made.name
        assert path.stat().st_mode & 0o777 == 0o600
        with tarfile.open(path) as tar:
            names = sorted(tar.getnames())
            manifest = json.loads(tar.extractfile("manifest.json").read())
        assert names == ["manifest.json", "secret.key", "spark.db", "tls/cert.pem", "tls/key.pem"] \
            or names == ["manifest.json", "secret.key", "spark.db"]
        from spark.db import CURRENT_VERSION
        assert manifest["format"] == 1 and manifest["schema_version"] == CURRENT_VERSION
        assert manifest["instance"] == "Lab"

    def test_made_while_spark_writes(self, running):
        """SQLite's online backup, not a file copy: consistent while live."""
        config, client = running
        made, _ = backup.nightly(config.app.data_dir)
        client.post("/settings/credentials", data={"kind": "truenas", "name": "nas2",
                                                   "device_id": "", "host": "127.0.0.1:9",
                                                   "api_key": "1-other"})
        out = Path(tempfile.mkdtemp())
        opened = backup.open_backup(backup.backup_dir(config.app.data_dir) / made.name, out,
                                    passphrase=None, newest_schema=10 ** 6)
        with sqlite3.connect(opened.folder / "spark.db") as con:
            assert con.execute("select count(*) from api_credential").fetchone()[0] == 1

    def test_keeps_the_last_seven(self, running):
        config, _ = running
        start = datetime(2026, 10, 1, 4, 0, tzinfo=timezone.utc)
        for day in range(9):
            backup.nightly(config.app.data_dir, now=start + timedelta(days=day))
        names = [s.name for s in backup.listing(config.app.data_dir)]
        assert len(names) == 7 and names[0] == "spark-backup-20261009-040000.tar.gz"
        assert names[-1] == "spark-backup-20261003-040000.tar.gz"

    def test_only_its_own_names_are_handed_out(self, running):
        config, _ = running
        for name in ("../secret.key", "spark.db", "spark-backup-1.tar.gz", "",
                     "spark-backup-20261001-040000.tar.gz"):
            with pytest.raises(backup.BackupError):
                backup.stored_path(config.app.data_dir, name)

    def test_nothing_to_back_up(self):
        with pytest.raises(backup.BackupError, match="nothing to back up"):
            backup.make(Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp()) / "x.tar.gz")


# --------------------------------------------------------------------------
# Encryption
# --------------------------------------------------------------------------


def _round(data: bytes, phrase: str = PHRASE, opener: str = PHRASE) -> bytes:
    sealed, out = io.BytesIO(), io.BytesIO()
    backup.encrypt(io.BytesIO(data), sealed, phrase)
    backup.decrypt(io.BytesIO(sealed.getvalue()), out, opener)
    return out.getvalue()


class TestEncryption:
    @pytest.mark.parametrize("size", [0, 1, backup.CHUNK - 1, backup.CHUNK, backup.CHUNK + 1,
                                      3 * backup.CHUNK + 17])
    def test_round_trip_at_every_chunk_edge(self, size):
        data = bytes(range(256)) * (size // 256) + bytes(size % 256)
        assert _round(data) == data

    def test_wrong_passphrase(self):
        with pytest.raises(backup.BackupError, match="Wrong passphrase"):
            _round(b"x" * 100, opener="not the passphrase")

    def _sealed(self, size: int = 2 * backup.CHUNK + 5) -> bytes:
        out = io.BytesIO()
        backup.encrypt(io.BytesIO(b"y" * size), out, PHRASE)
        return out.getvalue()

    def test_cut_short_at_a_chunk_edge_fails(self):
        sealed = self._sealed()
        cut = backup.HEADER.size + 2 * (backup.CHUNK + backup.TAG)
        with pytest.raises(backup.BackupError, match="damaged or cut short"):
            backup.decrypt(io.BytesIO(sealed[:cut]), io.BytesIO(), PHRASE)

    def test_cut_short_anywhere_fails(self):
        sealed = self._sealed()
        with pytest.raises(backup.BackupError):
            backup.decrypt(io.BytesIO(sealed[:-1]), io.BytesIO(), PHRASE)
        with pytest.raises(backup.BackupError):
            backup.decrypt(io.BytesIO(sealed[:backup.HEADER.size]), io.BytesIO(), PHRASE)

    def test_one_flipped_byte_fails(self):
        sealed = bytearray(self._sealed())
        sealed[backup.HEADER.size + backup.CHUNK + 40] ^= 1
        with pytest.raises(backup.BackupError):
            backup.decrypt(io.BytesIO(bytes(sealed)), io.BytesIO(), PHRASE)

    def test_an_altered_header_fails(self):
        sealed = bytearray(self._sealed())
        sealed[20] ^= 1          # inside the salt
        with pytest.raises(backup.BackupError):
            backup.decrypt(io.BytesIO(bytes(sealed)), io.BytesIO(), PHRASE)

    def test_two_encryptions_differ(self):
        assert self._sealed(10) != self._sealed(10), "fresh salt and nonce each time"

    @pytest.mark.parametrize("phrase, again, why", [
        ("short", "short", "at least 12"),
        (PHRASE, PHRASE + "x", "do not match"),
    ])
    def test_passphrase_rules(self, phrase, again, why):
        with pytest.raises(backup.BackupError, match=why):
            backup.check_passphrase(phrase, again)


# --------------------------------------------------------------------------
# Reading one back
# --------------------------------------------------------------------------


def _tar(path: Path, members: dict[str, bytes]) -> Path:
    with tarfile.open(path, "w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


class TestOpen:
    def _good(self, config: Config) -> dict[str, bytes]:
        made, _ = backup.nightly(config.app.data_dir)
        out = Path(tempfile.mkdtemp())
        with tarfile.open(backup.backup_dir(config.app.data_dir) / made.name) as tar:
            return {n: tar.extractfile(n).read() for n in tar.getnames()}

    @pytest.mark.parametrize("name", ["../escape", "/etc/passwd", "spark.db/../../x", "extra.txt",
                                      "tls/other.pem"])
    def test_anything_but_its_own_files_is_refused(self, running, name):
        config, _ = running
        members = {**self._good(config), name: b"x"}
        path = _tar(Path(tempfile.mkdtemp()) / "evil.tar.gz", members)
        out = Path(tempfile.mkdtemp()) / "out"
        with pytest.raises(backup.BackupError, match="should not"):
            backup.open_backup(path, out, passphrase=None, newest_schema=10 ** 6)
        assert not (out.parent / "escape").exists()

    def test_a_link_is_refused(self, running):
        config, _ = running
        path = Path(tempfile.mkdtemp()) / "link.tar.gz"
        with tarfile.open(path, "w:gz") as tar:
            info = tarfile.TarInfo("secret.key")
            info.type, info.linkname = tarfile.SYMTYPE, "/etc/shadow"
            tar.addfile(info)
        with pytest.raises(backup.BackupError, match="should not"):
            backup.open_backup(path, Path(tempfile.mkdtemp()), passphrase=None, newest_schema=99)

    def test_missing_parts_are_refused(self, running):
        config, _ = running
        members = self._good(config)
        del members["secret.key"]
        path = _tar(Path(tempfile.mkdtemp()) / "half.tar.gz", members)
        with pytest.raises(backup.BackupError, match="missing secret.key"):
            backup.open_backup(path, Path(tempfile.mkdtemp()), passphrase=None, newest_schema=99)

    def test_a_newer_spark_is_refused(self, running):
        config, _ = running
        made, _ = backup.nightly(config.app.data_dir)
        from spark.db import CURRENT_VERSION
        with pytest.raises(backup.BackupError, match="newer SPARK"):
            backup.open_backup(backup.backup_dir(config.app.data_dir) / made.name,
                               Path(tempfile.mkdtemp()), passphrase=None,
                               newest_schema=CURRENT_VERSION - 1)

    def test_a_damaged_database_is_refused(self, running):
        config, _ = running
        members = self._good(config)
        db = bytearray(members["spark.db"])
        db[5000:9000] = b"\xff" * 4000
        members["spark.db"] = bytes(db)
        path = _tar(Path(tempfile.mkdtemp()) / "bad.tar.gz", members)
        with pytest.raises(backup.BackupError, match="damaged"):
            backup.open_backup(path, Path(tempfile.mkdtemp()), passphrase=None, newest_schema=10 ** 6)

    def test_not_a_backup_at_all(self):
        path = Path(tempfile.mkdtemp()) / "x.tar.gz"
        path.write_bytes(b"hello")
        with pytest.raises(backup.BackupError, match="not a SPARK backup"):
            backup.open_backup(path, Path(tempfile.mkdtemp()), passphrase=None, newest_schema=99)

    def test_an_encrypted_one_needs_its_passphrase(self, running):
        config, _ = running
        path = backup.fresh_encrypted(config.app.data_dir, PHRASE)
        with pytest.raises(backup.BackupError, match="needs its passphrase"):
            backup.open_backup(path, Path(tempfile.mkdtemp()), passphrase=None, newest_schema=10 ** 6)
        with pytest.raises(backup.BackupError, match="Wrong passphrase"):
            backup.open_backup(path, Path(tempfile.mkdtemp()), passphrase="nope nope nope",
                               newest_schema=10 ** 6)


# --------------------------------------------------------------------------
# spark-restore, end to end
# --------------------------------------------------------------------------


def _restore(monkeypatch, capsys, config: Config, path: Path, *args: str,  # type: ignore[no-untyped-def]
             stdin: str = "") -> tuple[int, str, str]:
    from spark.cli import restore_main
    monkeypatch.setenv("SPARK__APP__DATA_DIR", str(config.app.data_dir))
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    code = restore_main([str(path), *args])
    out = capsys.readouterr()
    return code, out.out, out.err


class TestRestore:
    def test_a_download_restores_to_a_working_spark(self, monkeypatch, capsys):
        config = _config()
        client = _site(config)
        _seal_something(client)
        response = client.post("/settings/backup/download", data={
            "which": "now", "passphrase": PHRASE, "passphrase_confirm": PHRASE})
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/octet-stream"
        assert "attachment" in response.headers["content-disposition"]
        assert ".sparkbackup" in response.headers["content-disposition"]
        assert response.content.startswith(backup.MAGIC)
        _close(client)
        assert not list(backup.backup_dir(config.app.data_dir).glob(".download-*")), \
            "the temporary encrypted copy is deleted after sending"

        # A brand-new machine: an empty data directory.
        fresh = _config()
        file = fresh.app.data_dir / "downloaded.sparkbackup"
        file.write_bytes(response.content)
        code, out, err = _restore(monkeypatch, capsys, fresh, file, "--passphrase-stdin", "--yes",
                                  stdin=PHRASE + "\n")
        assert code == 0, err
        assert "Restored." in out
        assert _sealed_key(fresh) == "1-secret-api-key", "secret.key came along"
        site = TestClient(create_app(fresh), follow_redirects=False)
        with site:
            response = site.post("/login", data={"username": "admin", "password": PASSWORD})
            assert response.status_code == 303 and response.headers["location"] == "/"
            assert site.get("/settings/credentials").status_code == 200

    def test_the_current_data_is_kept_aside(self, monkeypatch, capsys, running):
        config, client = running
        made, _ = backup.nightly(config.app.data_dir)
        _close(client)
        before = config.app.db_path.read_bytes()
        code, out, err = _restore(monkeypatch, capsys, config,
                                  backup.backup_dir(config.app.data_dir) / made.name, "--yes")
        assert code == 0, err
        (aside,) = list(config.app.data_dir.glob("pre-restore-*"))
        assert (aside / "spark.db").read_bytes() == before
        assert (aside / "secret.key").is_file()
        assert config.app.db_path.stat().st_mode & 0o777 == 0o600
        assert (config.app.data_dir / "secret.key").stat().st_mode & 0o777 == 0o600

    def test_refused_while_spark_runs(self, monkeypatch, capsys, running):
        config, _ = running
        made, _ = backup.nightly(config.app.data_dir)
        before = config.app.db_path.read_bytes()
        code, _, err = _restore(monkeypatch, capsys, config,
                                backup.backup_dir(config.app.data_dir) / made.name, "--yes")
        assert code == 1 and "SPARK is running" in err
        assert config.app.db_path.read_bytes() == before
        assert not list(config.app.data_dir.glob("pre-restore-*"))

    def test_a_bad_passphrase_changes_nothing(self, monkeypatch, capsys, running):
        config, client = running
        path = backup.fresh_encrypted(config.app.data_dir, PHRASE)
        _close(client)
        before = config.app.db_path.read_bytes()
        code, _, err = _restore(monkeypatch, capsys, config, path, "--passphrase-stdin", "--yes",
                                stdin="the wrong passphrase\n")
        assert code == 1 and "Wrong passphrase" in err and "Nothing was changed" in err
        assert config.app.db_path.read_bytes() == before
        assert not list(config.app.data_dir.glob("pre-restore-*"))
        assert not list(config.app.data_dir.glob(".restore-*")), "its work folder is cleaned up"

    def test_saying_no_changes_nothing(self, monkeypatch, capsys, running):
        config, client = running
        made, _ = backup.nightly(config.app.data_dir)
        _close(client)
        monkeypatch.setattr("builtins.input", lambda prompt="": "n")
        code, out, err = _restore(monkeypatch, capsys, config,
                                  backup.backup_dir(config.app.data_dir) / made.name)
        assert code == 1 and "Nothing was changed" in err
        assert not list(config.app.data_dir.glob("pre-restore-*"))

    def test_no_such_file(self, monkeypatch, capsys):
        config = _config()
        code, _, err = _restore(monkeypatch, capsys, config, config.app.data_dir / "nope")
        assert code == 1 and "no file" in err


# --------------------------------------------------------------------------
# The page and the job
# --------------------------------------------------------------------------


class TestPage:
    def test_the_page(self, running):
        config, client = running
        page = client.get("/settings/backup").text
        assert "<h2>Backup</h2>" in page and "none yet" in page
        assert '<option value="now">Make one now</option>' in page
        assert "docker compose run --rm spark spark-restore" in page
        backup.nightly(config.app.data_dir)
        page = client.get("/settings/backup").text
        assert "<h3 class=\"sub-title\">Nightly backups</h3>" in page
        assert 'value="spark-backup-' in page

    def test_the_menu_entry(self, running):
        _, client = running
        assert 'href="/settings/backup"' in client.get("/settings").text

    @pytest.mark.parametrize("data, why", [
        ({"which": "now", "passphrase": "short", "passphrase_confirm": "short"}, "at least 12"),
        ({"which": "now", "passphrase": PHRASE, "passphrase_confirm": PHRASE + "!"}, "do not match"),
        ({"which": "../spark.db", "passphrase": PHRASE, "passphrase_confirm": PHRASE},
         "no backup by that name"),
    ])
    def test_refusals_say_why_and_send_nothing(self, running, data, why):
        _, client = running
        response = client.post("/settings/backup/download", data=data)
        assert response.status_code == 400 and why in response.text
        assert PHRASE not in response.text
        assert not response.content.startswith(backup.MAGIC)

    def test_a_nightly_one_downloads_encrypted(self, running):
        config, client = running
        made, _ = backup.nightly(config.app.data_dir)
        response = client.post("/settings/backup/download", data={
            "which": made.name, "passphrase": PHRASE, "passphrase_confirm": PHRASE})
        assert response.status_code == 200 and response.content.startswith(backup.MAGIC)
        plain = io.BytesIO()
        backup.decrypt(io.BytesIO(response.content), plain, PHRASE)
        assert plain.getvalue() == (backup.backup_dir(config.app.data_dir) / made.name).read_bytes()

    def test_a_download_is_a_security_event(self, running):
        config, client = running
        client.post("/settings/backup/download", data={
            "which": "now", "passphrase": PHRASE, "passphrase_confirm": PHRASE})
        with sqlite3.connect(config.app.db_path) as con:
            kinds = [r[0] for r in con.execute("select kind from notification")]
        assert "security_backup" in kinds

    def test_signed_out_gets_nothing(self, running):
        config, _ = running
        bare = TestClient(create_app(config), follow_redirects=False)
        response = bare.post("/settings/backup/download", data={
            "which": "now", "passphrase": PHRASE, "passphrase_confirm": PHRASE})
        assert response.status_code in (303, 401, 403)
        assert not response.content.startswith(backup.MAGIC)


class TestJob:
    def test_scheduled_at_four(self, running):
        from spark import scheduler
        job = scheduler._scheduler.get_job(backup.JOB_ID)
        assert job is not None and job.name == "Nightly backup"
        fields = {f.name: str(f) for f in job.trigger.fields}
        assert (fields["hour"], fields["minute"]) == ("4", "0")

    def test_the_job_records_how_it_went(self, running):
        config, client = running
        asyncio.run(backup.run_nightly(config))
        page = client.get("/settings/backup").text
        assert '<span class="pill ok dot">ok</span> Last night' in page

    def test_a_failure_is_shown_not_raised(self, running, monkeypatch):
        config, client = running

        def boom(*a, **k):  # type: ignore[no-untyped-def]
            raise OSError("disk full")
        monkeypatch.setattr(backup, "nightly", boom)
        asyncio.run(backup.run_nightly(config))
        page = client.get("/settings/backup").text
        assert '<span class="pill bad">failed</span>' in page and "disk full" in page

    def test_leftovers_are_swept_at_start(self):
        config = _config()
        folder = backup.backup_dir(config.app.data_dir)
        folder.mkdir()
        (folder / ".download-abc.sparkbackup").write_bytes(b"x")
        (folder / ".spark-backup-x.tar.gz.partial").write_bytes(b"x")
        client = _site(config)
        try:
            assert list(folder.iterdir()) == []
        finally:
            _close(client)
