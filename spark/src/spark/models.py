"""Database schema for SPARK.

The full Phase 1 schema is defined here up front, even though the check engine
and discovery workers arrive in later increments. Defining it once avoids a
migration for every feature, and the shape of these tables encodes three
decisions that are expensive to change later:

  1. Devices are identified by MAC, not IP. DHCP reassigns addresses; a tool
     keyed on IP loses a device's entire history the first time a lease churns.
  2. Incidents are rows, not something derived from check results at query time.
     "Was it down, and for how long" gets painful to compute otherwise.
  3. `Service` and `Target` are separate. A service is a fact about the network,
     discovered whether or not you care about it. A target is a decision to
     watch something. Merge them and you either monitor everything you find
     (noise) or lose the inventory of what you chose to ignore.
"""

from __future__ import annotations

import enum
import ipaddress
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class UTCDateTime(TypeDecorator):
    """A datetime column that is always tz-aware UTC in Python.

    SQLite has no datetime type and no concept of an offset, so
    `UTCDateTime` silently stores naive text and hands back naive
    datetimes. Mixing those with `utcnow()` raises "can't subtract offset-naive
    and offset-aware datetimes" at the first duration calculation — which is
    the "back up after 4m 12s" line in a recovery alert, i.e. in production, at
    3 a.m., inside the notifier.

    Storage format is unchanged (naive UTC), so existing databases still read.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):  # type: ignore[no-untyped-def]
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):  # type: ignore[no-untyped-def]
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Counter64(TypeDecorator):
    """An unsigned 64-bit SNMP counter, in SQLite's signed 64-bit INTEGER.

    Counter64 runs to 2**64 - 1 and SQLite's largest integer is 2**63 - 1, so
    the top half of the range does not fit: the driver raises OverflowError on
    write. Rare in practice -- it takes eight exabytes through one port -- but
    some agents start their counters at a random value, and a poll that raises
    on one device's numbers is a poll that stops recording that device.

    Stored in two's complement instead: exact, still an INTEGER, read back as
    the same unsigned number. Only ever compared for equality or differenced
    in Python, never ordered in SQL, so the sign in storage is invisible.
    """

    impl = Integer
    cache_ok = True

    def process_bind_param(self, value, dialect):  # type: ignore[no-untyped-def]
        if value is None:
            return None
        value = int(value)
        return value - (1 << 64) if value >= (1 << 63) else value

    def process_result_value(self, value, dialect):  # type: ignore[no-untyped-def]
        if value is None:
            return None
        return value + (1 << 64) if value < 0 else value


def enum_column(enum_cls, **kwargs):  # type: ignore[no-untyped-def]
    """Store an enum by its *value*, and read it back as an enum member.

    Plain `String` columns accepted these members on write (they subclass str)
    but returned bare strings on read, so `f"{status}"` on an unsaved default
    rendered "HealthStatus.DOWN" instead of "down". `values_callable` is what
    keeps the stored text as the lowercase value rather than the member name,
    so existing rows keep working.
    """
    return mapped_column(
        Enum(
            enum_cls,
            native_enum=False,
            length=16,
            values_callable=lambda e: [member.value for member in e],
        ),
        **kwargs,
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow
    )


# --------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------


# StrEnum, not (str, Enum): both compare equal to their plain-string value, but
# only StrEnum formats as one. Under (str, Enum) on 3.12, f"{HealthStatus.DOWN}"
# renders "HealthStatus.DOWN" — which is the text that would have gone into a
# Discord alert.
class DeviceRole(enum.StrEnum):
    GATEWAY = "gateway"
    SWITCH = "switch"
    HOST = "host"
    CLIENT = "client"
    UNKNOWN = "unknown"
    ACCESS_POINT = "access_point"


# How each role reads on a page, in the order the service map sorts siblings:
# the things other things hang off first.
ROLE_LABELS: dict[DeviceRole, str] = {
    DeviceRole.GATEWAY: "Gateway / router",
    DeviceRole.SWITCH: "Switch",
    DeviceRole.ACCESS_POINT: "Access point",
    DeviceRole.HOST: "Server",
    DeviceRole.CLIENT: "Client device",
    DeviceRole.UNKNOWN: "Not set",
}


class HealthStatus(enum.StrEnum):
    UP = "up"
    DEGRADED = "degraded"
    DOWN = "down"
    PAUSED = "paused"
    UNKNOWN = "unknown"


class CheckType(enum.StrEnum):
    PING = "ping"
    TCP = "tcp"
    HTTP = "http"
    DNS = "dns"
    DOCKER = "docker"


class ServiceSource(enum.StrEnum):
    DOCKER = "docker"
    SCAN = "scan"
    MANUAL = "manual"


class DockerTransport(enum.StrEnum):
    SSH = "ssh"
    PROXY = "proxy"
    TLS = "tls"


class Severity(enum.StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


# --------------------------------------------------------------------------
# Inventory
# --------------------------------------------------------------------------


class Subnet(Base, TimestampMixin):
    """One network segment SPARK knows about.

    Moved out of `spark.yaml` in increment 4c. The YAML entries seed this table
    once and are then ignored, because DESIGN.md's whole premise is that SPARK
    can be handed to someone else and configured through the browser. A setting
    that requires SSH and a container restart is not configuration, it is a
    rebuild.

    `attached` is not cosmetic. ARP only works on directly-attached layer 2
    segments, so on a routed VLAN SPARK can ping a host but cannot learn its
    MAC — and device identity keys on MAC. `vlan` is the opposite: nothing
    reads it, it is there so the inventory documents itself.
    """

    __tablename__ = "subnet"

    id: Mapped[int] = mapped_column(primary_key=True)
    cidr: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str | None] = mapped_column(String(64))
    vlan: Mapped[int | None] = mapped_column(Integer)
    attached: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    @property
    def label(self) -> str:
        return self.name or self.cidr

    @property
    def network(self):  # type: ignore[no-untyped-def]
        """The parsed network, or None if the stored CIDR is unparseable.

        Never raises. A subnet row with a bad CIDR should make one page section
        look wrong, not take the Devices page down.
        """
        try:
            return ipaddress.ip_network(self.cidr, strict=False)
        except ValueError:
            return None

    @property
    def host_count(self) -> int:
        network = self.network
        if network is None:
            return 0
        # A /31 or /32 has no usable-host concept worth reporting here.
        return max(0, network.num_addresses - 2) if network.num_addresses > 2 else network.num_addresses

    def contains(self, address: str | None) -> bool:
        """Whether an address falls inside this subnet.

        Membership is computed from the address rather than read from a stored
        label, so renaming a subnet cannot orphan the devices found on it, and
        adding a subnet retroactively classifies devices discovered before it
        existed.
        """
        network = self.network
        if network is None or not address:
            return False
        try:
            return ipaddress.ip_address(address) in network
        except ValueError:
            return False


class Device(Base, TimestampMixin):
    __tablename__ = "device"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Natural key where we can get it. Null on routed VLANs where ARP can't
    # reach; those devices fall back to identity by primary IP until SNMP
    # collection can supply the MAC.
    mac: Mapped[str | None] = mapped_column(String(17), unique=True, index=True)
    primary_ip: Mapped[str | None] = mapped_column(String(45), index=True)

    friendly_name: Mapped[str | None] = mapped_column(String(128))
    hostname: Mapped[str | None] = mapped_column(String(255))
    vendor: Mapped[str | None] = mapped_column(String(128))
    role: Mapped[DeviceRole] = enum_column(DeviceRole, default=DeviceRole.UNKNOWN)

    # Declared hierarchy in v1; replaced by SNMP-derived parenting in Phase 3.
    parent_device_id: Mapped[int | None] = mapped_column(
        ForeignKey("device.id", ondelete="SET NULL")
    )
    subnet: Mapped[str | None] = mapped_column(String(64))

    status: Mapped[HealthStatus] = enum_column(HealthStatus, default=HealthStatus.UNKNOWN)
    first_seen: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen: Mapped[datetime | None] = mapped_column(UTCDateTime)

    # Set true once a human has looked at it, so "new device" alerts stay quiet
    # for things you already know about.
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)
    ignored: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)

    parent = relationship("Device", remote_side=[id], backref="children")
    services = relationship(
        "Service", back_populates="device", cascade="all, delete-orphan"
    )
    interfaces = relationship(
        "Interface", back_populates="device", cascade="all, delete-orphan"
    )

    @property
    def display_name(self) -> str:
        return self.friendly_name or self.hostname or self.primary_ip or self.mac or "unknown"


class DeviceAddress(Base):
    """Another address a device answers at, besides its primary one.

    A firewall has a gateway address on every VLAN. On routed VLANs SPARK
    cannot see MACs and so identifies by IP, and each of those addresses
    became a device of its own. Merging one into the real device moves its
    address here, and a sweep that finds anything at an address listed here
    records it against that device instead of creating it again.
    """

    __tablename__ = "device_address"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(
        ForeignKey("device.id", ondelete="CASCADE"), index=True
    )
    ip: Mapped[str] = mapped_column(String(45), unique=True)
    # "merged": by hand, or by accepting what SNMP suggested (identity.py).
    source: Mapped[str] = mapped_column(String(16), default="merged")
    added_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Interface(Base, TimestampMixin):
    __tablename__ = "interface"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("device.id", ondelete="CASCADE"))
    name: Mapped[str | None] = mapped_column(String(64))
    mac: Mapped[str | None] = mapped_column(String(17), index=True)
    ip: Mapped[str | None] = mapped_column(String(45), index=True)
    speed_mbps: Mapped[int | None] = mapped_column(Integer)
    admin_state: Mapped[str | None] = mapped_column(String(16))
    oper_state: Mapped[str | None] = mapped_column(String(16))

    device = relationship("Device", back_populates="interfaces")


class DockerHost(Base, TimestampMixin):
    """A remote Docker daemon SPARK collects container inventory from.

    SPARK runs on its own VM, so this is always over the network. Plain TCP on
    2375 is deliberately not an option: it is an unauthenticated root shell.
    """

    __tablename__ = "docker_host"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    device_id: Mapped[int | None] = mapped_column(
        ForeignKey("device.id", ondelete="SET NULL")
    )
    transport: Mapped[DockerTransport] = enum_column(DockerTransport, default=DockerTransport.SSH)
    uri: Mapped[str] = mapped_column(String(512))

    # Path to an SSH key or client-cert bundle on disk. Secret *material* never
    # goes in the database; only the reference to it does.
    secret_ref: Mapped[str | None] = mapped_column(String(512))

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_ok_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_error: Mapped[str | None] = mapped_column(Text)


class Service(Base, TimestampMixin):
    """Something listening on a port. A fact, not a decision to monitor."""

    __tablename__ = "service"
    __table_args__ = (
        UniqueConstraint("device_id", "port", "protocol", name="uq_service_endpoint"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("device.id", ondelete="CASCADE"))
    port: Mapped[int] = mapped_column(Integer)
    protocol: Mapped[str] = mapped_column(String(8), default="tcp")

    name: Mapped[str | None] = mapped_column(String(128))
    source: Mapped[ServiceSource] = enum_column(ServiceSource, default=ServiceSource.SCAN)

    # Populated when source is DOCKER.
    docker_host_id: Mapped[int | None] = mapped_column(
        ForeignKey("docker_host.id", ondelete="SET NULL")
    )
    container_id: Mapped[str | None] = mapped_column(String(64))
    container_name: Mapped[str | None] = mapped_column(String(255))
    image: Mapped[str | None] = mapped_column(String(255))

    # From HTTP fingerprinting: page <title>, Server: header.
    banner: Mapped[str | None] = mapped_column(Text)

    state: Mapped[str | None] = mapped_column(String(32))
    first_seen: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen: Mapped[datetime | None] = mapped_column(UTCDateTime)
    ignored: Mapped[bool] = mapped_column(Boolean, default=False)

    device = relationship("Device", back_populates="services")
    targets = relationship("Target", back_populates="service")


# --------------------------------------------------------------------------
# Monitoring
# --------------------------------------------------------------------------


# Defaults for a new target, in one place: the column defaults, the form
# defaults and the text shown next to the "tune" checkbox all read from here.
# They have to agree, because the tuning fields are disabled unless that box is
# ticked, and a disabled input is not submitted at all -- so whatever the form
# falls back to IS the default a user gets.
DEFAULT_INTERVAL_SECONDS = 15
DEFAULT_TIMEOUT_SECONDS = 3.0
DEFAULT_FAILURE_THRESHOLD = 4
DEFAULT_RECOVERY_THRESHOLD = 4


class Target(Base, TimestampMixin):
    """A decision to watch something on a schedule."""

    __tablename__ = "target"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    check_type: Mapped[CheckType] = enum_column(CheckType)
    address: Mapped[str] = mapped_column(String(512))
    params: Mapped[dict] = mapped_column(JSON, default=dict)

    device_id: Mapped[int | None] = mapped_column(ForeignKey("device.id", ondelete="CASCADE"))
    service_id: Mapped[int | None] = mapped_column(ForeignKey("service.id", ondelete="CASCADE"))

    interval_seconds: Mapped[int] = mapped_column(Integer, default=DEFAULT_INTERVAL_SECONDS)
    timeout_seconds: Mapped[float] = mapped_column(Float, default=DEFAULT_TIMEOUT_SECONDS)

    # Hysteresis: how many consecutive results before we believe a state change.
    # The difference between a tool you trust and one you mute in a week.
    failure_threshold: Mapped[int] = mapped_column(Integer, default=DEFAULT_FAILURE_THRESHOLD)
    recovery_threshold: Mapped[int] = mapped_column(Integer, default=DEFAULT_RECOVERY_THRESHOLD)

    # Dependency suppression: if the parent target is down, this one's failure
    # is a symptom, not news. One alert for the switch, not thirty.
    depends_on_target_id: Mapped[int | None] = mapped_column(
        ForeignKey("target.id", ondelete="SET NULL")
    )

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    muted_until: Mapped[datetime | None] = mapped_column(UTCDateTime)

    status: Mapped[HealthStatus] = enum_column(HealthStatus, default=HealthStatus.UNKNOWN)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_successes: Mapped[int] = mapped_column(Integer, default=0)
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_status_change: Mapped[datetime | None] = mapped_column(UTCDateTime)

    service = relationship("Service", back_populates="targets")
    depends_on = relationship("Target", remote_side=[id], backref="dependents")


class CheckResult(Base):
    """The hot table. Downsampled nightly: raw 7d, 5-minute 90d, hourly 2y."""

    __tablename__ = "check_result"
    __table_args__ = (Index("ix_check_result_target_ts", "target_id", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("target.id", ondelete="CASCADE"))
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    status: Mapped[HealthStatus] = enum_column(HealthStatus)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    detail: Mapped[str | None] = mapped_column(Text)

    # Set during a speed test, when the WAN is deliberately saturated. Flagged
    # samples are excluded from trend charts so a nightly test doesn't look
    # like a nightly outage.
    suppressed: Mapped[bool] = mapped_column(Boolean, default=False)


# Bucket widths for downsampled history. Raw results are kept briefly, then
# folded into five-minute buckets, then into hourly ones.
FIVE_MINUTES = 300
ONE_HOUR = 3600


class CheckRollup(Base):
    """Downsampled check history.

    Raw results answer "what was the latency at 14:32:15", which matters for
    about a week. After that the question becomes "what did last month look
    like", and a five-minute bucket answers it with a sixtieth of the rows.

    Counts per status rather than a single average, because "95% up" and
    "up the whole time except one ten-minute outage" are different months and
    an average hides the difference.
    """

    __tablename__ = "check_rollup"
    __table_args__ = (
        UniqueConstraint(
            "target_id", "bucket_start", "bucket_seconds", name="uq_rollup_bucket"
        ),
        Index("ix_rollup_target_bucket", "target_id", "bucket_seconds", "bucket_start"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("target.id", ondelete="CASCADE"))
    bucket_start: Mapped[datetime] = mapped_column(UTCDateTime)
    bucket_seconds: Mapped[int] = mapped_column(Integer)

    samples: Mapped[int] = mapped_column(Integer, default=0)
    up: Mapped[int] = mapped_column(Integer, default=0)
    degraded: Mapped[int] = mapped_column(Integer, default=0)
    down: Mapped[int] = mapped_column(Integer, default=0)

    latency_min: Mapped[float | None] = mapped_column(Float)
    latency_avg: Mapped[float | None] = mapped_column(Float)
    latency_max: Mapped[float | None] = mapped_column(Float)

    @property
    def availability(self) -> float | None:
        """Fraction of samples that were not down."""
        if not self.samples:
            return None
        return (self.samples - self.down) / self.samples


class Incident(Base):
    __tablename__ = "incident"
    __table_args__ = (Index("ix_incident_target_opened", "target_id", "opened_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("target.id", ondelete="CASCADE"))
    opened_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    severity: Mapped[Severity] = enum_column(Severity, default=Severity.CRITICAL)
    cause: Mapped[str | None] = mapped_column(Text)

    # Why it closed. "Recovered" and "we stopped looking" are different facts
    # and a duration alone cannot tell them apart: an incident closed because
    # monitoring was paused may well have continued afterwards.
    resolution: Mapped[str | None] = mapped_column(String(32))
    acknowledged_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    # True when this incident was a downstream symptom of another failure and
    # therefore never alerted on.
    suppressed_by_dependency: Mapped[bool] = mapped_column(Boolean, default=False)

    @property
    def duration_seconds(self) -> float | None:
        if self.closed_at is None:
            return None
        return (self.closed_at - self.opened_at).total_seconds()


class SpeedtestResult(Base):
    __tablename__ = "speedtest_result"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    down_mbps: Mapped[float | None] = mapped_column(Float)
    up_mbps: Mapped[float | None] = mapped_column(Float)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    jitter_ms: Mapped[float | None] = mapped_column(Float)
    packet_loss: Mapped[float | None] = mapped_column(Float)
    server: Mapped[str | None] = mapped_column(String(255))
    method: Mapped[str] = mapped_column(String(32), default="ookla")
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    detail: Mapped[str | None] = mapped_column(Text)


class AlertMute(Base):
    """Something that never alerts: a whole device, or one target.

    One global list, kept in Settings -> Alerts. A muted thing is still
    checked, polled and shown; only the Discord messages stop. Exactly one of
    the two ids is set. Rows go with what they mute (ON DELETE CASCADE).
    """

    __tablename__ = "alert_mute"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int | None] = mapped_column(
        ForeignKey("device.id", ondelete="CASCADE"), unique=True
    )
    target_id: Mapped[int | None] = mapped_column(
        ForeignKey("target.id", ondelete="CASCADE"), unique=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class AlertState(Base):
    """Where one threshold rule stands for one device or port.

    "CPU over 90% for 10 minutes" needs to remember when the 10 minutes began
    and whether it has already said so -- or every poll after the tenth minute
    would say it again. Keyed like "cpu:3" or "port:41". A row exists only
    while the condition holds (or has fired and not yet cleared).
    """

    __tablename__ = "alert_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    since: Mapped[datetime] = mapped_column(UTCDateTime)
    seen_at: Mapped[datetime] = mapped_column(UTCDateTime)
    polls: Mapped[int] = mapped_column(Integer, default=0)
    fired: Mapped[bool] = mapped_column(Boolean, default=False)
    # Whether a message actually went out when it fired. Not the same as
    # `fired`: a muted device fires silently, and must not then send a
    # recovery for an alert nobody received.
    notified: Mapped[bool] = mapped_column(Boolean, default=False)
    value: Mapped[float | None] = mapped_column(Float)


class AlertIncident(Base):
    """An alert rule's incident: from when it fired until it cleared.

    What Incident is for a target, for everything decided by alert_state --
    SNMP thresholds, ports, storage, API credentials, drives, TrueNAS's own
    alerts -- and for a polled device that stops answering SNMP. The
    dashboard lists both. Recorded whether or not a message was sent (a muted
    device still had the problem); not recorded while the rule is switched
    off. Keyed like the rule state it follows (migration 17).
    """

    __tablename__ = "alert_incident"
    __table_args__ = (Index("ix_alert_incident_key_open", "key", "closed_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64))
    device_id: Mapped[int | None] = mapped_column(
        ForeignKey("device.id", ondelete="SET NULL"), index=True
    )
    title: Mapped[str] = mapped_column(String(256))
    detail: Mapped[str | None] = mapped_column(Text)
    opened_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # "recovered", or "no longer watched" when the thing it was about went
    # away (a port unstarred, a credential removed).
    resolution: Mapped[str | None] = mapped_column(String(32))


class NotificationStatus(enum.StrEnum):
    PENDING = "pending"    # waiting for the dispatcher
    HELD = "held"          # arrived during quiet hours; goes out in the digest
    SENT = "sent"
    FAILED = "failed"      # gave up after the last retry
    DROPPED = "dropped"    # alerting was switched off, or no webhook, when due


class Notification(Base):
    """One alert, from the moment it is decided until it is delivered or given up.

    An outbox. The decision to alert is written in the same transaction as the
    state change that caused it -- a target going down, a device answering
    again -- so an alert can be neither lost to a crash between the two nor
    sent for a change that rolled back. A separate dispatcher sends what is due,
    outside any transaction, and records how it went. Discord being slow or
    down delays alerts; it never blocks a check or a poll.

    `dedupe_key` is unique where set, so the same event cannot be queued
    twice: "incident:41:down", or an SNMP outage keyed by when the device last
    answered.

    Replaces an earlier `notification` table of the same name that nothing ever
    wrote to (migration 8).
    """

    __tablename__ = "notification"
    __table_args__ = (
        Index("ix_notification_status_due", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    kind: Mapped[str] = mapped_column(String(32))
    subject: Mapped[str] = mapped_column(String(512))
    body: Mapped[str | None] = mapped_column(Text)
    # "bad" / "ok" / "info": which colour the message carries in Discord.
    tone: Mapped[str] = mapped_column(String(8), default="info")

    incident_id: Mapped[int | None] = mapped_column(
        ForeignKey("incident.id", ondelete="SET NULL")
    )
    dedupe_key: Mapped[str | None] = mapped_column(String(128), unique=True)

    status: Mapped[NotificationStatus] = enum_column(
        NotificationStatus, default=NotificationStatus.PENDING
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    error: Mapped[str | None] = mapped_column(Text)


# --------------------------------------------------------------------------
# Auth and runtime settings
# --------------------------------------------------------------------------


class User(Base, TimestampMixin):
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class UserSession(Base):
    __tablename__ = "user_session"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    # Only the hash is stored, so a database leak doesn't hand over live sessions.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    user_agent: Mapped[str | None] = mapped_column(String(255))
    ip: Mapped[str | None] = mapped_column(String(45))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class LoginAttempt(Base):
    __tablename__ = "login_attempt"
    __table_args__ = (Index("ix_login_attempt_ip_ts", "ip", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ip: Mapped[str] = mapped_column(String(45))
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    ok: Mapped[bool] = mapped_column(Boolean, default=False)


class SnmpProfile(Base, TimestampMixin):
    """A set of SNMP credentials, shared by the devices that use it.

    A profile rather than credentials per device because a homelab has one
    community string, or one v3 user, used everywhere -- and when it changes it
    should change in one place, not on every row.

    Secret columns hold ciphertext from `vault.py`, never the secret. The v3
    username is not one of them: it travels in the clear in every v3 request
    regardless, so encrypting it at rest would protect nothing.
    """

    __tablename__ = "snmp_profile"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    version: Mapped[str] = mapped_column(String(8), default="v2c")  # "v2c" | "v3"

    community_sealed: Mapped[str | None] = mapped_column(Text)

    username: Mapped[str | None] = mapped_column(String(64))
    auth_protocol: Mapped[str | None] = mapped_column(String(16))
    auth_key_sealed: Mapped[str | None] = mapped_column(Text)
    # None means authNoPriv: authenticated, but the payload crosses in clear.
    priv_protocol: Mapped[str | None] = mapped_column(String(16))
    priv_key_sealed: Mapped[str | None] = mapped_column(Text)

    port: Mapped[int] = mapped_column(Integer, default=161)

    devices = relationship("SnmpDevice", back_populates="profile")


class SnmpDevice(Base, TimestampMixin):
    """A discovered device SPARK is to collect SNMP from.

    Keyed to the device row, not an address: the address is read from the
    device at the moment it is needed, so a DHCP move is followed rather than
    left pointing at whatever answers on the old address.

    A table of its own rather than columns on `device`. SQLite adds a column
    with a non-idempotent ALTER that has already taken startup down once in
    this project; a new table is a `create` with `checkfirst`.
    """

    __tablename__ = "snmp_device"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(
        ForeignKey("device.id", ondelete="CASCADE"), unique=True
    )
    # RESTRICT, not CASCADE or SET NULL: deleting a profile must not quietly
    # delete devices or leave them with no credentials. The UI refuses first;
    # this is the backstop.
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("snmp_profile.id", ondelete="RESTRICT")
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # The last Test: when, whether it answered, and what it said. `last_probe`
    # is the capability report as JSON, so the Settings page can show what the
    # device supports without asking it again.
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_ok_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_probe: Mapped[dict | None] = mapped_column(JSON)

    profile = relationship("SnmpProfile", back_populates="devices")
    device = relationship("Device")


# ---------------------------------------------------------------------------
# SNMP polling
#
# Tables of their own rather than columns on snmp_device, for the reason given
# there: a new table is a `create` with `checkfirst`, an added column is an
# ALTER. Every one hangs off snmp_device with CASCADE, so taking a device off
# the SNMP list takes its history with it.
# ---------------------------------------------------------------------------


class SnmpPoll(Base):
    """The latest scheduled poll of one device: when, whether, and what it said.

    Separate from the Test result on snmp_device on purpose. Test answers
    "what does this device support?", once, on request; this answers "how is
    it now?" every interval, and neither may overwrite the other.

    `uptime_seconds` is here for more than display: it going backwards between
    polls is how a reboot is recognised, and a reboot resets every counter, so
    the poll after one takes a new baseline instead of computing a rate.
    """

    __tablename__ = "snmp_poll"

    snmp_device_id: Mapped[int] = mapped_column(
        ForeignKey("snmp_device.id", ondelete="CASCADE"), primary_key=True
    )
    last_polled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_ok_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    uptime_seconds: Mapped[float | None] = mapped_column(Float)
    cpu_percent: Mapped[float | None] = mapped_column(Float)
    load_1min: Mapped[float | None] = mapped_column(Float)
    memory_percent: Mapped[float | None] = mapped_column(Float)
    temperature_max: Mapped[float | None] = mapped_column(Float)
    interfaces_total: Mapped[int | None] = mapped_column(Integer)
    interfaces_up: Mapped[int | None] = mapped_column(Integer)
    # Where each value came from, as the collector reported it.
    sources: Mapped[dict | None] = mapped_column(JSON)


class SnmpInterface(Base):
    """One interface on a polled device, as of the last poll that saw it.

    Also where the previous counter reading lives. A rate is the difference
    between two readings, and this is the first place SPARK has had to keep
    the earlier one.

    Keyed by ifIndex, which is stable on almost everything but renumbered by a
    reboot on some cheap switches. When that happens the names follow the new
    numbering on the next poll and the history under a number briefly belongs
    to a different port. Recorded rather than solved: keying by name instead
    breaks on the far more common devices whose names are not unique.

    Interfaces that stop appearing are kept, not deleted -- deleting would
    cascade their history away -- and are recognisable by `last_seen` being
    older than the device's last good poll.
    """

    __tablename__ = "snmp_interface"
    __table_args__ = (
        UniqueConstraint("snmp_device_id", "if_index", name="uq_snmp_interface"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    snmp_device_id: Mapped[int] = mapped_column(
        ForeignKey("snmp_device.id", ondelete="CASCADE"), index=True
    )
    if_index: Mapped[int] = mapped_column(Integer)

    name: Mapped[str | None] = mapped_column(String(128))
    descr: Mapped[str | None] = mapped_column(String(255))
    alias: Mapped[str | None] = mapped_column(String(255))
    type_name: Mapped[str | None] = mapped_column(String(64))
    mac: Mapped[str | None] = mapped_column(String(17))
    admin_status: Mapped[str | None] = mapped_column(String(16))
    oper_status: Mapped[str | None] = mapped_column(String(16))
    speed_mbps: Mapped[int | None] = mapped_column(Integer)

    # The previous reading. 32 or 64: a difference across a change of width is
    # meaningless, so a change of width means a new baseline.
    counter_bits: Mapped[int | None] = mapped_column(Integer)
    in_octets: Mapped[int | None] = mapped_column(Counter64)
    out_octets: Mapped[int | None] = mapped_column(Counter64)
    in_errors: Mapped[int | None] = mapped_column(Counter64)
    out_errors: Mapped[int | None] = mapped_column(Counter64)
    counters_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    # The latest rate, bits per second. None when there was nothing to compare
    # with, or when the comparison could not be trusted.
    in_bps: Mapped[float | None] = mapped_column(Float)
    out_bps: Mapped[float | None] = mapped_column(Float)

    status_changed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    first_seen: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen: Mapped[datetime | None] = mapped_column(UTCDateTime)

    # Starred on the device page: this port alerts when it goes down or stays
    # busy. Off by default, because most ports on a switch are desks and
    # access points whose links come and go all day (migration 9).
    starred: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")

    @property
    def label(self) -> str:
        return self.alias or self.name or self.descr or f"if{self.if_index}"


class SnmpAddress(Base):
    """What a polled device says about addresses: its own, and its ARP table.

    `own`: an address the device holds on one of its interfaces. A firewall
    lists its gateway on every VLAN, so a device SPARK found at one of those
    addresses is the firewall seen again, and the page says so.

    `arp`: an IP and the MAC the device last saw answering at it. Across a
    router SPARK sees no MACs; the router's ARP table has them.

    Read every 15 minutes (identity.py) and replaced whole each time, so it is
    what the device said last, not a history. Only ever suggests: nothing here
    merges devices on its own (migration 11).
    """

    __tablename__ = "snmp_address"
    __table_args__ = (
        UniqueConstraint("snmp_device_id", "kind", "ip", name="uq_snmp_address"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    snmp_device_id: Mapped[int] = mapped_column(
        ForeignKey("snmp_device.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(8))          # "own" or "arp"
    ip: Mapped[str] = mapped_column(String(45), index=True)
    mac: Mapped[str | None] = mapped_column(String(17))
    seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class SnmpNeighbour(Base):
    """What a polled switch says is on each of its ports.

    `fdb`: a MAC learned on a port (the switch's MAC table). `self`: one of
    the switch's own MACs, with no port. `lldp`: a neighbour LLDP heard on a
    port, by MAC and/or name.

    Read every 15 minutes with the addresses (identity.py) and replaced
    whole, like snmp_address. topology.py works out the map from it; nothing
    here changes a device's place on its own (migration 12).
    """

    __tablename__ = "snmp_neighbour"

    id: Mapped[int] = mapped_column(primary_key=True)
    snmp_device_id: Mapped[int] = mapped_column(
        ForeignKey("snmp_device.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(8))          # "fdb", "self" or "lldp"
    if_index: Mapped[int | None] = mapped_column(Integer)
    mac: Mapped[str | None] = mapped_column(String(17), index=True)
    vlan: Mapped[int | None] = mapped_column(Integer)
    name: Mapped[str | None] = mapped_column(String(255))     # LLDP: the neighbour's name
    port: Mapped[str | None] = mapped_column(String(128))     # LLDP: the neighbour's port
    seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class MapAuto(Base):
    """A place on the map that automatic mode set, and what it set it to.

    Automatic mode may move a device it placed when SNMP later sees it
    somewhere else; it never moves one placed by hand. A device whose parent
    still equals `parent_device_id` here is automatic's; one whose parent has
    since been changed -- on its page, by Accept, by anything -- is a person's,
    and stays as they left it (topology.apply_automatic, migration 13).
    """

    __tablename__ = "map_auto"

    device_id: Mapped[int] = mapped_column(
        ForeignKey("device.id", ondelete="CASCADE"), primary_key=True
    )
    parent_device_id: Mapped[int | None] = mapped_column(
        ForeignKey("device.id", ondelete="SET NULL")
    )
    placed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class SnmpStorage(Base):
    """Storage a polled device reports: pools, drives and filesystems.

    `pool`: a TrueNAS ZFS pool -- its health, and its space from the pool's
    root dataset. `drive`: a TrueNAS drive's temperature. `fs`: a filesystem
    from hrStorageTable, on anything running net-snmp.

    Read every 5 minutes, apart from the poll (storage.py): TrueNAS answers
    these slowly. Replaced whole on each read, like snmp_address; the alert
    rules keep what they need in alert_state (migration 14).
    """

    __tablename__ = "snmp_storage"
    __table_args__ = (
        UniqueConstraint("snmp_device_id", "kind", "name", name="uq_snmp_storage"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    snmp_device_id: Mapped[int] = mapped_column(
        ForeignKey("snmp_device.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(8))            # "pool", "drive" or "fs"
    name: Mapped[str] = mapped_column(String(255))
    health: Mapped[str | None] = mapped_column(String(32))  # pools: ONLINE, DEGRADED...
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    used_bytes: Mapped[int | None] = mapped_column(BigInteger)
    celsius: Mapped[float | None] = mapped_column(Float)    # drives
    read_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    @property
    def percent(self) -> float | None:
        if not self.size_bytes or self.used_bytes is None:
            return None
        return self.used_bytes / self.size_bytes * 100


class ApiCredential(Base, TimestampMixin):
    """A key for a device's own API (Settings -> Credentials).

    TrueNAS first; the UniFi controller will be another `kind`. The key is
    sealed with the install's secret and never shown again once saved. The
    TLS certificate is pinned by its SHA-256 on the first Test a person
    accepts (truenas.py says why), and the key is only ever sent to that
    certificate (migration 15).
    """

    __tablename__ = "api_credential"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))              # "truenas"
    name: Mapped[str] = mapped_column(String(64), unique=True)
    device_id: Mapped[int | None] = mapped_column(
        ForeignKey("device.id", ondelete="SET NULL"), index=True
    )
    host: Mapped[str] = mapped_column(String(255))            # address or host name, :port allowed
    key_sealed: Mapped[str] = mapped_column(Text)
    cert_sha256: Mapped[str | None] = mapped_column(String(95))       # trusted
    pending_sha256: Mapped[str | None] = mapped_column(String(95))    # seen, not yet trusted
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_ok_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_info: Mapped[dict | None] = mapped_column(JSON)       # version, hostname
    # What the last good check read: pools, drives, TrueNAS's alerts, and
    # read_at (truenas.parse; migration 16).
    readings: Mapped[dict | None] = mapped_column(JSON)


class SnmpHealthSample(Base):
    """One poll's health reading; the SNMP counterpart of check_result.

    A row for every poll, answered or not. An unanswered poll is a row with
    `reachable` false and nothing else, which is what lets a chart show a gap
    as a gap and a rollup count how often the device answered.
    """

    __tablename__ = "snmp_health_sample"
    __table_args__ = (Index("ix_snmp_health_device_ts", "snmp_device_id", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    snmp_device_id: Mapped[int] = mapped_column(
        ForeignKey("snmp_device.id", ondelete="CASCADE")
    )
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    reachable: Mapped[bool] = mapped_column(Boolean, default=False)
    cpu_percent: Mapped[float | None] = mapped_column(Float)
    load_1min: Mapped[float | None] = mapped_column(Float)
    memory_percent: Mapped[float | None] = mapped_column(Float)
    temperature_max: Mapped[float | None] = mapped_column(Float)


class SnmpInterfaceSample(Base):
    """One interval's traffic on one interface, as a rate.

    Stored only for interfaces that are up, and only when the rate could be
    trusted. An empty port would otherwise add a row of zeros every minute
    forever; its status is still on snmp_interface.

    Errors are the count during the interval, not the running total, so a sum
    over any window is the number of errors in it.
    """

    __tablename__ = "snmp_interface_sample"
    __table_args__ = (Index("ix_snmp_if_sample_if_ts", "interface_id", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    interface_id: Mapped[int] = mapped_column(
        ForeignKey("snmp_interface.id", ondelete="CASCADE")
    )
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    in_bps: Mapped[float | None] = mapped_column(Float)
    out_bps: Mapped[float | None] = mapped_column(Float)
    in_errors: Mapped[int | None] = mapped_column(Integer)
    out_errors: Mapped[int | None] = mapped_column(Integer)


class SnmpHealthRollup(Base):
    """Downsampled health history, on the same 5-minute/hourly ladder as checks.

    Average and peak both: a CPU averaging 20% over an hour that spent five
    minutes pinned at 100% is a different hour from one that sat at 20%, and
    an average alone cannot tell you which you had.
    """

    __tablename__ = "snmp_health_rollup"
    __table_args__ = (
        UniqueConstraint(
            "snmp_device_id", "bucket_start", "bucket_seconds", name="uq_snmp_health_bucket"
        ),
        Index("ix_snmp_health_rollup", "snmp_device_id", "bucket_seconds", "bucket_start"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    snmp_device_id: Mapped[int] = mapped_column(
        ForeignKey("snmp_device.id", ondelete="CASCADE")
    )
    bucket_start: Mapped[datetime] = mapped_column(UTCDateTime)
    bucket_seconds: Mapped[int] = mapped_column(Integer)

    samples: Mapped[int] = mapped_column(Integer, default=0)
    reachable: Mapped[int] = mapped_column(Integer, default=0)
    cpu_avg: Mapped[float | None] = mapped_column(Float)
    cpu_max: Mapped[float | None] = mapped_column(Float)
    load_avg: Mapped[float | None] = mapped_column(Float)
    memory_avg: Mapped[float | None] = mapped_column(Float)
    memory_max: Mapped[float | None] = mapped_column(Float)
    temperature_max: Mapped[float | None] = mapped_column(Float)


class SnmpInterfaceRollup(Base):
    """Downsampled traffic history: averages, peaks and error totals."""

    __tablename__ = "snmp_interface_rollup"
    __table_args__ = (
        UniqueConstraint(
            "interface_id", "bucket_start", "bucket_seconds", name="uq_snmp_if_bucket"
        ),
        Index("ix_snmp_if_rollup", "interface_id", "bucket_seconds", "bucket_start"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    interface_id: Mapped[int] = mapped_column(
        ForeignKey("snmp_interface.id", ondelete="CASCADE")
    )
    bucket_start: Mapped[datetime] = mapped_column(UTCDateTime)
    bucket_seconds: Mapped[int] = mapped_column(Integer)

    samples: Mapped[int] = mapped_column(Integer, default=0)
    in_avg: Mapped[float | None] = mapped_column(Float)
    in_max: Mapped[float | None] = mapped_column(Float)
    out_avg: Mapped[float | None] = mapped_column(Float)
    out_max: Mapped[float | None] = mapped_column(Float)
    in_errors: Mapped[int | None] = mapped_column(Integer)
    out_errors: Mapped[int | None] = mapped_column(Integer)


class Setting(Base, TimestampMixin):
    """Runtime settings a user edits in the UI.

    Deliberately not in the YAML file: SPARK is meant to be handed to someone
    else and configured through the browser, not by editing files over SSH.
    """

    __tablename__ = "setting"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)


DEFAULT_SETTINGS: dict[str, dict] = {
    # Set once, when spark.yaml's subnets have been copied into the subnet
    # table. Without it, deleting every subnet in the UI would be undone by the
    # next restart re-seeding them from the file.
    "network": {
        "subnets_seeded": False,
    },
    "discovery": {
        "enabled": True,
        "sweep_interval_seconds": 900,
        "port_scan_enabled": True,
        "port_scan_interval_seconds": 21600,
        "http_fingerprint": True,
    },
    "speedtest": {
        "enabled": True,
        "cron": "0 3 * * *",
        "method": "ookla",
        "iperf3_server": "",
        "suppress_alerts_during_test": True,
    },
    "alerting": {
        # Ciphertext from vault.py, never the URL. A webhook URL is a bearer
        # credential: anyone holding it can post to the channel.
        "discord_webhook_sealed": "",
        "enabled": True,
        # "HH:MM" in `timezone`; both empty means no quiet hours.
        "quiet_hours_start": "",
        "quiet_hours_end": "",
        # IANA name, taken from the browser that saved the settings.
        "timezone": "UTC",
        "notify_on_recovery": True,
        "notify_on_new_device": True,
        "notify_on_snmp": True,
    },
    # Threshold alerts from SNMP polling (snmp_alerts.py). Ports only alert
    # when starred on their device's page; the rest apply to every polled
    # device not on the mute list.
    "alert_rules": {
        "port_down": True,
        "port_busy": True, "port_busy_percent": 80, "port_busy_minutes": 10,
        "cpu": True, "cpu_percent": 90, "cpu_minutes": 10,
        "memory": True, "memory_percent": 90, "memory_minutes": 10,
        "temperature": True, "temperature_celsius": 80, "temperature_minutes": 5,
        # Storage (storage.py): TrueNAS pools and drives, filesystems anywhere.
        "pool_health": True,
        "pool_space": True, "pool_space_percent": 85,
        "disk_space": True, "disk_space_percent": 90,
        "drive_temperature": True, "drive_celsius": 50, "drive_minutes": 10,
        # API credentials (credentials.py): two failed checks in a row.
        "api_down": True,
        # Over the TrueNAS API (truenas_health.py): a drive not ONLINE or with
        # any errors; TrueNAS's own alerts at WARNING or above.
        "drive_errors": True,
        "truenas_alerts": True,
    },
    "snmp": {
        "poll_interval_seconds": 60,
    },
    # Merge suggestions from SNMP answered "Not the same", as
    # "<kept device id>:<address>" (identity.py).
    "merge_dismissed": {
        "pairs": [],
    },
    # Map suggestions answered "Not right", as "<device id>:<parent id>"
    # (topology.py).
    "map_dismissed": {
        "pairs": [],
    },
    # "manual": SNMP suggests, a person accepts. "automatic": SNMP's places
    # are applied after every read; places set by hand are kept (topology.py).
    "map": {
        "mode": "manual",
    },
    # Preferences. Empty time zone means "not chosen yet": prefs.py falls
    # back to the zone quiet hours used before this existed, then UTC.
    # idle_minutes is the sign-in timeout (prefs.IDLE_CHOICES).
    "preferences": {
        "timezone": "",
        "idle_minutes": 30,
    },
    "retention": {
        "raw_days": 7,
        "five_minute_days": 90,
        "hourly_days": 730,
    },
}
