"""SNMP traps and syslog from the network's devices: what polling misses.

A UPS that ran on battery for twenty seconds never shows in a 60-second poll;
its trap would. A firewall's failed logins are in its log, not its MIB. So
SPARK can listen (events: in spark.yaml, off by default) for SNMP traps,
v1 and v2c, on UDP 162, and syslog over UDP on 514, RFC 3164 and 5424 alike.

What arrives is a DeviceEvent, and only if it is from a device SPARK knows,
by its address or one merged into it: anything on the LAN can send either
of these, unauthenticated, so a stranger's packets are counted and dropped,
never stored. Each sender gets 120 a minute. v3 traps and informs are not
read (yet), and are counted as such. The text is cut to a kilobyte and kept
events.keep_days (30): this is for alerts, not a log store.

EventRules say which are worth an alert: a source, a device or any, some
text it must contain, a severity at or above which it counts. A match opens
one incident (alert_state "event:<rule>:<device>") and sends one alert --
muted devices and maintenance windows hold it like any other -- and later
matches are counted on it. It closes, and says so, once none has come for
the rule's quiet minutes.

Listening on ports below 1024 needs the image built with
SETCAP_NET_BIND_SERVICE=1 and NET_BIND_SERVICE in the compose file's
cap_add (Dockerfile). Without them binding fails, and the Events page says
exactly that rather than SPARK failing to start.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import alerts, events as live, snmp_alerts
from .db import session_scope
from .engine.state import human_duration
from .models import (AlertIncident, AlertState, Device, DeviceAddress, DeviceEvent, EventRule,
                     utcnow)

log = logging.getLogger(__name__)

TRAP, SYSLOG = "trap", "syslog"
SOURCES = (TRAP, SYSLOG)
SEVERITIES = ("emergency", "alert", "critical", "error", "warning", "notice", "info", "debug")
MAX_TEXT = 1024
PER_SENDER_PER_MINUTE = 120
QUEUE_SIZE = 2000
# Each packet queued is cut to this: a full queue is then 16 MB, not the
# 128 MB of 64 KB datagrams, in a 512 MB container. A trap is rarely more
# than one MTU; syslog text is cut to MAX_TEXT anyway.
MAX_DATAGRAM = 8192
BATCH = 200
ADDRESS_CACHE_SECONDS = 60
JOB_ID = "events:housekeeping"
HOUSEKEEPING_SECONDS = 60
QUIET = "quiet"

# Traps by OID: a name a person reads, and how bad it is on syslog's scale.
TRAPS = {
    "1.3.6.1.6.3.1.1.5.1": ("coldStart", 5),
    "1.3.6.1.6.3.1.1.5.2": ("warmStart", 5),
    "1.3.6.1.6.3.1.1.5.3": ("linkDown", 4),
    "1.3.6.1.6.3.1.1.5.4": ("linkUp", 6),
    "1.3.6.1.6.3.1.1.5.5": ("authenticationFailure", 4),
    "1.3.6.1.6.3.1.1.5.6": ("egpNeighborLoss", 4),
    # UPS-MIB (RFC 1628), which most network-card UPSes speak.
    "1.3.6.1.2.1.33.2.1": ("upsTrapOnBattery", 2),
    "1.3.6.1.2.1.33.2.2": ("upsTrapTestCompleted", 6),
    "1.3.6.1.2.1.33.2.3": ("upsTrapAlarmEntryAdded", 3),
    "1.3.6.1.2.1.33.2.4": ("upsTrapAlarmEntryRemoved", 6),
}
# Varbind OIDs worth a name in the text, by prefix (the index follows).
VARBINDS = {
    "1.3.6.1.2.1.2.2.1.1.": "ifIndex", "1.3.6.1.2.1.2.2.1.2.": "ifDescr",
    "1.3.6.1.2.1.2.2.1.7.": "ifAdminStatus", "1.3.6.1.2.1.2.2.1.8.": "ifOperStatus",
    "1.3.6.1.2.1.31.1.1.1.1.": "ifName", "1.3.6.1.2.1.33.1.2.3.": "upsEstimatedMinutesRemaining",
    "1.3.6.1.2.1.33.1.6.2.1.2.": "upsAlarmDescr",
}
_SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
_TRAP_OID = "1.3.6.1.6.3.1.1.4.1.0"


@dataclass(frozen=True)
class Parsed:
    source: str
    severity: int
    title: str
    text: str
    oid: str | None = None


class Unreadable(ValueError):
    """Not something SPARK can read; the reason is counted."""


# --------------------------------------------------------------------------
# Reading what arrives
# --------------------------------------------------------------------------


_PRI = re.compile(r"^<(\d{1,3})>(.*)$", re.S)
_RFC3164_HEAD = re.compile(r"^([A-Z][a-z]{2} [ \d]\d \d\d:\d\d:\d\d) (\S+) (.*)$", re.S)
_TAG = re.compile(r"^([^\s:\[]{1,48})(?:\[[^\]]{0,16}\])?: ?(.*)$", re.S)
_SD = re.compile(r"^(?:\[(?:[^\]\\]|\\.)*\])+ ?", re.S)


def parse_syslog(data: bytes) -> Parsed:
    """A syslog datagram, RFC 3164 or 5424. Without a PRI it is user.notice,
    as RFC 3164 says to treat it."""
    text = data.decode("utf-8", "replace").strip("\x00\r\n ").replace("\ufeff", "")
    if not text:
        raise Unreadable("empty")
    severity, rest = 5, text
    m = _PRI.match(text)
    if m and int(m.group(1)) <= 191:
        severity, rest = int(m.group(1)) % 8, m.group(2)
    app: str | None = None
    if rest.startswith("1 "):                                  # RFC 5424
        parts = rest.split(" ", 6)
        if len(parts) >= 4 and parts[3] != "-":
            app = parts[3]
        tail = parts[6] if len(parts) == 7 else ""
        if tail.startswith("- ") or tail == "-":
            message = tail[2:]
        else:
            message = _SD.sub("", tail, count=1)
    else:                                                      # RFC 3164
        head = _RFC3164_HEAD.match(rest)
        body = head.group(3) if head else rest
        tag = _TAG.match(body)
        app, message = (tag.group(1), tag.group(2)) if tag else (None, body)
    return Parsed(SYSLOG, severity, (app or "syslog")[:255], message.strip()[:MAX_TEXT])


def _label(oid: str) -> str:
    for prefix, name in VARBINDS.items():
        if oid.startswith(prefix):
            return f"{name}.{oid[len(prefix):]}"
    return oid


def parse_trap(data: bytes) -> Parsed:
    """An SNMP v1 or v2c trap, by the low-level API: no engine, no MIBs."""
    from pyasn1.codec.ber import decoder
    from pysnmp.proto import api

    try:
        version = int(api.decodeMessageVersion(data))
    except Exception as exc:  # noqa: BLE001 - garbage is garbage
        raise Unreadable("not SNMP") from exc
    if version not in (api.SNMP_VERSION_1, api.SNMP_VERSION_2C):
        raise Unreadable("SNMPv3")
    proto = api.PROTOCOL_MODULES[version]
    try:
        message, _ = decoder.decode(data, asn1Spec=proto.Message())
        pdu = proto.apiMessage.get_pdu(message)
    except Exception as exc:  # noqa: BLE001
        raise Unreadable("not SNMP") from exc
    if not pdu.isSameTypeWith(proto.TrapPDU()):
        raise Unreadable("not a trap")
    varbinds = [(str(o), v) for o, v in proto.apiTrapPDU.get_varbinds(pdu)]
    if version == api.SNMP_VERSION_1:
        generic = int(proto.apiTrapPDU.get_generic_trap(pdu))
        specific = int(proto.apiTrapPDU.get_specific_trap(pdu))
        enterprise = str(proto.apiTrapPDU.get_enterprise(pdu))
        oid = f"1.3.6.1.6.3.1.1.5.{generic + 1}" if generic < 6 else f"{enterprise}.0.{specific}"
    else:
        oid = next((v.prettyPrint() for o, v in varbinds if o == _TRAP_OID), "")
        varbinds = [(o, v) for o, v in varbinds if o not in (_SYS_UPTIME, _TRAP_OID)]
        if not oid:
            raise Unreadable("no trap OID")
    name, severity = TRAPS.get(oid, (oid, 5))
    text = "; ".join(f"{_label(o)} = {v.prettyPrint()}" for o, v in varbinds)
    return Parsed(TRAP, severity, name[:255], text[:MAX_TEXT], oid[:128])


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------


def matches(rule: EventRule, device_id: int, event: Parsed) -> bool:
    if not rule.enabled:
        return False
    if rule.source not in ("any", event.source):
        return False
    if rule.device_id is not None and rule.device_id != device_id:
        return False
    if event.severity > rule.max_severity:
        return False
    if rule.contains:
        hay = " ".join(x for x in (event.title, event.text, event.oid) if x).lower()
        if rule.contains.lower() not in hay:
            return False
    return True


def key_of(rule_id: int, device_id: int) -> str:
    return f"event:{rule_id}:{device_id}"


async def _fire(session: AsyncSession, rule: EventRule, device: Device, event: Parsed,
                now: datetime) -> None:
    """The first match opens an incident and sends; the rest are counted."""
    key = key_of(rule.id, device.id)
    what = f"{event.title}: {event.text}" if event.text else event.title
    body = f"`{device.primary_ip or '—'}` — {SEVERITIES[event.severity]} {event.source}. {what}"[:1500]
    state = await session.get(AlertState, key)
    if state is not None:
        state.polls = (state.polls or 0) + 1
        state.seen_at = now
        incident = await session.scalar(select(AlertIncident).where(
            AlertIncident.key == key, AlertIncident.closed_at.is_(None)).limit(1))
        if incident is not None:                       # the latest, and how many
            incident.detail = f"{body[:1000]} ({state.polls} so far)"
        return
    state = AlertState(key=key, since=now, seen_at=now, polls=1, fired=True, notified=False)
    session.add(state)
    subject = f"{device.display_name}: {rule.name}"
    await snmp_alerts.open_incident(session, key, snmp_alerts.Record(device.id, subject, body), now)
    settings = await alerts.load(session)
    if settings.get("enabled", True) and not await alerts.is_quiet(session, device_id=device.id,
                                                                   when=now):
        await alerts.enqueue(session, kind="event", tone="bad" if event.severity <= 4 else "info",
                             subject=subject, body=body,
                             dedupe_key=f"rule:{key}:{now.isoformat()}:fire")
        state.notified = True


async def expire(session: AsyncSession, now: datetime | None = None) -> int:
    """Close event incidents nothing has matched for their quiet minutes,
    and say so if the start was sent. Returns how many closed."""
    now = now or utcnow()
    settings = await alerts.load(session)
    closed = 0
    for state in (await session.execute(select(AlertState).where(
            AlertState.key.like("event:%")))).scalars().all():
        _, rule_id, device_id = state.key.split(":")
        rule = await session.get(EventRule, int(rule_id))
        if rule is not None and now - state.seen_at < timedelta(minutes=rule.quiet_minutes):
            continue
        await snmp_alerts.close_incidents(session, now, key=state.key,
                                          resolution=QUIET if rule else snmp_alerts.GONE)
        device = await session.get(Device, int(device_id))
        if (rule is not None and device is not None and state.notified
                and settings.get("enabled", True) and settings.get("notify_on_recovery", True)
                and not await alerts.is_quiet(session, device_id=device.id, when=now)):
            many = (f"{state.polls} matching events over "
                    f"{human_duration((state.seen_at - state.since).total_seconds())}"
                    if state.polls > 1 else "One matching event")
            minutes = f"{rule.quiet_minutes} minute{'s' if rule.quiet_minutes != 1 else ''}"
            await alerts.enqueue(
                session, kind="event_quiet", tone="ok",
                subject=f"{device.display_name}: {rule.name} — quiet again",
                body=f"{many}; none for {minutes} since.",
                dedupe_key=f"rule:{state.key}:{state.since.isoformat()}:clear")
        await session.delete(state)
        closed += 1
    return closed


# --------------------------------------------------------------------------
# Listening
# --------------------------------------------------------------------------


@dataclass
class Listener:
    port: int
    on: bool
    state: str = "off"            # off, listening, failed
    error: str | None = None


@dataclass
class Counts:
    received: int = 0
    stored: int = 0
    unknown: int = 0              # from an address SPARK does not know
    limited: int = 0              # over a sender's share, or the queue was full
    unreadable: dict[str, int] = field(default_factory=dict)
    strangers: dict[str, tuple[int, float]] = field(default_factory=dict)  # address -> (n, last)


class _Datagrams(asyncio.DatagramProtocol):
    def __init__(self, receiver: "Receiver", source: str) -> None:
        self.receiver, self.source = receiver, source

    def datagram_received(self, data: bytes, addr) -> None:  # type: ignore[no-untyped-def]
        self.receiver.feed(self.source, data, str(addr[0]))


class Receiver:
    """Both listeners, the queue between them and the database, and what
    has happened since SPARK started."""

    def __init__(self, host: str, *, traps: Listener, syslog: Listener) -> None:
        self.host = host
        self.listeners = {TRAP: traps, SYSLOG: syslog}
        self.counts = Counts()
        self._queue: asyncio.Queue[tuple[str, bytes, str, datetime]] = asyncio.Queue(QUEUE_SIZE)
        self._transports: list[asyncio.BaseTransport] = []
        self._task: asyncio.Task | None = None
        self._recent: dict[str, deque[float]] = {}
        self._addresses: dict[str, int] = {}
        self._addresses_at = 0.0

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        for source, listener in self.listeners.items():
            if not listener.on:
                continue
            try:
                transport, _ = await loop.create_datagram_endpoint(
                    lambda source=source: _Datagrams(self, source),
                    local_addr=(self.host, listener.port))
            except PermissionError:
                listener.state = "failed"
                listener.error = (f"Not allowed to listen on port {listener.port}: build the image "
                                  f"with SETCAP_NET_BIND_SERVICE=1 and add NET_BIND_SERVICE to "
                                  f"cap_add, or use a port above 1024.")
                log.warning("%s: %s", source, listener.error)
                continue
            except OSError as exc:
                listener.state = "failed"
                listener.error = (f"Port {listener.port} is already in use on this machine."
                                  if exc.errno == errno.EADDRINUSE else f"Could not listen: {exc}")
                log.warning("%s: %s", source, listener.error)
                continue
            self._transports.append(transport)
            listener.state = "listening"
            listener.port = transport.get_extra_info("sockname")[1]   # port 0: the one given
            log.info("Listening for %s on UDP %s", "SNMP traps" if source == TRAP else "syslog",
                     listener.port)
        if self._transports:
            self._task = asyncio.create_task(self._consume())

    async def stop(self) -> None:
        for transport in self._transports:
            transport.close()
        self._transports.clear()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    def feed(self, source: str, data: bytes, address: str, now: float | None = None) -> bool:
        """From a listener: queue it, unless this sender is over its share."""
        now = time.monotonic() if now is None else now
        if address.startswith("::ffff:"):
            address = address[7:]
        self.counts.received += 1
        recent = self._recent.setdefault(address, deque())
        while recent and now - recent[0] > 60:
            recent.popleft()
        if len(recent) >= PER_SENDER_PER_MINUTE:
            self.counts.limited += 1
            return False
        if len(self._recent) > 10_000:
            self._recent.clear()
        recent.append(now)
        try:
            self._queue.put_nowait((source, data[:MAX_DATAGRAM], address, utcnow()))
        except asyncio.QueueFull:
            self.counts.limited += 1
            return False
        return True

    async def _known(self, session: AsyncSession) -> dict[str, int]:
        """Address -> device, primary and merged, not ignored. Cached a minute."""
        if time.monotonic() - self._addresses_at < ADDRESS_CACHE_SECONDS and self._addresses:
            return self._addresses
        found: dict[str, int] = {}
        for ip, device_id in (await session.execute(
                select(Device.primary_ip, Device.id).where(Device.ignored.is_(False),
                                                           Device.primary_ip.is_not(None)))).all():
            found[ip] = device_id
        for ip, device_id in (await session.execute(
                select(DeviceAddress.ip, DeviceAddress.device_id))).all():
            found.setdefault(ip, device_id)
        self._addresses, self._addresses_at = found, time.monotonic()
        return found

    async def _consume(self) -> None:
        while True:
            first = await self._queue.get()
            batch = [first]
            while len(batch) < BATCH and not self._queue.empty():
                batch.append(self._queue.get_nowait())
            try:
                stored = await self.take(batch)
            except Exception:  # noqa: BLE001 - one bad batch must not stop listening
                log.exception("Storing device events failed")
                continue
            if stored:
                live.publish({"kind": "device-events", "count": stored})

    async def take(self, batch: list[tuple[str, bytes, str, datetime]]) -> int:
        """Store what is from known devices and match it to the rules."""
        stored = 0
        async with session_scope() as session:
            known = await self._known(session)
            rules = list((await session.execute(
                select(EventRule).where(EventRule.enabled.is_(True)))).scalars())
            for source, data, address, at in batch:
                device_id = known.get(address)
                if device_id is None:
                    self.counts.unknown += 1
                    n, _ = self.counts.strangers.get(address, (0, 0.0))
                    if len(self.counts.strangers) < 50 or address in self.counts.strangers:
                        self.counts.strangers[address] = (n + 1, time.time())
                    continue
                try:
                    parsed = parse_trap(data) if source == TRAP else parse_syslog(data)
                except Unreadable as why:
                    self.counts.unreadable[str(why)] = self.counts.unreadable.get(str(why), 0) + 1
                    continue
                session.add(DeviceEvent(device_id=device_id, received_at=at, source=parsed.source,
                                        severity=parsed.severity, title=parsed.title,
                                        text=parsed.text or None, oid=parsed.oid, address=address))
                stored += 1
                hits = [r for r in rules if matches(r, device_id, parsed)]
                if hits:
                    device = await session.get(Device, device_id)
                    for rule in hits:
                        await _fire(session, rule, device, parsed, at)
        self.counts.stored += stored
        return stored

    def status(self) -> dict:
        return {"listeners": self.listeners, "counts": self.counts}


current: Receiver | None = None


async def start(config) -> Receiver:  # type: ignore[no-untyped-def]
    global current
    ev = config.events
    current = Receiver(config.app.host or "0.0.0.0",
                       traps=Listener(ev.trap_port, ev.traps), syslog=Listener(ev.syslog_port, ev.syslog))
    await current.start()
    return current


async def stop() -> None:
    global current
    if current is not None:
        await current.stop()
    current = None


async def housekeeping(config) -> None:  # type: ignore[no-untyped-def]
    """The scheduler's job, every minute: close quiet incidents, and drop
    events past keep_days. Never raises."""
    try:
        async with session_scope() as session:
            closed = await expire(session)
            await session.execute(delete(DeviceEvent).where(
                DeviceEvent.received_at < utcnow() - timedelta(days=config.events.keep_days)))
        if closed:
            live.publish({"kind": "device-events"})
    except Exception:  # noqa: BLE001
        log.exception("Event housekeeping failed")
