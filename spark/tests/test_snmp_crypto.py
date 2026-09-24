"""SNMPv3 privacy, and telling SNMP failures apart.

SNMPv3 with privacy (authPriv) never worked in SPARK before this. pysnmp needs
the `cryptography` package for AES and DES, and it was not a dependency, and
pysnmp copes with that by quietly flagging encryption as unavailable rather
than failing at import. Every authPriv request then died locally with
"Ciphering services not available" -- which SPARK wrapped as a TimeoutError,
so the device looked unreachable. The one mode worth using on a network you
care about looked like a network fault.

Nothing noticed because nothing tested it: the live agent only spoke v2c.

Two layers of guard here:

  * The backend tests run on every `pytest`, agent or not. `cryptography` has
    scheduled removal of a cipher mode pysnmp calls; a lock upgrade that takes
    it away must fail here, not in someone's monitoring.
  * The live tests run against `tests/local_agent.sh`, which now answers v3 as
    well, and check each kind of failure lands under its own name.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pysnmp.hlapi.v3arch.asyncio  # noqa: F401 - load order; the priv modules import circularly when cold
import pytest
from pyasn1.type import univ
from pysnmp.proto.secmod.rfc3414.priv import des as pysnmp_des
from pysnmp.proto.secmod.rfc3826.priv import aes as pysnmp_aes

from spark.collectors import (
    AuthFailed,
    CipherUnavailable,
    SnmpCollector,
    SnmpCredential,
    Unreachable,
)
from spark.collectors import oids as O

AGENT = ("127.0.0.1", 11161)
V3_USER = "sparkv3"
V3_AUTH = "sparktest-auth"
V3_PRIV = "sparktest-priv"


def v3(**overrides) -> SnmpCredential:
    fields = dict(version="v3", username=V3_USER, auth_protocol="SHA",
                  auth_key=V3_AUTH, priv_protocol="AES", priv_key=V3_PRIV,
                  port=AGENT[1], timeout=1.5, retries=0)
    fields.update(overrides)
    return SnmpCredential(**fields)


async def _get_sysname(credential: SnmpCredential) -> str | None:
    collector = SnmpCollector(AGENT[0], credential)
    try:
        return (await collector.get(O.SYS_NAME)).get(O.SYS_NAME)
    finally:
        await collector.close()


def _agent_answers_v3() -> bool:
    """A real authPriv exchange, so the skip is honest.

    Needs the cipher backend to succeed, which is fine: without it the backend
    tests below fail loudly anyway, and that is the failure worth reading.
    """
    try:
        return bool(asyncio.run(_get_sysname(v3(timeout=1.0))))
    except Exception:  # noqa: BLE001 - any failure means "no agent to test against"
        return False


needs_v3_agent = pytest.mark.skipif(
    not _agent_answers_v3(),
    reason="no v3-capable agent on 127.0.0.1:11161 -- run tests/local_agent.sh start",
)


# --------------------------------------------------------------------------
# The backend -- no agent needed, runs every time
# --------------------------------------------------------------------------


class TestCipherBackend:
    def test_pysnmp_found_a_backend_for_aes_and_des(self):
        # The flags pysnmp sets at import. True means it gave up on encryption
        # silently -- exactly the state SPARK shipped in.
        assert pysnmp_aes.PysnmpCryptoError is False, "no AES backend: SNMPv3 privacy is dead"
        assert pysnmp_des.PysnmpCryptoError is False, "no DES backend"

    def test_aes_round_trips_through_pysnmp(self):
        """Through pysnmp's own cipher class, not `cryptography` directly.

        The risk is not that `cryptography` stops doing AES; it is that pysnmp
        calls a mode `cryptography` has scheduled for removal. Only a call
        through pysnmp's code exercises that.
        """
        service = pysnmp_aes.Aes()
        key = univ.OctetString(bytes(range(16)))
        plain = b"a scoped PDU that has to survive the round trip"
        encrypted, salt = service.encrypt_data(key, (1, 100, 0), plain)
        decrypted = service.decrypt_data(key, (1, 100, salt), encrypted)
        assert bytes(encrypted) != plain
        assert bytes(decrypted)[: len(plain)] == plain

    def test_cryptography_is_declared_not_merely_present(self):
        # Present in a dev venv proves nothing about the image, which installs
        # only what the lock pins.
        root = Path(__file__).resolve().parent.parent
        assert '"cryptography' in (root / "pyproject.toml").read_text()
        assert "\ncryptography==" in (root / "requirements.lock").read_text()


# --------------------------------------------------------------------------
# Against a live agent
# --------------------------------------------------------------------------


@needs_v3_agent
class TestLiveV3:
    def test_auth_priv_sha_aes_works(self):
        assert asyncio.run(_get_sysname(v3())) == "test-switch-01"

    def test_a_wrong_auth_key_is_an_auth_failure_not_a_timeout(self):
        # The agent answers this one -- a report PDU, wrongDigest -- so saying
        # "unreachable" would send you checking cables.
        with pytest.raises(AuthFailed):
            asyncio.run(_get_sysname(v3(auth_key="not-the-auth-key")))

    def test_an_unknown_user_is_an_auth_failure(self):
        with pytest.raises(AuthFailed):
            asyncio.run(_get_sysname(v3(username="nobody-here")))

    def test_a_wrong_privacy_key_is_indistinguishable_and_says_so(self):
        # The agent cannot decrypt the request, so it drops it; from outside
        # that is identical to a dead device. The message has to name the
        # credentials as a suspect rather than claim to know.
        with pytest.raises(Unreachable, match="credentials"):
            asyncio.run(_get_sysname(v3(priv_key="not-the-priv-key")))

    def test_a_missing_backend_reports_itself_and_fast(self, monkeypatch):
        """The original bug, reproduced on purpose.

        Needs the agent: v3 opens with an unencrypted engine-discovery exchange
        and only encrypts once the device has answered it, so against nothing
        at all this would correctly be Unreachable. With an agent it has to be
        CipherUnavailable, and it has to be quick -- the failure is local, so
        there is nothing to wait for.
        """
        monkeypatch.setattr(pysnmp_aes, "PysnmpCryptoError", True)
        started = time.monotonic()
        with pytest.raises(CipherUnavailable, match="SPARK's fault"):
            asyncio.run(_get_sysname(v3(timeout=5)))
        assert time.monotonic() - started < 2, "waited for a timeout on a local failure"

    def test_v2c_with_a_wrong_community_is_unreachable(self):
        credential = SnmpCredential(community="not-the-community", port=AGENT[1],
                                    timeout=1.0, retries=0)
        with pytest.raises(Unreachable):
            asyncio.run(_get_sysname(credential))
