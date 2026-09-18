"""Application entry point."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from . import scheduler as scheduler_module
from .config import Config, load_config
from .db import close_engine, init_db, init_engine, session_scope
from .web.deps import RedirectException
from .web.routes_auth import router as auth_router
from .web.routes_dashboard import router as dashboard_router
from .web.routes_devices import router as devices_router
from .web.routes_events import router as events_router
from .web.routes_settings import router as settings_router
from .web.routes_targets import router as targets_router

STATIC_DIR = Path(__file__).resolve().parent / "static"

log = logging.getLogger("spark")


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

        scheduler_module.start()
        scheduled = await scheduler_module.sync_jobs()
        # A few seconds, not a full interval: see schedule_discovery.
        sweeping = await scheduler_module.schedule_discovery(
            config, subnet_count=subnet_count, first_run_delay=15
        )
        scheduler_module.schedule_retention()

        log.info(
            "SPARK %s ready on http://%s:%s  (auth: %s, subnets: %d, "
            "polling %d target(s), discovery %s)",
            __version__,
            config.app.host,
            config.app.port,
            config.auth.mode,
            subnet_count,
            scheduled,
            "on" if sweeping else "off",
        )
        yield
        await scheduler_module.shutdown()
        await close_engine()

    app = FastAPI(
        title="SPARK",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
    )
    app.state.config = config

    @app.exception_handler(RedirectException)
    async def _handle_redirect(_request: Request, exc: RedirectException):
        return RedirectResponse(exc.location, status_code=exc.status_code)

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(auth_router)
    app.include_router(dashboard_router)
    app.include_router(targets_router)
    app.include_router(events_router)
    app.include_router(devices_router)
    app.include_router(settings_router)
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
