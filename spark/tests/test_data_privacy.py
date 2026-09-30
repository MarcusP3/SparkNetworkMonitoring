"""Files under data/ are readable by SPARK alone (review finding #31).

`secret.key` always was; the database beside it was created with the
process umask, 0644 in the image, so any local user on the VM could read the
network map and the password hash.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from spark.config import Config
from spark.main import create_app


def _config() -> Config:
    tmp = Path(tempfile.mkdtemp(prefix="spark-private-"))
    config = Config.model_validate(
        {"app": {"data_dir": str(tmp / "data"), "log_level": "WARNING"},
         "network": {"subnets": []}}
    )
    config.app.data_dir.mkdir(parents=True, exist_ok=True)
    return config


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class TestPrivateFiles:
    def test_a_fresh_database_is_private(self):
        config = _config()
        with TestClient(create_app(config)):
            data = config.app.data_dir
            assert _mode(data / "spark.db") == 0o600
            assert _mode(data / "secret.key") == 0o600
            for side in ("spark.db-wal", "spark.db-shm"):
                if (data / side).exists():
                    assert _mode(data / side) == 0o600, side

    def test_a_database_from_an_older_version_is_made_private_at_start(self):
        config = _config()
        with TestClient(create_app(config)):
            pass
        db = config.app.data_dir / "spark.db"
        db.chmod(0o644)                       # as the old image left it
        assert _mode(db) == 0o644
        with TestClient(create_app(config)):
            assert _mode(db) == 0o600

    def test_the_umask_covers_anything_written_later(self):
        config = _config()
        with TestClient(create_app(config)):
            probe = config.app.data_dir / "later"
            probe.write_bytes(b"")
            assert _mode(probe) == 0o600
            assert os.umask(0o077) == 0o077   # what the app set; put straight back
