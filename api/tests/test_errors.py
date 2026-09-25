"""Tests for the unified error envelope.

These run without dependencies: the debug probes are registered because the
test settings use APP_ENV=development.
"""

import pytest
from fastapi.testclient import TestClient

from app.core.errors import ERRORS, ErrorCode
from app.core.messages import MESSAGES
from app.main import app

client = TestClient(app, raise_server_exceptions=False)

ENVELOPE_FIELDS = {"code", "message_key", "message", "detail", "request_id", "timestamp"}


def test_catalogued_error_uses_the_envelope() -> None:
    response = client.get("/api/v1/_debug/errors/catalogued")

    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == ErrorCode.NOT_FOUND.value
    assert error["message_key"] == "errors.not_found"
    assert error["message"] == MESSAGES["es"]["errors.not_found"]
    assert ENVELOPE_FIELDS <= set(error)


def test_unexpected_error_is_catalogued_not_leaked() -> None:
    response = client.get("/api/v1/_debug/errors/unexpected")

    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == ErrorCode.INTERNAL_ERROR.value
    # The probe message stays in the log; the catalogue marks 5xx as not exposed.
    assert error["detail"] is None


def test_validation_error_lists_the_failing_fields() -> None:
    response = client.get("/api/v1/_debug/errors/validation", params={"limit": 99})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == ErrorCode.VALIDATION_FAILED.value
    assert error["fields"], "validation errors must name the offending field"
    assert "limit" in error["fields"][0]["field"]


def test_envelope_carries_the_request_id() -> None:
    response = client.get(
        "/api/v1/_debug/errors/catalogued", headers={"X-Request-ID": "trace-xyz"}
    )

    assert response.json()["error"]["request_id"] == "trace-xyz"
    assert response.headers["X-Request-ID"] == "trace-xyz"


def test_caller_request_id_survives_an_unhandled_error() -> None:
    """The 500 is produced by Starlette's outer error middleware, not by this
    application, so this is the case where the header was previously dropped."""
    response = client.get("/api/v1/_debug/errors/unexpected", headers={"X-Request-ID": "trace-500"})

    assert response.status_code == 500
    assert response.headers.get("X-Request-ID") == "trace-500"


def test_every_error_response_carries_a_request_id_header() -> None:
    paths = [
        "/api/v1/_debug/errors/catalogued",
        "/api/v1/_debug/errors/validation?limit=99",
        "/api/v1/_debug/errors/unexpected",
        "/api/v1/nope",
    ]
    for path in paths:
        response = client.get(path, headers={"X-Request-ID": "trace-all"})
        assert response.headers.get("X-Request-ID") == "trace-all", f"missing on {path}"


def test_generated_request_id_is_used_when_the_caller_sends_none() -> None:
    response = client.get("/health")

    assert len(response.headers["X-Request-ID"]) == 32  # uuid4().hex


def test_unknown_route_is_catalogued() -> None:
    response = client.get("/api/v1/does-not-exist")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == ErrorCode.NOT_FOUND.value


@pytest.mark.parametrize("code", list(ErrorCode))
def test_every_code_has_a_message_in_every_locale(code: ErrorCode) -> None:
    """A code without wording would render as nothing in the UI."""
    key = ERRORS[code].message_key
    for locale, catalogue in MESSAGES.items():
        assert key in catalogue, f"{key} missing from locale {locale}"


def test_message_catalogues_cover_the_same_keys() -> None:
    es_keys = set(MESSAGES["es"])
    for locale, catalogue in MESSAGES.items():
        assert set(catalogue) == es_keys, f"locale {locale} key set differs from es"
