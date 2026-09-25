"""Round-trip the API in production mode to inspect the JSON log format.

Runs a throwaway uvicorn on port 9001, issues one request and prints the emitted
log lines. Used to prove the non-development renderer produces one JSON object
per line with the required fields.
"""

import json
import subprocess
import sys
import time
import urllib.request

SERVER = [
    "uvicorn",
    "app.main:app",
    "--host",
    "127.0.0.1",
    "--port",
    "9001",
    "--no-access-log",
]
REQUIRED = {"event", "level", "timestamp", "request_id", "method", "path", "status_code"}


def main() -> None:
    server = subprocess.Popen(SERVER, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                urllib.request.urlopen("http://127.0.0.1:9001/health", timeout=2).read()
                break
            except Exception:
                time.sleep(0.5)
        else:
            print("server never became reachable")
            sys.exit(1)

        with urllib.request.urlopen(
            "http://127.0.0.1:9001/api/v1/info",
            timeout=5,
        ) as response:
            response.read()

        # Ask for the trace id header too: proves it survives in prod config.
        request = urllib.request.Request(
            "http://127.0.0.1:9001/api/v1/info", headers={"X-Request-ID": "prod-trace-1"}
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            echoed = response.headers.get("X-Request-ID")
    finally:
        server.terminate()
        try:
            output = server.communicate(timeout=10)[0]
        except subprocess.TimeoutExpired:
            server.kill()
            output = server.communicate()[0]

    print("=== raw stdout lines ===")
    json_lines = []
    for line in output.splitlines():
        stripped = line.strip()
        print(stripped)
        if stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if "event" in parsed:
                json_lines.append(parsed)

    print()
    print("=== assertions ===")
    failures = 0

    def check(label: str, condition: bool, observed: object) -> None:
        nonlocal failures
        print(f"[{'ok  ' if condition else 'FAIL'}] {label}: {observed}")
        if not condition:
            failures += 1

    check("at least one JSON log object", bool(json_lines), len(json_lines))
    completed = [entry for entry in json_lines if entry.get("event") == "request_completed"]
    check("request_completed emitted", bool(completed), len(completed))
    if completed:
        entry = completed[-1]
        check("all required fields present", REQUIRED <= set(entry), sorted(entry))
        check("timestamp is iso", "T" in str(entry.get("timestamp", "")), entry.get("timestamp"))
    check("request id survives in prod", echoed == "prod-trace-1", echoed)

    print()
    print("FAIL" if failures else "ALL CHECKS PASSED")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
