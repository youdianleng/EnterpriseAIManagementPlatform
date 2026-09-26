"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.v1 import departments as departments_v1
from app.api.v1 import employees as employees_v1
from app.cache import close_redis
from app.config import get_settings
from app.core.exception_handlers import register_exception_handlers
from app.db import dispose_engine
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
    try:
        yield
    finally:
        # Release pooled connections so a restart never leaves the database
        # holding sockets for a process that is gone.
        await dispose_engine()
        await close_redis()
        logger.info("app_stopped")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings)

    app = FastAPI(
        title="Enterprise AI Management Platform API",
        version=__version__,
        docs_url="/docs" if settings.is_development else None,
        redoc_url=None,
        lifespan=lifespan,
    )
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
    app.include_router(departments_v1.router, prefix=API_PREFIX)
    app.include_router(employees_v1.router, prefix=API_PREFIX)
    if settings.is_development:
        app.include_router(debug.router, prefix=API_PREFIX)

    return app


app = create_app()
