"""Development-only probes for the error envelope.

Registered only when APP_ENV=development, so the production surface stays free
of endpoints whose entire purpose is to fail.
"""

from fastapi import APIRouter, Query

from app.core.errors import AppError, ErrorCode

router = APIRouter(prefix="/_debug", tags=["debug"], include_in_schema=False)


@router.get("/errors/catalogued")
async def raise_catalogued_error() -> None:
    """Exercise the AppError path: a catalogued code with a log-only detail."""
    raise AppError(ErrorCode.NOT_FOUND, detail="probe: catalogued error")


@router.get("/errors/unexpected")
async def raise_unexpected_error() -> None:
    """Exercise the catch-all path: an uncatalogued exception becomes 500."""
    raise RuntimeError("probe: unexpected error")


@router.get("/errors/validation")
async def trigger_validation(limit: int = Query(ge=1, le=10)) -> dict[str, int]:
    """Exercise the validation path via a real framework validation failure."""
    return {"limit": limit}
