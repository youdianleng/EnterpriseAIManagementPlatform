"""Tests for request logging.

The middleware's contract is that every request produces one structured record
carrying these fields, so searching logs by request id or path keeps working.
"""

import logging
from typing import Any

import structlog
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from app.config import get_settings
from app.logging import build_processors, configure_logging
from app.main import app

REQUIRED_FIELDS = {"request_id", "method", "path", "status_code", "duration_ms"}


def _configure_test_capture() -> None:
    """Capture with the production processor chain, so what is asserted is what
    production emits — `capture_logs` alone drops the bound context vars."""
    structlog.configure(processors=build_processors(get_settings()))


def _capture():
    """Capture with the production processor chain.

    `capture_logs` disables all configured processors, so the chain has to be
    handed to it explicitly; otherwise bound context vars never reach the
    captured record and the assertion would be vacuous.
    """
    return capture_logs(processors=build_processors(get_settings()))


def _completed(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [record for record in records if record.get("event") == "request_completed"]


def test_successful_request_logs_required_fields() -> None:
    client = TestClient(app)
    _configure_test_capture()
    with _capture() as records:
        client.get("/health", headers={"X-Request-ID": "log-trace-1"})

    completed = _completed(records)
    assert completed, "no request_completed record was emitted"

    record = completed[-1]
    assert REQUIRED_FIELDS <= set(record), f"missing {REQUIRED_FIELDS - set(record)}"
    assert record["request_id"] == "log-trace-1"
    assert record["path"] == "/health"
    assert record["status_code"] == 200
    assert isinstance(record["duration_ms"], (int, float))


def test_failed_request_is_logged_with_its_status() -> None:
    client = TestClient(app, raise_server_exceptions=False)
    _configure_test_capture()
    with _capture() as records:
        client.get("/api/v1/_debug/errors/catalogued")

    assert _completed(records)[-1]["status_code"] == 404


def test_unexpected_error_is_logged_as_an_exception() -> None:
    client = TestClient(app, raise_server_exceptions=False)
    _configure_test_capture()
    with _capture() as records:
        client.get("/api/v1/_debug/errors/unexpected")

    errors = [record for record in records if record.get("event") == "unhandled_error"]
    assert errors, "an unhandled exception must produce an unhandled_error record"
    assert errors[-1]["error_type"] == "RuntimeError"
    assert errors[-1]["log_level"] == "error"


def test_health_does_not_probe_dependencies() -> None:
    """Liveness must stay green when dependencies are down.

    Proven by absence: /health emits no dependency-probe records.
    """
    client = TestClient(app)
    _configure_test_capture()
    with _capture() as records:
        response = client.get("/health")

    assert response.status_code == 200
    assert not [record for record in records if record.get("event") == "dependency_probe"]


def test_configure_logging_sets_the_configured_level() -> None:
    settings = get_settings()
    configure_logging(settings)

    root = logging.getLogger()
    assert root.level == getattr(logging, settings.log_level.upper())
    assert root.handlers, "a handler must be attached or logs go nowhere"
