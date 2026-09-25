"""FastAPI application factory."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.config import get_settings
from app.core.exception_handlers import register_exception_handlers
from app.logging import configure_logging, get_logger
from app.middleware import (
    REQUEST_ID_HEADER,
    EnvelopeErrorMiddleware,
    RequestContextMiddleware,
)
from app.routers import app_info, debug, system

API_PREFIX = "/api/v1"


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings)
    logger = get_logger(__name__)

    app = FastAPI(
        title="Enterprise AI Management Platform API",
        version=__version__,
        docs_url="/docs" if settings.is_development else None,
        redoc_url=None,
    )
    # Our envelope middleware replaces Starlette's default one; installing it
    # here clears the built-in before any other middleware is added.
    app.add_middleware(EnvelopeErrorMiddleware)

    # Handlers first so every response, including framework-generated ones,
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
    if settings.is_development:
        app.include_router(debug.router, prefix=API_PREFIX)

    logger.info("app_started", environment=settings.app_env, version=__version__)
    return app


app = create_app()
