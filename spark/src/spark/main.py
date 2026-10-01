"""Application entry point."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from . import scheduler as scheduler_module
from . import tls
from .config import Config, load_config
from .db import close_engine, init_db, init_engine, session_scope
from .web.deps import RedirectException, templates
from .web.hardening import Hardening
from .web.routes_auth import router as auth_router
from .web.routes_dashboard import router as dashboard_router
from .web.routes_device_page import router as device_page_router
from .web.routes_devices import router as devices_router
from .web.routes_map import router as map_router
from .web.routes_credentials import router as credentials_router
from .web.routes_suppressions import router as suppressions_router
from .web.routes_preferences import router as preferences_router
from .web.routes_events import router as events_router
from .web.routes_settings import router as settings_router
from .web.routes_targets import router as targets_router

STATIC_DIR = Path(__file__).resolve().parent / "static"

# How long uvicorn waits for open connections before it stops waiting and
# runs the shutdown anyway. Needed since HTTPS: closing a TLS connection
# sends close_notify and waits up to 30 s for the peer's, and a browser with
# a tab open (or anything holding an idle keep-alive connection) is not
# reading, so the wait runs out. Docker sends SIGTERM, allows 10 s, then
# SIGKILLs -- so without this, every `docker compose restart` with a tab open
# ended in a kill, no scheduler shutdown, no engine dispose. Measured:
# 30.1 s to exit over HTTPS, 0.2 s over HTTP, 3.2 s with this
# (performance review, 2026-10-01, P2). Compose sets stop_grace_period above it.
GRACEFUL_SHUTDOWN_SECONDS = 3

log = logging.getLogger("spark")


def require_writable(data_dir: Path) -> None:
    """Fail at startup, with the fix in the message, if SPARK cannot write its data.

    Checked by writing, not by `os.access`: that answers for the real uid and
    ignores ACLs, and a probe file is the only test that agrees with what
    SQLite is about to attempt.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    probe = data_dir / ".write-test"
    try:
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as exc:
        raise SystemExit(
            f"SPARK cannot write to its data directory {data_dir} ({exc}).\n"
            f"The container runs as uid {os.getuid()}, not root. On the host, run:\n"
            f"    sudo chown -R {os.getuid()}:{os.getgid()} ./data\n"
            "from the spark/ directory (the one docker-compose.yml is in)."
        ) from exc


def keep_data_private(data_dir: Path) -> None:
    """Everything SPARK writes is readable by SPARK alone.

    `secret.key` was created 0600 from the start; the database beside it was
    created with the process umask -- 0644 in the image -- so any local user
    on the VM could read the network map, the session hashes and the password
    hash (review finding #31). The umask covers every file made from here on
    (SQLite's -wal and -shm included); the chmod covers the ones already there.
    Best effort on the existing files: a bind mount that refuses chmod is not
    a reason to refuse to start.
    """
    os.umask(0o077)
    for name in ("spark.db", "spark.db-wal", "spark.db-shm", "secret.key"):
        path = data_dir / name
        try:
            if path.exists() and path.stat().st_mode & 0o077:
                path.chmod(0o600)
        except OSError as exc:
            log.warning("Could not make %s private (%s)", path, exc)


def _back_to(request: Request) -> str:
    """The page the request came from, if it was one of ours; else home."""
    referer = urlsplit(request.headers.get("referer", ""))
    if referer.netloc and referer.netloc.lower() == request.headers.get("host", "").lower():
        path = referer.path if referer.path.startswith("/") and not referer.path.startswith("//") else "/"
        return path + (f"?{referer.query}" if referer.query else "")
    return "/"


