"""Liveness and readiness probes.

`/health` answers "is this process alive" and must never touch a dependency,
otherwise a database blip would make the container look dead and get restarted.
`/ready` answers "can this instance serve traffic" and therefore does check
dependencies, reporting each one individually.
"""

import asyncio
from typing import Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Response
from pydantic import BaseModel

from app import __version__
from app.config import get_settings

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
    """Open a throwaway connection; a configured DSN is not a healthy database."""
    import psycopg

    dsn = _to_libpq_dsn(get_settings().database_url)
    try:
        async with await asyncio.wait_for(
            psycopg.AsyncConnection.connect(dsn), timeout=PROBE_TIMEOUT_SECONDS
        ) as connection:
            async with connection.cursor() as cursor:
                await cursor.execute("SELECT 1")
                await cursor.fetchone()
    except Exception as exc:
        return DependencyCheck(name="postgres", status="error", detail=_short(exc))

    parsed = urlparse(get_settings().database_url)
    return DependencyCheck(
        name="postgres",
        status="ok",
        detail=f"connected {parsed.hostname}:{parsed.port or 5432}{parsed.path}",
    )


async def _check_redis() -> DependencyCheck:
    import redis.asyncio as redis

    client = redis.from_url(get_settings().redis_url, socket_connect_timeout=PROBE_TIMEOUT_SECONDS)
    try:
        await asyncio.wait_for(client.ping(), timeout=PROBE_TIMEOUT_SECONDS)
    except Exception as exc:
        return DependencyCheck(name="redis", status="error", detail=_short(exc))
    finally:
        await client.aclose()

    parsed = urlparse(get_settings().redis_url)
    return DependencyCheck(
        name="redis",
        status="ok",
        detail=f"connected {parsed.hostname}:{parsed.port or 6379}",
    )


def _to_libpq_dsn(sqlalchemy_url: str) -> str:
    """Strip the SQLAlchemy driver suffix so libpq accepts the DSN."""
    return sqlalchemy_url.replace("postgresql+psycopg://", "postgresql://", 1)


def _short(exc: Exception) -> str:
    """First line only: readiness details must stay one line in JSON."""
    text = str(exc).strip().splitlines()
    return text[0][:200] if text else type(exc).__name__
