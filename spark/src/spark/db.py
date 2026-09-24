"""Database engine, session handling, and schema migration.

Migrations are a numbered list of small async functions rather than Alembic.
For a single-file SQLite app that someone may hand to a friend, a migration
system you can read top to bottom in one screen is worth more than autogenerate.
Fresh installs get `create_all` and are stamped at the current version; existing
installs run only the steps they haven't seen.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from sqlalchemy import event, inspect, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import Config
from .models import (
    DEFAULT_SETTINGS,
    Base,
    CheckRollup,
    Setting,
    SnmpDevice,
    SnmpHealthRollup,
    SnmpHealthSample,
    SnmpInterface,
    SnmpInterfaceRollup,
    SnmpInterfaceSample,
    SnmpPoll,
    SnmpProfile,
    Subnet,
)

log = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def init_engine(config: Config) -> AsyncEngine:
    global _engine, _sessionmaker

    url = f"sqlite+aiosqlite:///{config.app.db_path}"
    engine = create_async_engine(url, echo=False, future=True)

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragmas(dbapi_connection, _record):  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        # WAL lets the pollers write while the dashboard reads. Without it,
        # every page load can block a check.
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    _engine = engine
    _sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    return engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        raise RuntimeError("Database not initialised; call init_engine() first")
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# --------------------------------------------------------------------------
# Migrations
# --------------------------------------------------------------------------

Migration = Callable[[AsyncSession], Awaitable[None]]

# Append to this list to change the schema. Never edit or reorder an entry that
# has shipped; a migration that has already run somewhere is history.
MIGRATIONS: list[tuple[int, str, Migration]] = []


def migration(version: int, description: str):  # type: ignore[no-untyped-def]
    def decorator(fn: Migration) -> Migration:
        MIGRATIONS.append((version, description, fn))
        return fn

    return decorator


@migration(2, "add the check_rollup table for downsampled history")
async def _add_check_rollup(session: AsyncSession) -> None:
    """Create check_rollup on databases that predate it.

    Built from the model's own metadata rather than hand-written DDL, so this
    cannot drift from what `create_all` gives a fresh install -- which is the
    classic way a migration passes on the author's machine and produces a
    subtly different table on everyone else's.
    """
    connection = await session.connection()
    await connection.run_sync(CheckRollup.__table__.create, checkfirst=True)


@migration(3, "add the subnet table so subnets can be managed in the UI")
async def _add_subnet(session: AsyncSession) -> None:
    """Create the subnet table on databases that predate it.

    Only the table. Copying spark.yaml's subnets into it needs the config
    object, which migrations deliberately do not get -- they take a session and
    nothing else, so they can be tested against a database alone. The seed runs
    at startup instead; see subnets.seed_from_config.
    """
    connection = await session.connection()
    await connection.run_sync(Subnet.__table__.create, checkfirst=True)


@migration(4, "record why an incident closed")
async def _add_incident_resolution(session: AsyncSession) -> None:
    """Add incident.resolution.

    Hand-written DDL rather than model metadata, because SQLite can add a
    column but SQLAlchemy has no portable "make this table match the model".
    `tests/test_incidents.py` compares a migrated database against `create_all`
    to catch the drift that invites.

    Checks first rather than assuming. A database created by `create_all` at a
    version this migration then runs against already has the column, and
    `ALTER TABLE` is not idempotent -- it raises "duplicate column name" and
    takes startup down with it. Any migration that adds a column has to
    tolerate the column already being there.
    """
    connection = await session.connection()
    existing = await connection.run_sync(
        lambda sync: {c["name"] for c in inspect(sync).get_columns("incident")}
    )
    if "resolution" in existing:
        log.info("incident.resolution already present; nothing to do")
        return
    await session.execute(text("ALTER TABLE incident ADD COLUMN resolution VARCHAR(32)"))


@migration(5, "close incidents left open by pausing a target while it was down")
async def _close_overlapping_incidents(session: AsyncSession) -> None:
    """Repair overlapping open incidents.

    Pausing a target while it was down never closed its incident, and resuming
    reset the status to UNKNOWN -- so the next failure looked like a fresh
    transition and opened another. Repeat, and one target has several incidents
    all claiming to be ongoing at the same time.

    Each superseded incident is closed at the moment the next one opened, which
    is the last instant it can honestly be said to have still been running. The
    newest is left open: if the target is still down, it genuinely is.
    """
    await session.execute(
        text(
            """
            UPDATE incident
               SET closed_at = (
                       SELECT MIN(later.opened_at)
                         FROM incident AS later
                        WHERE later.target_id = incident.target_id
                          AND later.opened_at > incident.opened_at
                   ),
                   resolution = 'superseded'
             WHERE closed_at IS NULL
               AND EXISTS (
                       SELECT 1
                         FROM incident AS later
                        WHERE later.target_id = incident.target_id
                          AND later.opened_at > incident.opened_at
                   )
            """
        )
    )



@migration(6, "add SNMP credential profiles and the devices that use them")
async def _add_snmp_tables(session: AsyncSession) -> None:
    """Create snmp_profile and snmp_device on databases that predate them.

    From model metadata with `checkfirst`, like migrations 2 and 3, so the
    result cannot drift from a fresh install's `create_all`. Profile first:
    snmp_device holds a foreign key to it.
    """
    connection = await session.connection()
    await connection.run_sync(SnmpProfile.__table__.create, checkfirst=True)
    await connection.run_sync(SnmpDevice.__table__.create, checkfirst=True)


@migration(7, "add the SNMP polling and history tables")
async def _add_snmp_polling_tables(session: AsyncSession) -> None:
    """Create the polling tables on databases that predate them.

    From model metadata with `checkfirst`, like every migration since 2.
    Interfaces before their samples and rollups, which reference them.
    """
    connection = await session.connection()
    for model in (
        SnmpPoll, SnmpInterface, SnmpHealthSample, SnmpInterfaceSample,
        SnmpHealthRollup, SnmpInterfaceRollup,
    ):
        await connection.run_sync(model.__table__.create, checkfirst=True)


CURRENT_VERSION = 7


async def _ensure_version_table(session: AsyncSession) -> None:
    await session.execute(
        text("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    )


async def _get_version(session: AsyncSession) -> int:
    await _ensure_version_table(session)
    row = (await session.execute(text("SELECT version FROM schema_version"))).first()
    return int(row[0]) if row else 0


async def _set_version(session: AsyncSession, version: int) -> None:
    await _ensure_version_table(session)
    await session.execute(text("DELETE FROM schema_version"))
    await session.execute(
        text("INSERT INTO schema_version (version) VALUES (:v)"), {"v": version}
    )


async def init_db(config: Config) -> None:
    engine = _engine or init_engine(config)

    async with engine.begin() as conn:
        existing = await conn.run_sync(
            lambda sync_conn: sync_conn.dialect.has_table(sync_conn, "user")
        )
        fresh = not existing
        if fresh:
            await conn.run_sync(Base.metadata.create_all)

    async with session_scope() as session:
        if fresh:
            await _set_version(session, CURRENT_VERSION)
            log.info("Created new database at %s", config.app.db_path)
        else:
            current = await _get_version(session)
            pending = sorted(
                (m for m in MIGRATIONS if m[0] > current), key=lambda m: m[0]
            )
            for version, description, fn in pending:
                log.info("Applying migration %s: %s", version, description)
                await fn(session)
                await _set_version(session, version)
            if pending:
                log.info("Database migrated to version %s", pending[-1][0])

        await _seed_settings(session)


async def _seed_settings(session: AsyncSession) -> None:
    """Add any settings keys this version knows about that the DB doesn't.

    Merges at the key level so an upgrade that adds a new option doesn't wipe
    what the user already configured.
    """
    existing = {
        row.key: row.value
        for row in (await session.execute(select(Setting))).scalars().all()
    }
    for key, defaults in DEFAULT_SETTINGS.items():
        if key not in existing:
            session.add(Setting(key=key, value=defaults))
        else:
            merged = {**defaults, **(existing[key] or {})}
            if merged != existing[key]:
                setting = await session.get(Setting, key)
                if setting is not None:
                    setting.value = merged


async def get_setting(session: AsyncSession, key: str) -> dict:
    setting = await session.get(Setting, key)
    if setting is None:
        return dict(DEFAULT_SETTINGS.get(key, {}))
    return dict(setting.value or {})


async def save_setting(session: AsyncSession, key: str, value: dict) -> None:
    setting = await session.get(Setting, key)
    if setting is None:
        session.add(Setting(key=key, value=value))
    else:
        setting.value = value


async def close_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
