"""Preferences: for now, the time zone SPARK shows times in.

One setting for the instance rather than one per user. SPARK has a single
admin account, and the time zone is also what quiet hours are kept in -- which
must be one answer, not one per browser.

Every stored time stays UTC. The zone is applied only when a time is shown:
the `local` template filter, chart labels, and quiet hours.
"""

from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_setting, save_setting

SETTING = "preferences"
DEFAULT_TIMEZONE = "UTC"


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


def local(when: datetime | None, tz, fmt: str) -> str:  # type: ignore[no-untyped-def]
    """Format a stored (UTC) time in a zone. "—" for None."""
    if when is None:
        return "—"
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(tz).strftime(fmt)
