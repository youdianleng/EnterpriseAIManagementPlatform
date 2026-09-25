"""Debug helper: which response paths carry the X-Request-ID header."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app, raise_server_exceptions=False)

PATHS = [
    "/health",
    "/ready",
    "/api/v1/info",
    "/api/v1/_debug/errors/catalogued",
    "/api/v1/_debug/errors/validation?limit=99",
    "/api/v1/_debug/errors/unexpected",
    "/api/v1/nope",
]

for path in PATHS:
    response = client.get(path, headers={"X-Request-ID": "dbg-1"})
    print(f"{path:<45} status={response.status_code} xrid={response.headers.get('X-Request-ID')}")
