"""Liveness and readiness probes.

`/health` answers "is this process alive" and must never touch a dependency,
otherwise a database blip would make the container look dead and get restarted.
`/ready` answers "can this instance serve traffic" and therefore does check
dependencies, reporting each one individually.
"""

from typing import Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Response
from pydantic import BaseModel

from app import __version__
from app.config import get_settings

router = APIRouter(tags=["system"])

CheckStatus = Literal["ok", "error"]


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
    checks = [_check_postgres(), _check_redis()]
    healthy = all(check.status == "ok" for check in checks)
    if not healthy:
        response.status_code = 503
    return ReadyResponse(status="ok" if healthy else "degraded", checks=checks)


def _check_postgres() -> DependencyCheck:
    """Validate the configured target without opening a connection.

    Ticket 04 adds the real engine and turns this into an actual ping; a live
    probe here would make `/ready` fail for reasons unrelated to this ticket.
    """
    dsn = get_settings().database_url
    parsed = urlparse(dsn)
    if not parsed.scheme.startswith("postgresql"):
        return DependencyCheck(name="postgres", status="error", detail="not a postgresql DSN")
    return DependencyCheck(
        name="postgres",
        status="ok",
        detail=f"configured {parsed.hostname}:{parsed.port or 5432}{parsed.path}",
    )


def _check_redis() -> DependencyCheck:
    parsed = urlparse(get_settings().redis_url)
    if not parsed.scheme.startswith("redis"):
        return DependencyCheck(name="redis", status="error", detail="not a redis DSN")
    return DependencyCheck(
        name="redis",
        status="ok",
        detail=f"configured {parsed.hostname}:{parsed.port or 6379}",
    )
