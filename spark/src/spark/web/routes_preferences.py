"""Preferences, reached from the account chip in the top bar.

Kept apart from Settings on purpose: Settings is what SPARK does on the
network; this is how SPARK presents itself to you: the time zone every time
on every page is shown in, how long you stay signed in without using it,
whether the network map is placed by you or by SNMP (with Wipe map), and --
the Account card -- your password and where you are signed in.

The Account card exists because there was no way to change the password
short of editing the database (review finding #24). Changing it ends every
session, this one included, and hands the person a fresh one; "Sign out
everywhere else" ends every session but this one. `spark-reset-password`
(cli.py) is the way back in when the password is lost.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.ext.asyncio import AsyncSession

from .. import limits
from .. import prefs, topology
from ..auth import (MIN_PASSWORD_LENGTH, AuthError, change_password, client_ip, create_session,
                    end_idle_sessions, list_sessions, note_password_changed,
                    revoke_other_sessions, session_token)
from ..config import Config
from ..models import User, utcnow
from .deps import get_config, get_session, redirect, require_user, templates
from .routes_auth import _set_session_cookie

router = APIRouter()


async def _render(request: Request, session: AsyncSession, config: Config, user: User, *,
                  error: str | None = None, saved: str = "", status_code: int = 200,
                  account_error: str | None = None, ended: int | None = None):  # type: ignore[no-untyped-def]
    current = await prefs.get_timezone(session)
    idle = await prefs.get_idle_minutes(session)
    placed = request.query_params.get("placed", "")
    proxy_mode = config.auth.mode == "proxy"
    sessions = [] if proxy_mode else await list_sessions(
        session, user.id, session_token(request), timedelta(minutes=idle))
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
            "proxy_mode": proxy_mode,
            "map_mode": await topology.get_mode(session),
            "placed": int(placed) if placed.isdigit() and len(placed) < 6 else None,
            "now": utcnow(),
            "error": error,
            "saved": saved,
            "account_error": account_error,
            "sessions": sessions,
            "others": sum(1 for _, current_one in sessions if not current_one),
            "ended": ended,
            "min_password": MIN_PASSWORD_LENGTH,
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
    ended = request.query_params.get("ended", "")
    return await _render(request, session, config, user,
                         saved=saved if saved in ("timezone", "session", "map", "password",
                                                  "sessions") else "",
                         ended=int(ended) if ended.isdigit() and len(ended) < 6 else None)


@router.post("/preferences/password")
async def save_password(
    request: Request,
    current_password: str = Form("", max_length=limits.PASSWORD),
    new_password: str = Form("", max_length=limits.PASSWORD),
    new_password_confirm: str = Form("", max_length=limits.PASSWORD),
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    """Change the password. Every session ends, and this browser gets a new one."""
    if config.auth.mode == "proxy":
        return redirect("/preferences")
    if new_password != new_password_confirm:
        return await _render(request, session, config, user, status_code=400,
                             account_error="The two new passwords do not match.")
    try:
        await change_password(session, user, current_password, new_password)
    except AuthError as exc:
        return await _render(request, session, config, user, status_code=400,
                             account_error=str(exc))
    await note_password_changed(session, "changed under Preferences", client_ip(request, config.auth))
    token = await create_session(session, user, config.auth,
                                 user_agent=request.headers.get("user-agent"),
                                 ip=client_ip(request, config.auth))
    await session.commit()      # before the redirect; see routes_auth.login_submit
    response = redirect("/preferences?saved=password#account")
    _set_session_cookie(request, response, token, config)
    return response


@router.post("/preferences/sessions/end-others")
async def end_other_sessions(
    request: Request,
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    """Sign out everywhere else: every session but the one that pressed the button."""
    if config.auth.mode == "proxy":
        return redirect("/preferences")
    token = session_token(request) or ""
    ended = await revoke_other_sessions(session, user.id, token)
    await session.commit()
    return redirect(f"/preferences?saved=sessions&ended={ended}#account")


@router.post("/preferences")
async def save_preferences(
    request: Request,
    timezone_name: str | None = Form(None, alias="timezone", max_length=limits.TIMEZONE),
    idle_minutes: str | None = Form(None, max_length=limits.SHORT),
    map_mode: str | None = Form(None, max_length=limits.SHORT),
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
        elif map_mode is not None:
            applied = await topology.set_mode(session, map_mode)
            await session.commit()
            return redirect(f"/preferences?saved=map&placed={applied.placed + applied.moved}#map")
        else:
            raise ValueError("Nothing to save.")
    except ValueError as exc:
        return await _render(request, session, config, user, error=str(exc), status_code=400)
    await session.commit()
    return redirect(f"/preferences?saved={saved}")


@router.get("/preferences/wipe-map")
async def wipe_map_confirm(
    request: Request,
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    """Say what Wipe map clears before it does."""
    return templates.TemplateResponse(
        request, "wipe_map.html",
        {"config": config, "user": user, "title": "Wipe the map",
         "counts": await topology.wipe_counts(session),
         "map_mode": await topology.get_mode(session)},
    )


@router.post("/preferences/wipe-map")
async def wipe_map(
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(require_user),
):
    _counts, applied = await topology.wipe(session)
    await session.commit()
    return redirect(f"/map?wiped={applied.placed}")
