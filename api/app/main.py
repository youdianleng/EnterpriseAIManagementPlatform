"""FastAPI application factory."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.config import get_settings
from app.logging import configure_logging, get_logger
from app.middleware import RequestContextMiddleware
from app.routers import app_info, system

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

    # Order matters: request context runs outermost so every inner log line,
    # including CORS rejections, carries the request id.
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )

    app.include_router(system.router)
    app.include_router(app_info.router, prefix=API_PREFIX)

    logger.info("app_started", environment=settings.app_env, version=__version__)
    return app


app = create_app()
