"""Acceptance probe for ticket 02.

Checks the running stack over HTTP: the readiness probe against live
dependencies, and every error path returning the same envelope.

Run inside the compose network:
    docker compose exec -T api python /app/tests/tools/probe_errors.py
"""

import json
import urllib.error
import urllib.request

BASE = "http://localhost:8000"


def get(path: str, headers: dict[str, str] | None = None):
    request = urllib.request.Request(BASE + path, headers=headers or {})
    try:
        response = urllib.request.urlopen(request, timeout=10)
        return response.status, json.loads(response.read() or b"null"), response.headers
    except urllib.error.HTTPError as exc:
        body = exc.read()
        return exc.code, json.loads(body) if body else None, exc.headers


ENVELOPE_FIELDS = {"code", "message_key", "message", "detail", "request_id", "timestamp"}


def main() -> None:
    failures: list[str] = []

    def check(label: str, condition: bool, observed: object) -> None:
        status = "ok  " if condition else "FAIL"
        print(f"[{status}] {label}: {observed}")
        if not condition:
            failures.append(label)

    # Readiness against live dependencies.
    status, body, _ = get("/ready")
    check("ready returns 200", status == 200, status)
    check("ready status ok", body.get("status") == "ok", body.get("status"))
    names = {c["name"] for c in body.get("checks", [])}
    check("ready checks both deps", names == {"postgres", "redis"}, sorted(names))
    check(
        "both deps report ok",
        all(c["status"] == "ok" for c in body["checks"]),
        [(c["name"], c["status"]) for c in body["checks"]],
    )
    check(
        "postgres detail says connected",
        "connected" in (body["checks"][0]["detail"] or ""),
        body["checks"][0]["detail"],
    )

    # Liveness must not depend on anything.
    status, body, _ = get("/health")
    check("health returns 200", status == 200, status)

    # Error paths.
    status, body, headers = get(
        "/api/v1/_debug/errors/catalogued", {"X-Request-ID": "probe-trace-1"}
    )
    check("catalogued error is 404", status == 404, status)
    check("envelope has all fields", ENVELOPE_FIELDS <= set(body["error"]), sorted(body["error"]))
    check("code is catalogued", body["error"]["code"] == "ERR_RESOURCE_001", body["error"]["code"])
    check(
        "message is rendered in Spanish",
        body["error"]["message"].startswith("No se encontró"),
        body["error"]["message"],
    )
    check(
        "request id echoed into envelope",
        body["error"]["request_id"] == "probe-trace-1",
        body["error"]["request_id"],
    )
    check(
        "request id echoed as header",
        headers.get("X-Request-ID") == "probe-trace-1",
        headers.get("X-Request-ID"),
    )

    status, body, _ = get("/api/v1/_debug/errors/validation?limit=99")
    check("validation error is 422", status == 422, status)
    fields = body["error"].get("fields") or []
    check("validation names the field", bool(fields) and "limit" in fields[0]["field"], fields)

    status, body, headers = get(
        "/api/v1/_debug/errors/unexpected", {"X-Request-ID": "probe-trace-500"}
    )
    check("unexpected error is 500", status == 500, status)
    check(
        "5xx detail is not exposed",
        body["error"]["detail"] is None,
        body["error"]["detail"],
    )
    check(
        "5xx keeps the caller request id as a header",
        headers.get("X-Request-ID") == "probe-trace-500",
        headers.get("X-Request-ID"),
    )

    status, body, _ = get("/api/v1/nope")
    check("unknown route is catalogued", status == 404, status)

    print()
    print("FAIL" if failures else "ALL CHECKS PASSED")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
