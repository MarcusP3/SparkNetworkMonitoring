"""Preferences, reached from the account chip in the top bar.

Kept apart from Settings on purpose: Settings is what SPARK does on the
network; this is how SPARK presents itself to you: the time zone every time
on every page is shown in, and how long you stay signed in without using it.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.ext.asyncio import AsyncSession

from .. import prefs
from ..auth import end_idle_sessions
from ..config import Config
from ..models import User, utcnow
from .deps import get_config, get_session, redirect, require_user, templates

router = APIRouter()


async def _render(request: Request, session: AsyncSession, config: Config, user: User, *,
                  error: str | None = None, saved: str = "", status_code: int = 200):  # type: ignore[no-untyped-def]
    current = await prefs.get_timezone(session)
    idle = await prefs.get_idle_minutes(session)
    return templates.TemplateResponse(
        request,
        "preferences.html",
        {
            "config": config,
            "user": user,
            "title": "Preferences",
            "timezone": current,
            "choices": prefs.timezone_choices(),
            "idle_minutes": idle,
            "idle_label": prefs.idle_label(idle),
            "idle_choices": [(m, prefs.idle_label(m)) for m in prefs.IDLE_CHOICES],
            "proxy_mode": config.auth.mode == "proxy",
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
    return await _render(request, session, config, user,
                         saved=saved if saved in ("timezone", "session") else "")


@router.post("/preferences")
async def save_preferences(
    request: Request,
    timezone_name: str | None = Form(None, alias="timezone"),
    idle_minutes: str | None = Form(None),
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    """Each card posts its own field; whichever arrived is saved."""
    try:
        if timezone_name is not None:
            await prefs.set_timezone(session, timezone_name)
            saved = "timezone"
        elif idle_minutes is not None:
            previous = await prefs.set_idle_minutes(session, idle_minutes)
            # Sessions that had already timed out stay ended, even if the
            # timeout just went up. This request's own session was touched
            # moments ago, so it is never among them.
            new = await prefs.get_idle_minutes(session)
            await end_idle_sessions(session, timedelta(minutes=min(previous, new)))
            saved = "session"
        else:
            raise ValueError("Nothing to save.")
    except ValueError as exc:
        return await _render(request, session, config, user, error=str(exc), status_code=400)
    await session.commit()
    return redirect(f"/preferences?saved={saved}")
