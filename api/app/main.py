"""FastAPI application factory."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import async_sessionmaker

from app import __version__
from app.api.v1 import accounts as accounts_v1
from app.api.v1 import attendance as attendance_v1
from app.api.v1 import audit as audit_v1
from app.api.v1 import auth as auth_v1
from app.api.v1 import departments as departments_v1
from app.api.v1 import employees as employees_v1
from app.api.v1 import holidays as holidays_v1
from app.api.v1 import leave as leave_v1
from app.api.v1 import notifications as notifications_v1
from app.api.v1 import overtime as overtime_v1
from app.api.v1 import personnel as personnel_v1
from app.api.v1 import positions as positions_v1
from app.api.v1 import projects as projects_v1
from app.api.v1 import roles as roles_v1
from app.api.v1 import schedules as schedules_v1
from app.api.v1 import timesheets as timesheets_v1
from app.cache import close_redis
from app.config import get_settings
from app.core.exception_handlers import register_exception_handlers
from app.db import dispose_engine, get_session_factory
from app.guards import enforce_password_change
from app.logging import configure_logging, get_logger
from app.middleware import (
    REQUEST_ID_HEADER,
    EnvelopeErrorMiddleware,
    RequestContextMiddleware,
)
from app.routers import app_info, debug, system

API_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logger = get_logger(__name__)
    logger.info("app_started", environment=settings.app_env, version=__version__)
    await _publish_role_catalogue(logger)
    runner = _start_personnel_runner(settings)
    try:
        yield
    finally:
        if runner is not None:
            runner.cancel()
            with suppress(asyncio.CancelledError):
                await runner
        # Release pooled connections so a restart never leaves the database
        # holding sockets for a process that is gone.
        await dispose_engine()
        await close_redis()
        logger.info("app_stopped")


def _start_personnel_runner(settings) -> "asyncio.Task[None] | None":  # noqa: ANN001 - Settings
    """The optional in-process applier, off unless the setting turns it on.

    Two servers running it is safe — each change is taken with `FOR UPDATE SKIP
    LOCKED` — but the command remains the supported way to run it, because a loop
    that lives inside the API dies whenever the API does, and the API is the thing
    that gets redeployed.
    """
    if not settings.personnel_apply_runner_enabled:
        return None
    from app.jobs.apply_personnel_changes import run_forever

    return asyncio.create_task(run_forever(settings.personnel_apply_interval_seconds))


async def _publish_role_catalogue(logger) -> None:  # noqa: ANN001 - structlog logger
    """Rewrite the published role tables from the catalogue in code.

    On the owner connection, because the application's own role can only read
    them (migration 0008). A failure here is logged and not fatal: the tables are
    documentation, and refusing to start because a description could not be
    refreshed would turn a cosmetic problem into an outage. The test that asserts
    they agree is what makes drift visible.
    """
    from app.db import build_engine
    from app.domain.access.catalogue import sync_role_catalogue

    engine = build_engine(get_settings(), get_settings().database_url)
    try:
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as session:
            await sync_role_catalogue(session)
    except Exception as exc:  # noqa: BLE001 - reported, never fatal
        logger.warning("role_catalogue_not_published", error=str(exc))
    finally:
        await engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings)

    app = FastAPI(
        title="Enterprise AI Management Platform API",
        version=__version__,
        docs_url="/docs" if settings.is_development else None,
        redoc_url=None,
        lifespan=lifespan,
        # Applied to every route, so a new endpoint cannot be accidentally exempt
        # from the forced password change rule.
        dependencies=[Depends(enforce_password_change)],
    )
    # The guard opens its own session; the app owns the factory it uses.
    app.state.session_factory = get_session_factory()
    # Our envelope middleware replaces Starlette's default one; installing it
    # here clears the built-in before any other middleware is added.
    app.add_middleware(EnvelopeErrorMiddleware)

    # Handlers next so every response, including framework-generated ones,
    # uses the same envelope.
    register_exception_handlers(app)

    # Starlette runs the last-added middleware outermost. CORS is added first so
    # it sits inside; otherwise it would answer preflight requests itself and
    # those responses would never receive a request id.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[REQUEST_ID_HEADER],
    )
    app.add_middleware(RequestContextMiddleware)

    app.include_router(system.router)
    app.include_router(app_info.router, prefix=API_PREFIX)
    app.include_router(attendance_v1.router, prefix=API_PREFIX)
    app.include_router(departments_v1.router, prefix=API_PREFIX)
    app.include_router(employees_v1.router, prefix=API_PREFIX)
    app.include_router(holidays_v1.router, prefix=API_PREFIX)
    app.include_router(leave_v1.router, prefix=API_PREFIX)
    app.include_router(positions_v1.router, prefix=API_PREFIX)
    app.include_router(accounts_v1.router, prefix=API_PREFIX)
    app.include_router(auth_v1.router, prefix=API_PREFIX)
    app.include_router(audit_v1.router, prefix=API_PREFIX)
    app.include_router(roles_v1.router, prefix=API_PREFIX)
    app.include_router(notifications_v1.router, prefix=API_PREFIX)
    app.include_router(overtime_v1.router, prefix=API_PREFIX)
    app.include_router(personnel_v1.router, prefix=API_PREFIX)
    app.include_router(projects_v1.router, prefix=API_PREFIX)
    app.include_router(schedules_v1.router, prefix=API_PREFIX)
    app.include_router(timesheets_v1.router, prefix=API_PREFIX)
    if settings.is_development:
        app.include_router(debug.router, prefix=API_PREFIX)

    return app


app = create_app()
