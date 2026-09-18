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


class Notification(Base):
    """Outbound log. Prevents duplicate sends and gives an audit trail."""

    __tablename__ = "notification"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    channel: Mapped[str] = mapped_column(String(32), default="discord")
    incident_id: Mapped[int | None] = mapped_column(
        ForeignKey("incident.id", ondelete="SET NULL")
    )
    subject: Mapped[str | None] = mapped_column(String(512))
    body: Mapped[str | None] = mapped_column(Text)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
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
        "discord_webhook_url": "",
        "enabled": True,
        "quiet_hours_start": "",
        "quiet_hours_end": "",
        "notify_on_recovery": True,
        "notify_on_new_device": True,
    },
    "retention": {
        "raw_days": 7,
        "five_minute_days": 90,
        "hourly_days": 730,
    },
}
