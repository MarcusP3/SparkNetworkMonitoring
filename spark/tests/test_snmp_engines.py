"""One SNMP engine per credential, kept across polls (performance review
2026-10-01, P1).

Building a pysnmp engine compiles thirteen MIB modules -- 70-80 ms and ~5 MB
per poll when every collector made its own, and a heap that grew with every
burst of overlapping polls. The collector now takes an engine from a cache
keyed by credential and event loop and only ever lets go of its target.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from spark.collectors import SnmpCollector, SnmpCredential
from spark.collectors import oids as O
from spark.collectors import snmp as S
from spark.collectors.base import AuthFailed, Unreachable
from spark.config import Config
from spark.main import create_app

from test_snmp import AGENT_COMMUNITY, AGENT_HOST, AGENT_PORT, needs_agent

V2 = SnmpCredential(community="one", port=1161)
V2_OTHER = SnmpCredential(community="two", port=1161)
V3 = SnmpCredential(version="v3", username="ro", auth_key="auth-key-one", priv_key="priv-key-one")
V3_SAME_NAME = SnmpCredential(version="v3", username="ro", auth_key="auth-key-two",
                              priv_key="priv-key-two")


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


class TestSharing:
    def test_two_collectors_with_one_credential_share_an_engine(self):
        async def go():
            a = SnmpCollector("192.0.2.1", V2)
            b = SnmpCollector("192.0.2.2", SnmpCredential(community="one", port=1161))
            ea, _, ta = await a._ensure()
            eb, _, tb = await b._ensure()
            assert ea is eb                       # one engine for the credential
            assert ta is not tb                   # a target each
            assert S.engine_count() == 1
            await a.close()
            await b.close()
            assert S.engine_count() == 1          # close() keeps the engine
            # And the next collector gets the same one again.
            c = SnmpCollector("192.0.2.3", V2)
            ec, _, _ = await c._ensure()
            assert ec is ea
            S.close_engines()
            assert S.engine_count() == 0
        run(go())

    def test_different_credentials_get_different_engines(self):
        async def go():
            engines = {
                name: (await SnmpCollector("192.0.2.1", cred)._ensure())[0]
                for name, cred in (("v2", V2), ("v2b", V2_OTHER), ("v3", V3))
            }
            assert len({id(e) for e in engines.values()}) == 3
            assert S.engine_count() == 3
            S.close_engines()
        run(go())

    def test_same_v3_user_with_different_keys_never_shares(self):
        # pysnmp keys its user table by (name, engine id): two profiles with
        # the same user name and different keys on one engine would delete
        # and re-add each other's entry between requests and fail to
        # authenticate. Separate engines, by design.
        async def go():
            ea = (await SnmpCollector("192.0.2.1", V3)._ensure())[0]
            eb = (await SnmpCollector("192.0.2.1", V3_SAME_NAME)._ensure())[0]
            assert ea is not eb
            S.close_engines()
        run(go())

    def test_host_port_timeout_and_retries_do_not_make_new_engines(self):
        async def go():
            first = (await SnmpCollector("192.0.2.1", SnmpCredential(community="x", port=161))._ensure())[0]
            same = (await SnmpCollector("192.0.2.9", SnmpCredential(
                community="x", port=1161, timeout=0.5, retries=3))._ensure())[0]
            assert first is same
            assert S.engine_count() == 1
            S.close_engines()
        run(go())

    def test_each_loop_has_its_own_engines_and_dead_loops_are_pruned(self):
        async def make():
            engine = (await SnmpCollector("192.0.2.1", V2)._ensure())[0]
            return engine, asyncio.get_running_loop()

        first, loop_one = run(make())
        assert loop_one.is_closed()
        assert loop_one in S._engines             # left behind, for now
        second, loop_two = run(make())
        assert second is not first                # engines belong to a loop
        # The first loop closed without close_engines(); the next lookup on
        # any loop prunes it rather than letting its socket pin it forever.
        assert loop_one not in S._engines
        assert loop_two in S._engines
        S.close_engines()                         # outside any loop: prunes only
        assert loop_two not in S._engines


class TestEviction:
    def _patched(self, monkeypatch, outcome):  # type: ignore[no-untyped-def]
        from pysnmp.proto import errind

        async def fake_get_cmd(*_args, **_kwargs):
            # pysnmp returns an agent's answer, including a timeout, as an
            # error indication; only its own failures are raised.
            if isinstance(outcome, errind.ErrorIndication):
                return outcome, None, 0, []
            raise outcome
        monkeypatch.setattr(S, "get_cmd", fake_get_cmd)

    def test_a_socket_error_replaces_the_engine(self, monkeypatch, caplog):
        self._patched(monkeypatch, OSError("Network is unreachable"))

        async def go():
            collector = SnmpCollector("192.0.2.1", V2)
            before = (await collector._ensure())[0]
            with pytest.raises(OSError):
                await collector.get(O.SYS_DESCR)
            assert S.engine_count() == 0           # evicted; closed once drained
            after = (await SnmpCollector("192.0.2.1", V2)._ensure())[0]
            assert after is not before             # the next poll builds anew
            S.close_engines()
        with caplog.at_level("WARNING", logger="spark.collectors.snmp"):
            run(go())
        assert "replacing it" in caplog.text

    def test_a_timeout_or_refusal_is_the_agent_talking_and_keeps_the_engine(self, monkeypatch):
        from pysnmp.proto import errind

        for indication, expected in ((errind.RequestTimedOut(), Unreachable),
                                     (errind.WrongDigest(), AuthFailed)):
            self._patched(monkeypatch, indication)

            async def go():
                collector = SnmpCollector("192.0.2.1", V2)
                before = (await collector._ensure())[0]
                with pytest.raises(expected):
                    await collector.get(O.SYS_DESCR)
                assert S.engine_count() == 1
                assert (await SnmpCollector("192.0.2.1", V2)._ensure())[0] is before
                S.close_engines()
            run(go())


    def test_an_oid_pysnmp_cannot_encode_keeps_the_engine(self, monkeypatch):
        """Security review 2026-11-05: a custom OID like 1.40.1 cannot be BER
        encoded, and pysnmp says so with StatusInformation. That is one bad
        request, not a broken engine -- evicting on it rebuilt the profile's
        shared engine on every poll and stranded other devices' requests."""
        from pysnmp.proto import errind
        from pysnmp.proto.error import StatusInformation

        self._patched(monkeypatch, StatusInformation(errorIndication=errind.SerializationError()))

        async def go():
            collector = SnmpCollector("192.0.2.1", V2)
            before = (await collector._ensure())[0]
            with pytest.raises(StatusInformation):
                await collector.get("1.40.1")
            assert S.engine_count() == 1
            assert (await SnmpCollector("192.0.2.1", V2)._ensure())[0] is before
            S.close_engines()
        run(go())


