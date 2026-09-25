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
from .config import Config, load_config
from .db import close_engine, init_db, init_engine, session_scope
from .web.deps import RedirectException, templates
from .web.hardening import Hardening
from .web.routes_auth import router as auth_router
from .web.routes_dashboard import router as dashboard_router
from .web.routes_device_page import router as device_page_router
from .web.routes_devices import router as devices_router
from .web.routes_preferences import router as preferences_router
from .web.routes_events import router as events_router
from .web.routes_settings import router as settings_router
from .web.routes_targets import router as targets_router

STATIC_DIR = Path(__file__).resolve().parent / "static"

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


def _back_to(request: Request) -> str:
    """The page the request came from, if it was one of ours; else home."""
    referer = urlsplit(request.headers.get("referer", ""))
    if referer.netloc and referer.netloc.lower() == request.headers.get("host", "").lower():
        path = referer.path if referer.path.startswith("/") and not referer.path.startswith("//") else "/"
        return path + (f"?{referer.query}" if referer.query else "")
    return "/"


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # uvicorn's access log duplicates what we care about and drowns the rest.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


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
        init_engine(config)
        await init_db(config)

        # Touch the secret key at startup so a read-only or misowned data
        # directory fails loudly here rather than on the first login.
        config.secret_key()

        from .auth import purge_expired
        from .subnets import count_enabled, seed_from_config

        async with session_scope() as session:
            await purge_expired(session)
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

        log.info(
            "SPARK %s ready on http://%s:%s  (auth: %s, subnets: %d, "
            "polling %d target(s), SNMP %d device(s), discovery %s)",
            __version__,
            config.app.host,
            config.app.port,
            config.auth.mode,
            subnet_count,
            scheduled,
            snmp_polled,
            "on" if sweeping else "off",
        )
        yield
        await scheduler_module.shutdown()
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
    # errors, and a cross-site POST is refused before any dependency runs.
    app.add_middleware(Hardening)

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
    return app


def run() -> None:
    import uvicorn

    config = load_config()
    configure_logging(config.app.log_level)
    uvicorn.run(
        create_app(config),
        host=config.app.host,
        port=config.app.port,
        log_config=None,
    )


def factory() -> FastAPI:
    """For `uvicorn spark.main:factory --factory`, e.g. with --reload in dev."""
    return create_app()


if __name__ == "__main__":
    run()
