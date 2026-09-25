"""Shared FastAPI dependencies and the Jinja environment."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from sqlalchemy.ext.asyncio import AsyncSession

from .. import prefs
from ..auth import SESSION_COOKIE, resolve_proxy_user, resolve_session, setup_required
from ..config import Config
from ..db import get_sessionmaker
from ..models import User

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


def _asset_version() -> str:
    """A cache key that changes exactly when a static asset does.

    Without this, /static/app.css is a stable URL and browsers keep serving
    the copy they already have. Templates are rendered per request so they
    update the moment a new image starts, but the CSS does not -- which shows
    up as a deploy that looks half-applied, and costs you a hard refresh to
    diagnose every single time.

    A content hash rather than the app version: it changes when the file
    changes, which is the actual thing that matters, and never when it doesn't.
    """
    try:
        # A cache key, not a checksum anyone relies on; MD5 is fine for that
        # and the flag says so, so a FIPS build does not refuse to start.
        # Every hand-written asset the templates version with it: the
        # stylesheet and, since the device page, charts.js. One key for both,
        # so a change to either reaches browsers on the next load.
        digest = hashlib.md5(usedforsecurity=False)
        for name in ("app.css", "charts.js"):
            digest.update((STATIC_DIR / name).read_bytes())
        return digest.hexdigest()[:8]
    except OSError:
        # A missing stylesheet is the static mount's problem, not ours.
        return "dev"


def _app_version() -> str:
    """The version for the footer.

    Read from installed package metadata rather than written into a template,
    so it cannot drift from what `pyproject.toml` says. A source checkout that
    was never installed has no metadata, and "dev" is the honest answer there
    -- better than a number that stopped being true three releases ago.
    """
    try:
        from importlib.metadata import version

        # The distribution name from pyproject.toml, not the import name. They
        # differ here -- the package is `spark`, the distribution is
        # `spark-monitor` -- and asking for the wrong one raises
        # PackageNotFoundError, which this used to swallow into "dev".
        return version("spark-monitor")
    except Exception:  # noqa: BLE001 - a footer must never take a page down
        return "dev"


@pass_context
def _local(context, when, fmt: str = "%b %-d, %H:%M") -> str:  # type: ignore[no-untyped-def]
    """`{{ some_time | local }}`: a stored UTC time, in the chosen time zone.

    The zone comes from the request (set by require_user); pages rendered
    before sign-in have none and show UTC, which is what they said before.
    """
    request = context.get("request")
    tz = getattr(getattr(request, "state", None), "tz", None) or prefs.zone_of(None)
    return prefs.local(when, tz, fmt)


templates.env.filters["local"] = _local
templates.env.globals["asset_version"] = _asset_version()
templates.env.globals["app_version"] = _app_version()


class RedirectException(Exception):
    """Raised by dependencies that need to bounce the browser somewhere."""

    def __init__(self, location: str, status_code: int = 303) -> None:
        self.location = location
        self.status_code = status_code
        super().__init__(location)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def get_config(request: Request) -> Config:
    return request.app.state.config


async def current_user(
    request: Request,
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
) -> User | None:
    if config.auth.mode == "proxy":
        return await resolve_proxy_user(session, request, config.auth)
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return await resolve_session(session, token)


async def require_user(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User | None = Depends(current_user),
) -> User:
    if user is not None:
        # Every signed-in page shows times in the chosen zone. One indexed
        # read of a settings row, here rather than in every route.
        name = await prefs.get_timezone(session)
        request.state.tz_name = name
        request.state.tz = prefs.zone_of(name)
        return user
    if await setup_required(session):
        raise RedirectException("/setup")
    nxt = request.url.path
    suffix = f"?next={nxt}" if nxt and nxt != "/" else ""
    raise RedirectException(f"/login{suffix}")


def redirect(location: str, status_code: int = 303) -> RedirectResponse:
    return RedirectResponse(location, status_code=status_code)
