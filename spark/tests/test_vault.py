"""Encrypted credential storage.

The claims `vault.py` makes, each pinned: ciphertext only in the database, a
restored database without its key decrypts nothing and says why, tampering is
refused, and equal secrets do not produce equal ciphertext.
"""

from __future__ import annotations

import base64
import os
import stat
import tempfile
from pathlib import Path

import pytest
from cryptography.fernet import Fernet, InvalidToken

from spark.config import Config
from spark.vault import SecretUnavailable, Vault, vault_for

KEY = "k" * 64
OTHER_KEY = "z" * 64


class TestRoundTrip:
    def test_a_sealed_value_opens_to_itself(self):
        vault = Vault(KEY)
        assert vault.open(vault.seal("my-community")) == "my-community"

    def test_unicode_survives(self):
        vault = Vault(KEY)
        assert vault.open(vault.seal("pässwörd-✓")) == "pässwörd-✓"

    def test_the_ciphertext_does_not_contain_the_secret(self):
        token = Vault(KEY).seal("my-community")
        assert "my-community" not in token
        assert "my-community" not in base64.urlsafe_b64decode(token + "==").decode("latin-1")

    def test_equal_secrets_give_different_ciphertext(self):
        # Otherwise the database reveals which devices share a community
        # string without decrypting anything.
        vault = Vault(KEY)
        assert vault.seal("shared") != vault.seal("shared")


class TestRefusal:
    def test_a_different_install_cannot_open_it(self):
        # The restored-backup case: same database, fresh secret.key.
        token = Vault(KEY).seal("my-community")
        with pytest.raises(SecretUnavailable, match="enter the credential again"):
            Vault(OTHER_KEY).open(token)

    def test_a_tampered_value_is_refused(self):
        token = Vault(KEY).seal("my-community")
        raw = bytearray(base64.urlsafe_b64decode(token))
        raw[-5] ^= 0x01
        with pytest.raises(SecretUnavailable):
            Vault(KEY).open(base64.urlsafe_b64encode(bytes(raw)).decode())

    @pytest.mark.parametrize("junk", ["", "not-a-token", "gAAAA", "🙂"])
    def test_junk_is_refused_not_crashed_on(self, junk):
        with pytest.raises(SecretUnavailable):
            Vault(KEY).open(junk)

    def test_no_key_material_is_an_error_not_a_weak_key(self):
        with pytest.raises(ValueError):
            Vault("")


class TestKeyDerivation:
    def test_the_raw_secret_is_not_used_directly_as_the_key(self):
        """HKDF, not the secret.key bytes themselves.

        A Fernet key built straight from the raw material must not open what
        the vault sealed; if it could, the derivation step would be decoration.
        """
        token = Vault(KEY).seal("my-community")
        naive = Fernet(base64.urlsafe_b64encode(KEY.encode()[:32]))
        with pytest.raises(InvalidToken):
            naive.decrypt(token.encode())


class TestTheKeyFile:
    def _config(self) -> Config:
        tmp = Path(tempfile.mkdtemp(prefix="spark-vault-"))
        return Config.model_validate({"app": {"data_dir": str(tmp)}})

    def test_the_key_file_is_private_from_creation(self, monkeypatch):
        """Private from the first byte, not merely private in the end.

        The first version of this test checked the final mode, and passed
        against the very code it was written to catch: that code wrote the key
        with the default mode and chmod-ed it afterwards, which does end at
        0600 -- after a window in which, under the usual umask, the key sat on
        disk at 0644. The end state was never the bug.

        So this watches for the after-the-fact fix instead. If anything chmods
        the key, the mode it had just before must already have been private.
        """
        before_chmod: list[int] = []
        real_chmod = Path.chmod

        def spy(self, mode, *args, **kwargs):
            if self.name == "secret.key":
                before_chmod.append(stat.S_IMODE(self.stat().st_mode))
            return real_chmod(self, mode, *args, **kwargs)

        monkeypatch.setattr(Path, "chmod", spy)
        old = os.umask(0o022)  # the common default, where the window was 0644
        try:
            config = self._config()
            config.secret_key()
        finally:
            os.umask(old)

        assert all(mode == 0o600 for mode in before_chmod), (
            f"secret.key existed at {[oct(m) for m in before_chmod]} before being locked down"
        )
        assert stat.S_IMODE(config.app.secret_key_path.stat().st_mode) == 0o600

    def test_the_key_is_stable_across_calls(self):
        config = self._config()
        assert config.secret_key() == config.secret_key()

    def test_the_vault_follows_the_install_key(self):
        config = self._config()
        token = vault_for(config).seal("x")
        assert vault_for(config).open(token) == "x"
        with pytest.raises(SecretUnavailable):
            vault_for(self._config()).open(token)
