"""First-run setup, login, and logout."""

from __future__ import annotations

import logging
from datetime import timedelta
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from .. import limits
from ..auth import (
    SECURE_SESSION_COOKIE,
    SESSION_COOKIE,
    AuthError,
    RateLimited,
    announce_setup_code,
    authenticate,
    check_rate_limit,
    client_ip,
    create_admin,
    create_session,
    new_setup_code,
    note_lockout,
    note_password_changed,
    note_sign_in,
    record_attempt,
    resolve_session,
    revoke_session,
    seconds_left,
    session_token,
    setup_code_matches,
    setup_required,
    cookie_name,
)
from ..config import Config
from .. import prefs, topology
from ..alerts import AlertError, set_webhook, validate_webhook
from ..vault import vault_for
from .deps import current_user, get_config, get_session, redirect, templates

log = logging.getLogger(__name__)

router = APIRouter()


def _set_session_cookie(request: Request, response, token: str, config: Config) -> None:  # type: ignore[no-untyped-def]
    response.set_cookie(
        # __Host-spark_session over HTTPS: the browser then refuses to send
        # it over http or to let an http page overwrite it (auth.cookie_name).
        cookie_name(request),
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


SETUP_CODE_HELP = ("The setup code is in SPARK's log: `docker compose logs spark` "
                   "on the machine it runs on.")


def setup_code_for(request: Request) -> str:
    """The code this run of SPARK accepts, made and logged the first time it
    is needed. Cleared once the administrator exists."""
    state = request.app.state
    code = getattr(state, "setup_code", None)
    if not code:
        code = state.setup_code = new_setup_code()
        config = state.config
        announce_setup_code(code, config.app.host, config.app.port)
    return code


def _setup_page(request: Request, config: Config, *, status_code: int = 200, **context):  # type: ignore[no-untyped-def]
    return templates.TemplateResponse(
        request, "setup.html",
        {"config": config, "title": "Set up SPARK", "setup_help": SETUP_CODE_HELP,
         "timezone_choices": prefs.timezone_choices(), **context},
        status_code=status_code,
    )


@router.get("/setup")
async def setup_form(
    request: Request,
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
):
    if not await setup_required(session):
        return redirect("/login")
    setup_code_for(request)  # logged now, so it is in the log by the time the page is read
    return _setup_page(request, config)


@router.post("/setup")
async def setup_submit(
    request: Request,
    setup_code: str = Form("", max_length=limits.SHORT),
    username: str = Form("admin", max_length=limits.USERNAME),
    password: str = Form(..., max_length=limits.PASSWORD),
    password_confirm: str = Form(..., max_length=limits.PASSWORD),
    discord_webhook_url: str = Form("", max_length=limits.WEBHOOK),
    timezone_name: str = Form("", alias="timezone", max_length=limits.TIMEZONE),
    map_mode: str = Form("manual", max_length=limits.SHORT),
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
):
    if not await setup_required(session):
        return redirect("/login")

    # The code first, under the same lockout as a login: a wrong code is a
    # failed attempt from this address, and ten of them close the door for
    # lockout_minutes. Nothing else on the form is looked at until it matches.
    ip = client_ip(request, config.auth)
    try:
        await check_rate_limit(session, ip, config.auth)
    except RateLimited as exc:
        await note_lockout(session, ip, config.auth, "Setup code")
        await session.commit()
        minutes = max(1, exc.retry_after_seconds // 60)
        return _setup_page(request, config, status_code=429,
                           error=f"Too many wrong setup codes. Try again in {minutes} minutes.",
                           username=username)
    if not setup_code_matches(setup_code_for(request), setup_code):
        await record_attempt(session, ip, ok=False)
        await session.commit()
        return _setup_page(request, config, status_code=400,
                           error="That is not the setup code. " + SETUP_CODE_HELP.replace("`", ""),
                           username=username, map_mode=map_mode)

    error: str | None = None
    if map_mode not in topology.MODES:
        error = "Choose Manual or Automatic for the network map."
    webhook = discord_webhook_url.strip()
    if error is None and password != password_confirm:
        error = "The two passwords do not match."
    elif error is None and webhook:
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
        return _setup_page(request, config, status_code=400, error=error, username=username,
                           timezone=tz if prefs.valid(tz) else None, map_mode=map_mode,
                           # A right code is kept on the page, so a typo in the
                           # password does not mean reading the log again.
                           setup_code=setup_code)

    if webhook:
        await set_webhook(session, vault_for(config), webhook)
    if tz:
        await prefs.set_timezone(session, tz)
    await topology.set_mode(session, map_mode)
    from ..alerts import on_security_event

    await on_security_event(
        session, kind="security_setup", tone="info",
        subject=f"SPARK set up: administrator {user.username!r} created from {ip}",
        body="This is the first-run setup completing. If it was not you, the setup code "
             "was read from the log by someone else.")

    token = await create_session(
        session,
        user,
        config.auth,
        user_agent=request.headers.get("user-agent"),
        ip=ip,
    )
    # Committed here, not left to get_session: its commit runs after the
    # response has gone, and the browser follows this redirect at once -- so
    # the next request could arrive before the session row existed, and bounce
    # straight back to the sign-in page.
    await session.commit()
    # Spent. A second claimant with the same code now meets "already exists".
    request.app.state.setup_code = None
    log.info("First-run setup completed from %s", ip)
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
    username: str = Form(..., max_length=limits.USERNAME),
    password: str = Form(..., max_length=limits.PASSWORD),
    next: str = Form("/", max_length=limits.URL),
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
        await note_lockout(session, ip, config.auth, "Sign-in")
        await session.commit()
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
        # The failed attempt counts toward the lockout from this moment, not
        # from whenever get_session's commit runs after the page has gone.
        await session.commit()
        return templates.TemplateResponse(
            request,
            "login.html",
            {"config": config, "title": "Sign in", "error": str(exc), "next": next},
            status_code=401,
        )

    await note_sign_in(session, user, ip, request.headers.get("user-agent"))
    token = await create_session(
        session,
        user,
        config.auth,
        user_agent=request.headers.get("user-agent"),
        ip=ip,
    )
    # Committed here, not left to get_session: its commit runs after the
    # response has gone, and the browser follows this redirect at once -- so
    # the next request could arrive before the session row existed, and bounce
    # straight back to the sign-in page.
    await session.commit()
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
    token = session_token(request)
    if token:
        await revoke_session(session, token)
        await session.commit()      # before the redirect; see login_submit
    response = redirect("/login")
    response.delete_cookie(SESSION_COOKIE, path="/")
    # A __Host- cookie can only be cleared by a Secure cookie of the same name.
    response.delete_cookie(SECURE_SESSION_COOKIE, path="/", secure=True)
    return response


# --------------------------------------------------------------------------
# Idle timeout
# --------------------------------------------------------------------------


async def _session_answer(session: AsyncSession, request: Request, *, touch: bool):  # type: ignore[no-untyped-def]
    config = request.app.state.config
    if config.auth.mode == "proxy":
        # No timeout of ours to report: the proxy decides.
        return JSONResponse({"remaining": None}, status_code=404)
    token = session_token(request)
    idle = timedelta(minutes=await prefs.get_idle_minutes(session))
    if token and touch:
        await resolve_session(session, token, idle=idle, touch=True)
        await session.commit()      # the page's next check may be moments away
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
