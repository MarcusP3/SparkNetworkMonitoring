"""First-run setup, login, and logout."""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import (
    SESSION_COOKIE,
    AuthError,
    RateLimited,
    authenticate,
    client_ip,
    create_admin,
    create_session,
    resolve_session,
    revoke_session,
    seconds_left,
    setup_required,
)
from ..config import Config
from .. import prefs
from ..alerts import AlertError, set_webhook, validate_webhook
from ..vault import vault_for
from .deps import current_user, get_config, get_session, redirect, templates

router = APIRouter()


def _set_session_cookie(request: Request, response, token: str, config: Config) -> None:  # type: ignore[no-untyped-def]
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=config.auth.session_days * 86400,
        httponly=True,
        samesite="lax",
        # Secure exactly when the login itself arrived over TLS. Forcing it
        # would break the common case -- plain HTTP on a LAN, where a Secure
        # cookie is silently never sent -- and never setting it left a
        # TLS-fronted install sending its session over any http:// link. The
        # scheme is uvicorn's view: direct, or from X-Forwarded-Proto when the
        # proxy is one it trusts (`--forwarded-allow-ips`).
        secure=request.url.scheme == "https",
        path="/",
    )


def _safe_next(target: str) -> str:
    """A same-site path, or "/". An open redirect here is a phishing primitive.

    A startswith("/") and not startswith("//") test is not enough: browsers
    normalise a backslash to a forward slash while parsing, so "/\\evil.com"
    passes that check and then navigates to //evil.com. Parse it instead and
    require that no scheme and no host survived.
    """
    if not target:
        return "/"
    parsed = urlparse(target.replace("\\", "/"))
    if parsed.scheme or parsed.netloc:
        return "/"
    # An empty authority parses to netloc="" while leaving the slashes in the
    # path, so "////evil.com" arrives here as path="//evil.com" with nothing in
    # netloc. Emitting that verbatim is a protocol-relative redirect, i.e. the
    # hole this function exists to close, one level further down.
    path = parsed.path
    if not path.startswith("/") or path.startswith("//"):
        return "/"
    return path + (f"?{parsed.query}" if parsed.query else "")


# --------------------------------------------------------------------------
# First-run setup
# --------------------------------------------------------------------------


@router.get("/setup")
async def setup_form(
    request: Request,
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
):
    if not await setup_required(session):
        return redirect("/login")
    return templates.TemplateResponse(
        request,
        "setup.html",
        {"config": config, "title": "Set up SPARK",
         "timezone_choices": prefs.timezone_choices()},
    )


@router.post("/setup")
async def setup_submit(
    request: Request,
    username: str = Form("admin"),
    password: str = Form(...),
    password_confirm: str = Form(...),
    discord_webhook_url: str = Form(""),
    timezone_name: str = Form("", alias="timezone"),
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
):
    if not await setup_required(session):
        return redirect("/login")

    error: str | None = None
    webhook = discord_webhook_url.strip()
    if password != password_confirm:
        error = "The two passwords do not match."
    elif webhook:
        try:
            validate_webhook(webhook)
        except AlertError as exc:
            error = str(exc)
    tz = timezone_name.strip()
    if error is None and tz and not prefs.valid(tz):
        error = f"{tz!r} is not a time zone SPARK knows."
    if error is None:
        try:
            user = await create_admin(session, username, password)
        except AuthError as exc:
            error = str(exc)

    if error:
        return templates.TemplateResponse(
            request,
            "setup.html",
            {"config": config, "title": "Set up SPARK", "error": error, "username": username,
             "timezone": tz if prefs.valid(tz) else None,
             "timezone_choices": prefs.timezone_choices()},
            status_code=400,
        )

    if webhook:
        await set_webhook(session, vault_for(config), webhook)
    if tz:
        await prefs.set_timezone(session, tz)

    token = await create_session(
        session,
        user,
        config.auth,
        user_agent=request.headers.get("user-agent"),
        ip=client_ip(request, config.auth),
    )
    response = redirect("/")
    _set_session_cookie(request, response, token, config)
    return response


# --------------------------------------------------------------------------
# Login / logout
# --------------------------------------------------------------------------


@router.get("/login")
async def login_form(
    request: Request,
    next: str = "/",
    expired: str = "",
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
):
    if await setup_required(session):
        return redirect("/setup")
    if config.auth.mode == "proxy":
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "config": config,
                "title": "Sign in",
                "proxy_mode": True,
                "error": "This instance authenticates through a reverse proxy, "
                "but no valid identity header arrived.",
            },
            status_code=401,
        )
    notice = None
    if expired:
        minutes = await prefs.get_idle_minutes(session)
        notice = (f"You were signed out after {prefs.idle_label(minutes)} without "
                  "activity. Sign in to carry on where you were.")
    return templates.TemplateResponse(
        request, "login.html",
        {"config": config, "title": "Sign in", "next": next, "notice": notice},
    )


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
):
    if config.auth.mode == "proxy":
        # The proxy is the authenticator. This route still verified passwords
        # in proxy mode -- and minted a cookie nothing would read -- which
        # left the admin password open to guessing, from behind the proxy,
        # on an instance that never asks for it.
        return redirect("/login")

    ip = client_ip(request, config.auth)
    try:
        user = await authenticate(session, username, password, ip, config.auth)
    except RateLimited as exc:
        minutes = max(1, exc.retry_after_seconds // 60)
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "config": config,
                "title": "Sign in",
                "error": f"Too many failed attempts. Try again in {minutes} minutes.",
                "next": next,
            },
            status_code=429,
        )
    except AuthError as exc:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"config": config, "title": "Sign in", "error": str(exc), "next": next},
            status_code=401,
        )

    token = await create_session(
        session,
        user,
        config.auth,
        user_agent=request.headers.get("user-agent"),
        ip=ip,
    )
    destination = _safe_next(next)
    response = redirect(destination)
    _set_session_cookie(request, response, token, config)
    return response


@router.post("/logout")
async def logout(
    request: Request,
    session: AsyncSession = Depends(get_session),
    _user=Depends(current_user),
):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await revoke_session(session, token)
    response = redirect("/login")
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


# --------------------------------------------------------------------------
# Idle timeout
# --------------------------------------------------------------------------


async def _session_answer(session: AsyncSession, request: Request, *, touch: bool):  # type: ignore[no-untyped-def]
    config = request.app.state.config
    if config.auth.mode == "proxy":
        # No timeout of ours to report: the proxy decides.
        return JSONResponse({"remaining": None}, status_code=404)
    token = request.cookies.get(SESSION_COOKIE)
    idle = timedelta(minutes=await prefs.get_idle_minutes(session))
    if token and touch:
        await resolve_session(session, token, idle=idle, touch=True)
    left = await seconds_left(session, token, idle) if token else 0
    return JSONResponse({"remaining": left}, status_code=200 if left else 401,
                        headers={"Cache-Control": "no-store"})


@router.get("/session")
async def session_status(request: Request, session: AsyncSession = Depends(get_session)):
    """Seconds until this session times out. Does not count as activity.

    Asked by the page's timer (base.html) when it thinks time is up, since
    another tab may have kept the session going in the meantime.
    """
    return await _session_answer(session, request, touch=False)


@router.post("/session")
async def session_touch(request: Request, session: AsyncSession = Depends(get_session)):
    """Typing or clicking on a page: counts as activity, like a page load.

    Without it, filling in a long form without saving would time out under
    you. The page sends it at most once a minute.
    """
    return await _session_answer(session, request, touch=True)
