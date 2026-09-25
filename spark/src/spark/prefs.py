"""Preferences: the time zone SPARK shows times in, and the sign-in timeout.

Settings for the instance rather than per user. SPARK has a single admin
account, and the time zone is also what quiet hours are kept in -- which must
be one answer, not one per browser.

Every stored time stays UTC. The zone is applied only when a time is shown:
the `local` template filter, chart labels, and quiet hours.

The timeout is how long a signed-in session may sit unused before it ends.
auth.resolve_session enforces it; this module only stores it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_setting, save_setting

SETTING = "preferences"
DEFAULT_TIMEZONE = "UTC"

# Minutes of inactivity before a session ends. A fixed list, not a free number:
# nobody needs 37 minutes, and a list cannot be set to 0 or to a year.
# No "never" on purpose -- SPARK holds a map of the network.
IDLE_CHOICES = (15, 30, 60, 120, 240, 480, 1440)
DEFAULT_IDLE_MINUTES = 30


@lru_cache(maxsize=1)
def timezone_choices() -> tuple[str, ...]:
    """Every IANA zone this system knows, "UTC" first, then by name.

    From the system's tz database (the image installs `tzdata`). The legacy
    aliases ("US/Central", "EST5EDT") are left out: they are the same zones
    under names nobody should be choosing in 2026.
    """
    names = {
        name for name in available_timezones()
        if "/" in name and not name.startswith(("Etc/", "SystemV/", "US/", "posix/", "right/"))
    }
    return (DEFAULT_TIMEZONE, *sorted(names))


def valid(name: str | None) -> bool:
    if not name:
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def zone_of(name: str | None) -> ZoneInfo | timezone:
    """The zone for a name, or UTC for anything unusable. Never raises."""
    if valid(name):
        return ZoneInfo(name)  # type: ignore[arg-type]
    return timezone.utc


async def get_timezone(session: AsyncSession) -> str:
    """The saved time zone name.

    Falls back to the zone quiet hours were saved in by the version before
    this setting existed, so upgrading does not move anyone's quiet hours.
    """
    saved = (await get_setting(session, SETTING)).get("timezone")
    if valid(saved):
        return saved  # type: ignore[return-value]
    legacy = (await get_setting(session, "alerting")).get("timezone")
    return legacy if valid(legacy) else DEFAULT_TIMEZONE


async def set_timezone(session: AsyncSession, name: str) -> None:
    """Save a time zone. ValueError with a message if it is not one."""
    name = (name or "").strip()
    if not valid(name):
        raise ValueError(f"{name!r} is not a time zone SPARK knows.")
    settings = await get_setting(session, SETTING)
    settings["timezone"] = name
    await save_setting(session, SETTING, settings)


def idle_label(minutes: int) -> str:
    """ "15 minutes", "1 hour", "24 hours"."""
    if minutes % 60:
        return f"{minutes} minutes"
    hours = minutes // 60
    return f"{hours} hour{'' if hours == 1 else 's'}"


async def get_idle_minutes(session: AsyncSession) -> int:
    """Minutes of inactivity a session is allowed. 30 unless changed."""
    saved = (await get_setting(session, SETTING)).get("idle_minutes")
    return saved if saved in IDLE_CHOICES else DEFAULT_IDLE_MINUTES


async def set_idle_minutes(session: AsyncSession, value: str | int) -> int:
    """Save the timeout; returns the previous one. ValueError if not a choice."""
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        minutes = -1
    if minutes not in IDLE_CHOICES:
        raise ValueError(f"{value!r} is not one of the timeout choices.")
    previous = await get_idle_minutes(session)
    settings = await get_setting(session, SETTING)
    settings["idle_minutes"] = minutes
    await save_setting(session, SETTING, settings)
    return previous


def local(when: datetime | None, tz, fmt: str) -> str:  # type: ignore[no-untyped-def]
    """Format a stored (UTC) time in a zone. "—" for None."""
    if when is None:
        return "—"
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(tz).strftime(fmt)