class TestReplacedEngines:
    """An engine is shared by every device on its profile. Closing a replaced
    one at once left the others' requests in flight waiting forever (their
    timeouts are the dispatcher's to fire), and a poll job that never returns
    is never run again. So it is closed only after a drain period."""

    def test_a_request_in_flight_on_a_replaced_engine_still_times_out(self):
        import socket
        import time

        quiet = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        quiet.bind(("127.0.0.1", 0))          # takes datagrams, never answers
        port = quiet.getsockname()[1]

        async def go():
            collector = SnmpCollector("127.0.0.1", SnmpCredential(
                community="drain", port=port, timeout=1.0, retries=0))
            engine = (await collector._ensure())[0]
            started = time.monotonic()
            asking = asyncio.create_task(collector.get(O.SYS_DESCR))
            await asyncio.sleep(0.2)
            S._evict(engine)                 # what another device's failure does
            assert S.engine_count() == 0 and engine in S._draining[asyncio.get_running_loop()]
            with pytest.raises(Unreachable):
                await asyncio.wait_for(asking, 10)
            assert time.monotonic() - started < 5, "its own timeout, not a hang"
            S.close_engines()
            assert not S._draining.get(asyncio.get_running_loop())
        try:
            run(go())
        finally:
            quiet.close()

    def test_a_replaced_engine_is_closed_after_the_drain(self, monkeypatch):
        monkeypatch.setattr(S, "ENGINE_DRAIN_SECONDS", 0.05)

        async def go():
            engine = (await SnmpCollector("192.0.2.1", V2)._ensure())[0]
            closed = []
            monkeypatch.setattr(S, "_close_quietly", lambda e: closed.append(e))
            S._evict(engine)
            S._evict(engine)                 # twice is still one close
            assert closed == []
            await asyncio.sleep(0.2)
            assert closed == [engine]
            assert not S._draining[asyncio.get_running_loop()]
        run(go())


class TestAppShutdown:
    def test_the_lifespan_closes_the_engines_it_used(self, tmp_path):
        config = Config.model_validate(
            {"app": {"data_dir": str(tmp_path / "data"), "log_level": "WARNING", "tls": "off"},
             "network": {"subnets": []}}
        )
        config.app.data_dir.mkdir(parents=True, exist_ok=True)
        seen: dict[str, object] = {}

        app = create_app(config)
        with TestClient(app) as client:
            # Make an engine on the app's loop, the way a poll would.
            async def poll():
                await SnmpCollector("192.0.2.1", V2)._ensure()
                seen["during"] = S.engine_count()
                seen["loop"] = asyncio.get_running_loop()
            client.portal.call(poll)
        # After shutdown nothing is left for that loop.
        assert seen["during"] == 1
        assert seen["loop"] not in S._engines


@needs_agent
class TestLive:
    def test_two_polls_reuse_one_engine_and_answer(self):
        cred = SnmpCredential(community=AGENT_COMMUNITY, port=AGENT_PORT, timeout=2.0, retries=0)

        async def go():
            engines = set()
            for _ in range(2):
                collector = SnmpCollector(AGENT_HOST, cred)
                try:
                    health = await collector.collect_health(inventory=False)
                    engines.add(id(collector._engine))
                finally:
                    await collector.close()
                assert health.reachable, health.error
            assert len(engines) == 1
            assert S.engine_count() == 1
            # Concurrent polls on the one engine all get their own answer.
            results = await asyncio.gather(*(
                SnmpCollector(AGENT_HOST, cred).get(O.SYS_NAME) for _ in range(8)))
            assert len({r[O.SYS_NAME] for r in results}) == 1
            S.close_engines()
        run(go())
