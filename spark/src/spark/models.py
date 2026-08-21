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
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


# --------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------


class DeviceRole(str, enum.Enum):
    GATEWAY = "gateway"
    SWITCH = "switch"
    HOST = "host"
    CLIENT = "client"
    UNKNOWN = "unknown"


class HealthStatus(str, enum.Enum):
    UP = "up"
    DEGRADED = "degraded"
    DOWN = "down"
    PAUSED = "paused"
    UNKNOWN = "unknown"


class CheckType(str, enum.Enum):
    PING = "ping"
    TCP = "tcp"
    HTTP = "http"
    DNS = "dns"
    DOCKER = "docker"


class ServiceSource(str, enum.Enum):
    DOCKER = "docker"
    SCAN = "scan"
    MANUAL = "manual"


class DockerTransport(str, enum.Enum):
    SSH = "ssh"
    PROXY = "proxy"
    TLS = "tls"


class Severity(str, enum.Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


# --------------------------------------------------------------------------
# Inventory
# --------------------------------------------------------------------------


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
    role: Mapped[DeviceRole] = mapped_column(String(16), default=DeviceRole.UNKNOWN)

    # Declared hierarchy in v1; replaced by SNMP-derived parenting in Phase 3.
    parent_device_id: Mapped[int | None] = mapped_column(
        ForeignKey("device.id", ondelete="SET NULL")
    )
    subnet: Mapped[str | None] = mapped_column(String(64))

    status: Mapped[HealthStatus] = mapped_column(String(16), default=HealthStatus.UNKNOWN)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

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
    transport: Mapped[DockerTransport] = mapped_column(String(16), default=DockerTransport.SSH)
    uri: Mapped[str] = mapped_column(String(512))

    # Path to an SSH key or client-cert bundle on disk. Secret *material* never
    # goes in the database; only the reference to it does.
    secret_ref: Mapped[str | None] = mapped_column(String(512))

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
    source: Mapped[ServiceSource] = mapped_column(String(16), default=ServiceSource.SCAN)

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
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ignored: Mapped[bool] = mapped_column(Boolean, default=False)

    device = relationship("Device", back_populates="services")
    targets = relationship("Target", back_populates="service")


# --------------------------------------------------------------------------
# Monitoring
# --------------------------------------------------------------------------


class Target(Base, TimestampMixin):
    """A decision to watch something on a schedule."""

    __tablename__ = "target"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    check_type: Mapped[CheckType] = mapped_column(String(16))
    address: Mapped[str] = mapped_column(String(512))
    params: Mapped[dict] = mapped_column(JSON, default=dict)

    device_id: Mapped[int | None] = mapped_column(ForeignKey("device.id", ondelete="CASCADE"))
    service_id: Mapped[int | None] = mapped_column(ForeignKey("service.id", ondelete="CASCADE"))

    interval_seconds: Mapped[int] = mapped_column(Integer, default=60)
    timeout_seconds: Mapped[float] = mapped_column(Float, default=5.0)

    # Hysteresis: how many consecutive results before we believe a state change.
    # The difference between a tool you trust and one you mute in a week.
    failure_threshold: Mapped[int] = mapped_column(Integer, default=3)
    recovery_threshold: Mapped[int] = mapped_column(Integer, default=2)

    # Dependency suppression: if the parent target is down, this one's failure
    # is a symptom, not news. One alert for the switch, not thirty.
    depends_on_target_id: Mapped[int | None] = mapped_column(
        ForeignKey("target.id", ondelete="SET NULL")
    )

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    muted_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    status: Mapped[HealthStatus] = mapped_column(String(16), default=HealthStatus.UNKNOWN)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_successes: Mapped[int] = mapped_column(Integer, default=0)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status_change: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    service = relationship("Service", back_populates="targets")
    depends_on = relationship("Target", remote_side=[id], backref="dependents")


class CheckResult(Base):
    """The hot table. Downsampled nightly: raw 7d, 5-minute 90d, hourly 2y."""

    __tablename__ = "check_result"
    __table_args__ = (Index("ix_check_result_target_ts", "target_id", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("target.id", ondelete="CASCADE"))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    status: Mapped[HealthStatus] = mapped_column(String(16))
    latency_ms: Mapped[float | None] = mapped_column(Float)
    detail: Mapped[str | None] = mapped_column(Text)

    # Set during a speed test, when the WAN is deliberately saturated. Flagged
    # samples are excluded from trend charts so a nightly test doesn't look
    # like a nightly outage.
    suppressed: Mapped[bool] = mapped_column(Boolean, default=False)


class Incident(Base):
    __tablename__ = "incident"
    __table_args__ = (Index("ix_incident_target_opened", "target_id", "opened_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("target.id", ondelete="CASCADE"))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    severity: Mapped[Severity] = mapped_column(String(16), default=Severity.CRITICAL)
    cause: Mapped[str | None] = mapped_column(Text)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

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
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
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
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
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
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserSession(Base):
    __tablename__ = "user_session"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    # Only the hash is stored, so a database leak doesn't hand over live sessions.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    ip: Mapped[str | None] = mapped_column(String(45))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class LoginAttempt(Base):
    __tablename__ = "login_attempt"
    __table_args__ = (Index("ix_login_attempt_ip_ts", "ip", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ip: Mapped[str] = mapped_column(String(45))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
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
