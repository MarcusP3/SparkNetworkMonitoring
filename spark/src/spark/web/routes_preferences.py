"""Preferences, reached from the account chip in the top bar.

Kept apart from Settings on purpose: Settings is what SPARK does on the
network; this is how SPARK presents itself to you. For now that is one
thing, the time zone every time on every page is shown in.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.ext.asyncio import AsyncSession

from .. import prefs
from ..config import Config
from ..models import User, utcnow
from .deps import get_config, get_session, redirect, require_user, templates

router = APIRouter()


async def _render(request: Request, session: AsyncSession, config: Config, user: User, *,
                  error: str | None = None, saved: bool = False, status_code: int = 200):  # type: ignore[no-untyped-def]
    current = await prefs.get_timezone(session)
    return templates.TemplateResponse(
        request,
        "preferences.html",
        {
            "config": config,
            "user": user,
            "title": "Preferences",
            "timezone": current,
            "choices": prefs.timezone_choices(),
            "now": utcnow(),
            "error": error,
            "saved": saved,
        },
        status_code=status_code,
    )


@router.get("/preferences")
async def preferences_page(
    request: Request,
    saved: str = "",
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    return await _render(request, session, config, user, saved=saved == "1")


@router.post("/preferences")
async def save_preferences(
    request: Request,
    timezone_name: str = Form("", alias="timezone"),
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    try:
        await prefs.set_timezone(session, timezone_name)
    except ValueError as exc:
        return await _render(request, session, config, user, error=str(exc), status_code=400)
    await session.commit()
    return redirect("/preferences?saved=1")
