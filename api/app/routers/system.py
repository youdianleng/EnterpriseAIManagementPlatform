"""Liveness and readiness probes.

`/health` answers "is this process alive" and must never touch a dependency,
otherwise a database blip would make the container look dead and get restarted.
`/ready` answers "can this instance serve traffic" and therefore does check
dependencies, reporting each one individually.
"""

import asyncio
from typing import Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel
from sqlalchemy import text

from app import __version__
from app.cache import get_redis
from app.config import get_settings
from app.db import get_engine

router = APIRouter(tags=["system"])

CheckStatus = Literal["ok", "error"]

# A probe must answer quickly; a slow dependency is a failed dependency.
PROBE_TIMEOUT_SECONDS = 2.0


class DependencyCheck(BaseModel):
    name: str
    status: CheckStatus
    detail: str | None = None


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str
    environment: str


class ReadyResponse(BaseModel):
    status: Literal["ok", "degraded"]
    checks: list[DependencyCheck]


@router.get("/health", response_model=HealthResponse, summary="Liveness probe")
async def health() -> HealthResponse:
    settings = get_settings()
    return HealthResponse(status="ok", version=__version__, environment=settings.app_env)


@router.get("/ready", response_model=ReadyResponse, summary="Readiness probe")
async def ready(response: Response) -> ReadyResponse:
    checks = list(await asyncio.gather(_check_postgres(), _check_redis()))
    healthy = all(check.status == "ok" for check in checks)
    if not healthy:
        response.status_code = 503
    return ReadyResponse(status="ok" if healthy else "degraded", checks=checks)


async def _check_postgres() -> DependencyCheck:
    """Run a statement; a configured DSN is not a healthy database."""
    try:
        async with get_engine().connect() as connection:
            await asyncio.wait_for(
                connection.execute(text("SELECT 1")), timeout=PROBE_TIMEOUT_SECONDS
            )
    except Exception as exc:
        return DependencyCheck(name="postgres", status="error", detail=_short(exc))

    return DependencyCheck(
        name="postgres",
        status="ok",
        detail=f"connected {_target(get_settings().database_url)}",
    )


async def _check_redis() -> DependencyCheck:
    try:
        await asyncio.wait_for(get_redis().ping(), timeout=PROBE_TIMEOUT_SECONDS)
    except Exception as exc:
        return DependencyCheck(name="redis", status="error", detail=_short(exc))

    return DependencyCheck(
        name="redis",
        status="ok",
        detail=f"connected {_target(get_settings().redis_url)}",
    )


def _target(url: str) -> str:
    """host:port/database, with credentials left out of the response."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    default_port = 6379 if parsed.scheme.startswith("redis") else 5432
    return f"{parsed.hostname}:{parsed.port or default_port}{parsed.path}"


def _short(exc: Exception) -> str:
    """First line only: readiness details must stay one line in JSON."""
    text_lines = str(exc).strip().splitlines()
    return text_lines[0][:200] if text_lines else type(exc).__name__
