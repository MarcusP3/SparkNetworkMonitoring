"""Encryption for the secrets SPARK has to keep.

DESIGN.md says the database holds references to secrets, never the secrets
themselves. SNMP cannot honour that literally -- a community string or a v3 key
has to be sent, so SPARK has to be able to recover it -- so the next best thing
is that the database holds only ciphertext, and the key lives somewhere else.

What that buys, precisely, because it is easy to overclaim:

  * **Protected:** a copied `spark.db`, a backup of it, a database dump pasted
    into a bug report. None of them contain a usable credential.
  * **Not protected:** anyone who owns the VM or the container. The key file
    sits beside the database in the data directory and SPARK reads it at will;
    an attacker with that access reads it too. Nothing short of an external
    secret store changes that, and a homelab tool does not need one.

The key is derived, not reused. `secret.key` is 48 random bytes that already
exist on every install; HKDF turns them into a key for this one purpose, so the
same raw material is never used directly for two jobs, and a future second use
gets its own label and its own key.

Fernet for the envelope: AES-128-CBC with an HMAC-SHA256 over the lot, a random
IV per value, and a format that refuses to decrypt anything tampered with. The
random IV matters for a quieter reason too -- two devices sharing a community
string get two different ciphertexts, so the database does not reveal that
they match.
"""

from __future__ import annotations

import base64
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# The HKDF label. Versioned so the derivation can change without a stored
# value silently decrypting under the wrong scheme.
PURPOSE = b"spark/stored-credentials/v1"


class SecretUnavailable(Exception):
    """A stored secret cannot be decrypted.

    Almost always one cause: the database was restored or copied without the
    `secret.key` it was written under. That is by design, not a fault -- it is
    the same property that stops a restored backup resurrecting old sessions --
    and the fix is to enter the credential again, which is what the UI says.
    """


class Vault:
    def __init__(self, key_material: str) -> None:
        if not key_material:
            raise ValueError("no key material")
        derived = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=None, info=PURPOSE
        ).derive(key_material.encode())
        self._fernet = Fernet(base64.urlsafe_b64encode(derived))

    def seal(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode("ascii")

    def open(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode()
        except (InvalidToken, ValueError, UnicodeError):
            # `from None`: the underlying error is always "the MAC did not
            # match", which says nothing the message below does not.
            raise SecretUnavailable(
                "This credential cannot be decrypted. The database was most "
                "likely restored or copied without its secret.key -- enter the "
                "credential again."
            ) from None


@lru_cache(maxsize=4)
def _vault(key_material: str) -> Vault:
    return Vault(key_material)


def vault_for(config) -> Vault:  # type: ignore[no-untyped-def]
    """The vault for this install. Cached, because HKDF on every read is waste."""
    return _vault(config.secret_key())
