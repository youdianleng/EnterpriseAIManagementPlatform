"""Smoke tests for the application skeleton.

These run without Postgres or Redis: nothing here touches a dependency, so the
suite stays fast and can gate every commit.
"""

from fastapi.testclient import TestClient

from app import __version__
from app.main import API_PREFIX, app

client = TestClient(app)


def test_health_reports_liveness() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__


def test_health_does_not_require_dependencies() -> None:
    """Liveness must stay green even when dependencies are unreachable."""
    response = client.get("/health")

    assert response.status_code == 200


def test_ready_reports_each_dependency() -> None:
    response = client.get("/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert {check["name"] for check in body["checks"]} == {"postgres", "redis"}
    assert all(check["status"] == "ok" for check in body["checks"])


def test_info_returns_app_metadata() -> None:
    response = client.get(f"{API_PREFIX}/info")

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Enterprise AI Management Platform"
    assert body["version"] == __version__
    assert body["api_prefix"] == API_PREFIX


def test_request_id_is_echoed_back() -> None:
    response = client.get("/health")

    assert response.headers["X-Request-ID"]


def test_inbound_request_id_is_preserved() -> None:
    """A caller-supplied trace id must survive so traces span services."""
    response = client.get("/health", headers={"X-Request-ID": "trace-from-caller"})

    assert response.headers["X-Request-ID"] == "trace-from-caller"
