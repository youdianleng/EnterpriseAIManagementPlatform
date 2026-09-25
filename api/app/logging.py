"""Structured logging.

Console renderer in development for readability, JSON in every other
environment so log lines stay machine-parseable in production.
"""

import logging
import sys

import structlog

from app.config import Settings


def configure_logging(settings: Settings) -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    # Route stdlib logging (uvicorn, sqlalchemy) through the same renderer so a
    # request produces one consistent stream instead of two formats. `force`
    # keeps repeated calls (tests, reloads) effective.
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level, force=True)

    renderer: structlog.types.Processor
    if settings.is_development:
        renderer = structlog.dev.ConsoleRenderer()
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[*build_processors(settings), renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )


def build_processors(settings: Settings) -> list[structlog.types.Processor]:
    """The processor chain before the renderer.

    Exposed so tests can capture exactly what production emits, context vars
    included, instead of re-declaring a simplified chain.
    """
    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]
    if not settings.is_development:
        processors.append(structlog.processors.format_exc_info)
    return processors


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
