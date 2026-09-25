"""Cross-cutting HTTP concerns: request id propagation and access logging.

Implemented as pure ASGI middleware rather than `BaseHTTPMiddleware` for one
concrete reason: the 500 path. An unhandled endpoint exception propagates past
every `BaseHTTPMiddleware` response hook and is turned into a response by
Starlette's outer `ServerErrorMiddleware`, so any header set by a response hook
is silently dropped. Wrapping `send` keeps the header on *every* response,
including the ones this application never returns itself.
"""

import json
import time
import uuid
from typing import Any

import structlog

from app.logging import get_logger

logger = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
REQUEST_ID_HEADER_BYTES = REQUEST_ID_HEADER.lower().encode()


class RequestContextMiddleware:
    """Bind a request id to every log line and echo it on every response.

    An inbound `X-Request-ID` is honoured so a trace survives across services;
    otherwise one is generated.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = self._request_id(scope)
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        started = time.perf_counter()
        status_code = 0
        response_started = False

        async def send_with_request_id(message: Any) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                status_code = message["status"]
                response_started = True
                # Drop any inbound duplicate, then set ours.
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != REQUEST_ID_HEADER_BYTES
                ]
                headers.append((REQUEST_ID_HEADER_BYTES, request_id.encode()))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            if response_started:
                # Too late to send a body; the transport will break the connection.
                logger.error(
                    "response_failed_midstream",
                    method=scope.get("method"),
                    path=scope.get("path"),
                    status_code=status_code,
                    duration_ms=duration_ms,
                )
            else:
                # No response was produced: the outer error middleware will make
                # the 500, so this log line is the only record carrying the
                # request id. Re-raise to keep the response flowing through it.
                logger.exception(
                    "request_failed",
                    method=scope.get("method"),
                    path=scope.get("path"),
                    duration_ms=duration_ms,
                )
            raise
        else:
            if status_code:
                logger.info(
                    "request_completed",
                    method=scope.get("method"),
                    path=scope.get("path"),
                    status_code=status_code,
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                )
        finally:
            structlog.contextvars.clear_contextvars()

    @staticmethod
    def _request_id(scope: Any) -> str:
        for name, value in scope.get("headers", []):
            if name.lower() == REQUEST_ID_HEADER_BYTES:
                try:
                    decoded = value.decode("latin-1").strip()
                except Exception:
                    decoded = ""
                if decoded:
                    return decoded
        return uuid.uuid4().hex


class EnvelopeErrorMiddleware:
    """Turn an unhandled exception into the standard error envelope.

    This replaces Starlette's `ServerErrorMiddleware`. That middleware sits
    *outside* all user middleware, so a 500 it generates never passes through
    the request-context wrapper and loses the `X-Request-ID` header — exactly
    the header a user needs to quote in a bug report. Handling the exception
    here keeps it inside the wrapper.

    The traceback is logged; the response body carries only the catalogue code.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def send_tracking_start(message: Any) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, send_tracking_start)
        except Exception as exc:
            logger.exception("unhandled_error", error_type=type(exc).__name__)
            if response_started:
                raise
            from app.core.errors import ErrorCode
            from app.core.exception_handlers import build_envelope

            body = json.dumps(build_envelope(ErrorCode.INTERNAL_ERROR)).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 500,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