class _ExpectedShutdownNote(logging.Filter):
    """uvicorn reports the graceful-shutdown timeout at ERROR. With HTTPS it
    fires on most restarts (see GRACEFUL_SHUTDOWN_SECONDS) and means nothing
    went wrong, so it is kept, at INFO, where it does not read as an alarm."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno == logging.ERROR and "timeout graceful shutdown exceeded" in record.getMessage():
            record.levelno = logging.INFO
            record.levelname = "INFO"
        return True


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # uvicorn's access log duplicates what we care about and drowns the rest.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").addFilter(_ExpectedShutdownNote())
    # APScheduler logs two INFO lines for every job it runs -- 84% of the log
    # on a box with a dozen SNMP devices, 10-15 MB a day into Docker's log.
    # Its warnings (a run missed, a job skipped because the last one is still
    # going) and a job's own traceback at ERROR still come through; SPARK logs
    # the scheduler starting and stopping itself.
    logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
    logging.getLogger("apscheduler.scheduler").setLevel(logging.WARNING)


def create_app(config: Config | None = None) -> FastAPI:
    config = config or load_config()
    configure_logging(config.app.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Before the database is opened: SQLite's own error for an unwritable
        # directory is "unable to open database file", which says nothing
        # about ownership. The container no longer runs as root, so a data
        # directory created by an older image, or by Docker on first start, is
        # the one thing most likely to be wrong on upgrade.
        require_writable(config.app.data_dir)
        keep_data_private(config.app.data_dir)
        init_engine(config)
        await init_db(config)

        # Touch the secret key at startup so a read-only or misowned data
        # directory fails loudly here rather than on the first login.
        config.secret_key()

        from .auth import announce_setup_code, new_setup_code, purge_expired, setup_required
        from .subnets import count_enabled, seed_from_config

        async with session_scope() as session:
            await purge_expired(session)
            # No administrator yet: /setup needs the code this prints. Made
            # here so it is in the startup log, where the Quick start says
            # to look; routes_auth makes one on demand if it is ever missing.
            app.state.setup_code = None
            if await setup_required(session):
                app.state.setup_code = new_setup_code()
                announce_setup_code(app.state.setup_code, config.app.host, config.app.port,
                                    config.app.scheme)
            # One-shot: copies spark.yaml's subnets in on the first start after
            # upgrading, then never again. See subnets.seed_from_config.
            await seed_from_config(session, config)
            subnet_count = await count_enabled(session)
            # Earlier versions stored the Discord webhook in plaintext; seal it.
            from .alerts import seal_plaintext_webhook
            from .vault import vault_for

            if await seal_plaintext_webhook(session, vault_for(config)):
                log.info("Moved the Discord webhook into encrypted storage")

        scheduler_module.start()
        scheduled = await scheduler_module.sync_jobs()
        # A few seconds, not a full interval: see schedule_discovery.
        sweeping = await scheduler_module.schedule_discovery(
            config, subnet_count=subnet_count, first_run_delay=15
        )
        # After the first sweep: it scans whatever that sweep found, so going
        # first would mean scanning an empty device table.
        await scheduler_module.schedule_port_scan(first_run_delay=120)
        scheduler_module.schedule_retention()
        snmp_polled = await scheduler_module.sync_snmp_jobs(config)
        scheduler_module.schedule_alerts(config)
        scheduler_module.schedule_identity(config)
        scheduler_module.schedule_storage(config)
        scheduler_module.schedule_credentials(config)

        log.info(
            "SPARK %s ready on %s://%s:%s  (auth: %s, subnets: %d, "
            "polling %d target(s), SNMP %d device(s), discovery %s)",
            __version__,
            config.app.scheme,
            config.app.host,
            config.app.port,
            config.auth.mode,
            subnet_count,
            scheduled,
            snmp_polled,
            "on" if sweeping else "off",
        )
        if config.auth.mode == "proxy" and config.app.host in ("0.0.0.0", "::", ""):
            # The identity header is only as private as this port. Anything
            # that can reach it directly, past the proxy, is one header away
            # from being admin unless its address is refused -- which it is,
            # but binding to the proxy's side of the box is the stronger wall.
            log.warning(
                "auth.mode is 'proxy' and SPARK is listening on every interface "
                "(app.host: %s). If the proxy is on this machine, set app.host to "
                "127.0.0.1; otherwise firewall port %s so only %s can reach it.",
                config.app.host, config.app.port,
                ", ".join(config.auth.proxy.trusted_proxies),
            )
        yield
        await scheduler_module.shutdown()
        # The SNMP engines are shared across polls and hold this loop's UDP
        # socket (collectors/snmp.py); they go after the jobs that use them.
        from .collectors.snmp import close_engines

        close_engines()
        await close_engine()

    app = FastAPI(
        title="SPARK",
        version=__version__,
        lifespan=lifespan,
        # No OpenAPI document and no Swagger page. There is no API to document
        # -- every route serves a form or a page -- and the schema was the one
        # thing served to anyone without a session: a map of every route and
        # every form field on the box that holds a map of the network.
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )
    app.state.config = config
    # Outermost, so the headers land on every response including redirects and
    # errors, a cross-site POST is refused before any dependency runs, and the
    # client address is settled -- from X-Forwarded-For only behind a proxy
    # SPARK was told to trust -- before anything reads it.
    app.add_middleware(Hardening, trusted_proxies=config.auth.proxy.trusted_proxies,
                       allowed_hosts=config.app.allowed_hosts,
                       # HSTS only behind a certificate the operator chose; see tls.py.
                       hsts=config.app.tls.mode == "custom")

    @app.exception_handler(RedirectException)
    async def _handle_redirect(_request: Request, exc: RedirectException):
        return RedirectResponse(exc.location, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _handle_invalid(request: Request, exc: RequestValidationError):
        """A request SPARK's own pages would not send, answered as a page.

        FastAPI's default is a JSON document that repeats the input back --
        harmless (it is served as JSON), but a raw dump is no answer to a
        person. An impossible id in the URL is "not found"; anything else is
        a form field that was missing, too long, or not a number.
        """
        errors = exc.errors()
        if any(e.get("loc", ("",))[0] == "path" for e in errors):
            return templates.TemplateResponse(
                request, "error.html",
                {"config": config, "title": "Not found", "heading": "Not found",
                 "message": "There is nothing at that address.",
                 "problems": [], "back": "/", "back_label": "Go to the dashboard"},
                status_code=404,
            )
        problems = []
        for e in errors:
            field = str(e.get("loc", ("", "?"))[-1]).replace("_", " ")
            kind = e.get("type", "")
            if kind == "missing":
                problems.append(f"{field}: missing")
            elif kind == "string_too_long":
                limit = (e.get("ctx") or {}).get("max_length")
                problems.append(f"{field}: longer than {limit} characters")
            else:
                problems.append(f"{field}: not a value SPARK can use")
        return templates.TemplateResponse(
            request, "error.html",
            {"config": config, "title": "Not saved", "heading": "Nothing was saved",
             "message": "Part of that form could not be used, so nothing changed.",
             "problems": problems, "back": _back_to(request), "back_label": "Go back"},
            status_code=400,
        )

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(auth_router)
    app.include_router(dashboard_router)
    app.include_router(targets_router)
    app.include_router(events_router)
    app.include_router(devices_router)
    app.include_router(device_page_router)
    app.include_router(settings_router)
    app.include_router(preferences_router)
    app.include_router(map_router)
    app.include_router(credentials_router)
    app.include_router(suppressions_router)
    return app


def tls_arguments(config: Config) -> dict:
    """What uvicorn needs to serve HTTPS, per app.tls; empty for plain HTTP.

    The self-signed pair is made here, before uvicorn binds, because uvicorn
    reads the files at start -- so the data directory is checked and made
    private first, the same two steps the lifespan repeats harmlessly.
    """
    mode = config.app.tls.mode
    if mode == "off":
        return {}
    if mode == "custom":
        for path in (config.app.tls.cert, config.app.tls.key):
            if path is None or not Path(path).exists():
                raise SystemExit(f"app.tls names {path}, which does not exist.")
        return {"ssl_certfile": str(config.app.tls.cert), "ssl_keyfile": str(config.app.tls.key)}
    require_writable(config.app.data_dir)
    keep_data_private(config.app.data_dir)
    cert, key, made_now = tls.ensure_self_signed(config.app.tls_dir, config.app.instance_name)
    tls.announce(cert, config.app.host, config.app.port, made_now)
    return {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}


def server_arguments(config: Config) -> dict:
    """Everything SPARK tells uvicorn besides the app, host and port.

    In one place so the live-server tests start uvicorn the way `run` does.
    """
    return {
        "log_config": None,
        # SPARK applies X-Forwarded-* itself, only from auth.proxy.trusted_proxies
        # (web/hardening.py). uvicorn's own handling trusts loopback by
        # default, which with host networking is every container on the VM.
        "proxy_headers": False,
        "timeout_graceful_shutdown": GRACEFUL_SHUTDOWN_SECONDS,
        **tls_arguments(config),
    }


def run() -> None:
    import uvicorn

    config = load_config()
    configure_logging(config.app.log_level)
    uvicorn.run(
        create_app(config),
        host=config.app.host,
        port=config.app.port,
        **server_arguments(config),
    )


def factory() -> FastAPI:
    """For `uvicorn spark.main:factory --factory`, e.g. with --reload in dev.

    Run it with `--no-proxy-headers` (or FORWARDED_ALLOW_IPS unset and no
    local proxy): SPARK handles X-Forwarded-* itself, see hardening.py.
    """
    return create_app()


if __name__ == "__main__":
    run()
