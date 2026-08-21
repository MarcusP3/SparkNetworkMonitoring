"""Shared FastAPI dependencies and the Jinja environment."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import SESSION_COOKIE, resolve_proxy_user, resolve_session, setup_required
from ..config import Config
from ..db import get_sessionmaker
from ..models import User

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


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
        return user
    if await setup_required(session):
        raise RedirectException("/setup")
    nxt = request.url.path
    suffix = f"?next={nxt}" if nxt and nxt != "/" else ""
    raise RedirectException(f"/login{suffix}")


def redirect(location: str, status_code: int = 303) -> RedirectResponse:
    return RedirectResponse(location, status_code=status_code)
