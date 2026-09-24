"""The four check types: ping, TCP, HTTP(S) and DNS.

One module rather than four files, because each is short and they share the
same shape. Split them when one grows a personality.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from .base import CheckOutcome, CheckSpec, describe_exception

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# ICMP
# --------------------------------------------------------------------------


async def check_ping(spec: CheckSpec) -> CheckOutcome:
    """ICMP echo, reporting latency and packet loss.

    Sends several packets rather than one: a single lost packet on a wifi link
    is noise, and the difference between "one of four dropped" and "all four
    dropped" is the difference between a note and an outage.

    Needs CAP_NET_RAW for real ICMP. Without it icmplib can fall back to
    unprivileged datagram sockets, which only work if the host's
    net.ipv4.ping_group_range allows it, so both are attempted before giving up.
    """
    try:
        from icmplib import async_ping
    except ImportError:  # pragma: no cover - dependency is declared
        return CheckOutcome.down("icmplib is not installed")

    # Bounded: `count` is free text in the params box, and each echo waits
    # `interval` (or, unanswered, the timeout), so "count": 500 is a check
    # that never finishes and a job that never yields its scheduler slot.
    count = min(max(int(spec.param("count", 3) or 3), 1), 20)
    interval = min(max(float(spec.param("interval", 0.2) or 0.2), 0.05), 5.0)
    loss_warn = float(spec.param("loss_warn_percent", 1) or 0)

    last_error: str | None = None
    for privileged in (True, False):
        try:
            host = await async_ping(
                spec.address,
                count=count,
                interval=interval,
                timeout=spec.timeout_seconds,
                privileged=privileged,
            )
        except Exception as exc:  # noqa: BLE001 - never raise out of a check
            last_error = describe_exception(exc)
            continue

        loss_percent = host.packet_loss * 100
        received = count - round(host.packet_loss * count)
        if not host.is_alive or received == 0:
            return CheckOutcome.down(f"no reply to {count} echo request(s)")

        latency = host.avg_rtt
        detail = f"{received}/{count} replies, {latency:.1f}ms avg"
        if loss_percent > loss_warn:
            return CheckOutcome.warn(f"{detail}, {loss_percent:.0f}% loss", latency_ms=latency)
        return CheckOutcome.up(latency_ms=latency, detail=detail)

    return CheckOutcome.down(last_error or "ping failed")


# --------------------------------------------------------------------------
# TCP
# --------------------------------------------------------------------------


async def check_tcp(spec: CheckSpec) -> CheckOutcome:
    """Is anything accepting connections on this port."""
    port = spec.param("port")
    host, port = _split_host_port(spec.address, port)
    if port is None:
        return CheckOutcome.down("no port given; set params.port or use host:port")

    started = time.monotonic()
    writer = None
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=spec.timeout_seconds
        )
        latency = (time.monotonic() - started) * 1000
        return CheckOutcome.up(latency_ms=latency, detail=f"connected to {host}:{port}")
    except asyncio.TimeoutError:
        return CheckOutcome.down(f"timed out after {spec.timeout_seconds}s")
    except Exception as exc:  # noqa: BLE001
        return CheckOutcome.down(describe_exception(exc))
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001 - closing is best effort
                pass


def _split_host_port(address: str, port: Any) -> tuple[str, int | None]:
    """Accept "host", "host:port" and bare IPv6 in brackets."""
    if port not in (None, ""):
        return address, int(port)
    if address.startswith("["):  # [::1]:8080
        host, _, rest = address.partition("]")
        host = host.lstrip("[")
        if rest.startswith(":") and rest[1:].isdigit():
            return host, int(rest[1:])
        return host, None
    host, sep, tail = address.rpartition(":")
    if sep and tail.isdigit():
        return host, int(tail)
    return address, None


# --------------------------------------------------------------------------
# HTTP(S)
# --------------------------------------------------------------------------


async def check_http(spec: CheckSpec) -> CheckOutcome:
    """Status code, optional body match, and a TLS expiry countdown.

    The certificate check is the reason this is worth more than a TCP probe:
    an expiring certificate is an outage you can see coming, and the only
    warning you get is the one you built.
    """
    import httpx

    url = spec.address
    if "://" not in url:
        url = f"http://{url}"

    expect_status = spec.param("expect_status")
    expect_body = spec.param("expect_body")
    cert_warn_days = int(spec.param("cert_warn_days", 14) or 0)
    verify = bool(spec.param("verify_tls", True))
    method = str(spec.param("method", "GET") or "GET").upper()

    started = time.monotonic()
    try:
        async with httpx.AsyncClient(
            timeout=spec.timeout_seconds, verify=verify, follow_redirects=True
        ) as client:
            response = await client.request(method, url)
            latency = (time.monotonic() - started) * 1000
            body = response.text if expect_body else ""
            cert_days = _cert_days_remaining(response)
    except Exception as exc:  # noqa: BLE001
        return CheckOutcome.down(describe_exception(exc))

    if expect_status not in (None, ""):
        if response.status_code != int(expect_status):
            return CheckOutcome.down(
                f"HTTP {response.status_code}, expected {expect_status}", latency_ms=latency
            )
    elif response.status_code >= 400:
        return CheckOutcome.down(f"HTTP {response.status_code}", latency_ms=latency)

    if expect_body and expect_body not in body:
        return CheckOutcome.down(
            f"HTTP {response.status_code} but body did not contain {expect_body!r}",
            latency_ms=latency,
        )

    detail = f"HTTP {response.status_code} in {latency:.0f}ms"
    if cert_days is not None:
        detail = f"{detail}, cert {cert_days}d left"
        if cert_days <= 0:
            return CheckOutcome.down(f"TLS certificate expired ({detail})", latency_ms=latency)
        if cert_warn_days and cert_days <= cert_warn_days:
            return CheckOutcome.warn(
                f"TLS certificate expires in {cert_days} days", latency_ms=latency
            )
    return CheckOutcome.up(latency_ms=latency, detail=detail)


def _cert_days_remaining(response: Any) -> int | None:
    """Days until the peer certificate expires, or None if not discoverable.

    Reaching for the live SSL object through httpx's transport extensions is
    best-effort by nature: it is absent on plain HTTP, on a mocked transport,
    and whenever httpcore changes shape. A missing countdown must never fail
    the check, so every failure path here returns None.
    """
    try:
        stream = response.extensions.get("network_stream")
        if stream is None:
            return None
        ssl_object = stream.get_extra_info("ssl_object")
        if ssl_object is None:
            return None
        cert = ssl_object.getpeercert()
        if not cert or "notAfter" not in cert:
            return None
        expires = datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(
            tzinfo=timezone.utc
        )
        return (expires - datetime.now(timezone.utc)).days
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------
# DNS
# --------------------------------------------------------------------------


async def check_dns(spec: CheckSpec) -> CheckOutcome:
    """Does a name still resolve.

    Worth its own check type because "the internet is down" is so often "DNS
    is down" -- every other check fails at once and none of them says why.
    """
    try:
        import dns.asyncresolver
        import dns.resolver
    except ImportError:  # pragma: no cover - dependency is declared
        return CheckOutcome.down("dnspython is not installed")

    qname = str(spec.param("qname") or spec.address)
    rdtype = str(spec.param("rdtype", "A") or "A").upper()
    server = spec.param("server")
    expect = spec.param("expect")

    resolver = dns.asyncresolver.Resolver(configure=not server)
    if server:
        resolver.nameservers = [str(server)]
    resolver.lifetime = spec.timeout_seconds
    resolver.timeout = spec.timeout_seconds

    started = time.monotonic()
    try:
        answer = await resolver.resolve(qname, rdtype)
    except Exception as exc:  # noqa: BLE001
        return CheckOutcome.down(describe_exception(exc))
    latency = (time.monotonic() - started) * 1000

    records = sorted(str(item) for item in answer)
    if not records:
        return CheckOutcome.down(f"{rdtype} {qname} returned no records", latency_ms=latency)

    via = f" via {server}" if server else ""
    detail = f"{rdtype} {qname} -> {', '.join(records[:3])}{via}"
    if expect and not any(str(expect) in record for record in records):
        return CheckOutcome.down(
            f"{detail}, expected {expect!r}", latency_ms=latency
        )
    return CheckOutcome.up(latency_ms=latency, detail=detail)


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

# Keyed by CheckType.value so nothing here has to import the ORM.
CHECKS: dict[str, Any] = {
    "ping": check_ping,
    "tcp": check_tcp,
    "http": check_http,
    "dns": check_dns,
}


async def run_check(check_type: str, spec: CheckSpec) -> CheckOutcome:
    fn = CHECKS.get(str(check_type))
    if fn is None:
        return CheckOutcome.down(f"unknown check type {check_type!r}")
    try:
        return await fn(spec)
    except Exception as exc:  # noqa: BLE001 - the last line of defence
        log.exception("Check %s against %s raised", check_type, spec.address)
        return CheckOutcome.down(describe_exception(exc))
