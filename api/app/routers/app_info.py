"""Application endpoints."""

from fastapi import APIRouter
from pydantic import BaseModel

from app import __version__
from app.config import get_settings

router = APIRouter(tags=["app"])


class AppInfo(BaseModel):
    name: str
    version: str
    environment: str
    api_prefix: str


@router.get("/info", response_model=AppInfo, summary="Application information")
async def info() -> AppInfo:
    """Facts the web client renders.

    Serving this from the API rather than inlining a string in the frontend is
    what proves the web -> api hop works end to end.
    """
    settings = get_settings()
    return AppInfo(
        name="Enterprise AI Management Platform",
        version=__version__,
        environment=settings.app_env,
        api_prefix="/api/v1",
    )
