"""Exception handlers that produce one consistent error envelope.

Envelope shape:

    {"error": {"code", "message_key", "message", "detail",
               "request_id", "timestamp", "fields"}}

`message` is a convenience rendering in Spanish; `message_key` is the contract
the client translates.
"""

from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import AppError, ErrorCode, definition_of
from app.core.messages import message_for
from app.domain.errors import DomainError
from app.logging import get_logger

logger = get_logger(__name__)

# Maps the HTTP statuses starlette/fastapi raise on their own onto catalogue codes.
_STATUS_TO_CODE: dict[int, ErrorCode] = {
    400: ErrorCode.INVALID_REQUEST,
    401: ErrorCode.UNAUTHENTICATED,
    403: ErrorCode.FORBIDDEN,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.METHOD_NOT_ALLOWED,
    410: ErrorCode.RESOURCE_GONE,
    422: ErrorCode.VALIDATION_FAILED,
}

# The language used to render the convenience `message` field.
_ENVELOPE_LOCALE = "es"


def _request_id() -> str | None:
    import structlog

    value = structlog.contextvars.get_contextvars().get("request_id")
    return str(value) if value else None


def build_envelope(
    code: ErrorCode,
    *,
    detail: str | None = None,
    fields: list[dict[str, Any]] | None = None,
    message_key: str | None = None,
) -> dict[str, Any]:
    """The catalogue's wording for a code, unless the raise site named a different one.

    `message_key` is the override ticket 41 needed: a confirmation refused because the
    *document's* rules moved is reported under the agent's own code — the envelope has to
    say whose draft it is — while the sentence a person should read is the domain's, naming
    the balance or the week that moved. It is validated against the catalogue before it is
    used, so the one way to get this wrong (a key with no wording) is an error here rather
    than an empty string in a browser.
    """
    definition = definition_of(code)
    if message_key is not None and not message_for(message_key, _ENVELOPE_LOCALE):
        raise ValueError(f"{message_key!r} is not a catalogue key")
    key = message_key or definition.message_key
    error: dict[str, Any] = {
        "code": code.value,
        "message_key": key,
        "message": message_for(key, _ENVELOPE_LOCALE),
        "detail": detail if definition.expose_detail else None,
        "request_id": _request_id(),
        "timestamp": datetime.now(UTC).isoformat(),
    }
    if fields:
        error["fields"] = fields
    return {"error": error}


def _json(code: ErrorCode, **kwargs: Any) -> JSONResponse:
    return JSONResponse(
        status_code=definition_of(code).status_code,
        content=build_envelope(code, **kwargs),
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        logger.info("app_error", error_code=exc.code.value, detail=exc.detail)
        return _json(exc.code, detail=exc.detail, message_key=exc.message_key)

    @app.exception_handler(DomainError)
    async def handle_domain_error(_: Request, exc: DomainError) -> JSONResponse:
        """Translate a rule violation into the shared envelope.

        The domain raised a code; deciding that this becomes a 409 is the edge's
        job, which is why `DomainError` carries no status of its own.
        """
        code = ErrorCode(exc.code.value)
        logger.info("domain_error", error_code=code.value, detail=exc.detail)
        return _json(code, detail=exc.detail)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Flatten pydantic's error list into field paths the UI can attach to inputs.
        fields = [
            {
                "field": ".".join(str(part) for part in error.get("loc", ())),
                "message": error.get("msg", ""),
                "type": error.get("type", ""),
            }
            for error in exc.errors()
        ]
        logger.info("validation_failed", field_count=len(fields))
        return _json(ErrorCode.VALIDATION_FAILED, fields=fields)

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _STATUS_TO_CODE.get(exc.status_code, ErrorCode.INVALID_REQUEST)
        return _json(code, detail=str(exc.detail) if exc.detail else None)

    @app.exception_handler(Exception)
    async def handle_unexpected_error(_: Request, exc: Exception) -> JSONResponse:
        # Log with the traceback; the client only gets the catalogued message.
        logger.exception("unhandled_error", error_type=type(exc).__name__)
        return _json(ErrorCode.INTERNAL_ERROR, detail=str(exc))
