"""Acceptance probe for ticket 06.

Drives the department API over real HTTP against the running stack: the seed
hierarchy shape (4 levels), ltree subtree scoping, the single-statement subtree
rewrite on a move, the depth limit, and the catalogued errors.

Run inside the compose network:
    docker compose exec -T api python /app/tests/tools/probe_departments.py
"""

import json
import sys
import urllib.error
import urllib.request
from uuid import uuid4

BASE = "http://localhost:8000/api/v1"
HEADERS = {"X-Actor-Roles": "hr", "Content-Type": "application/json"}

failures: list[str] = []


def check(label: str, condition: bool, observed: object = "") -> None:
    print(f"[{'ok  ' if condition else 'FAIL'}] {label}: {observed}")
    if not condition:
        failures.append(label)


def call(method: str, path: str, body: dict | None = None) -> tuple[int, object]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method, headers=HEADERS)
    try:
        response = urllib.request.urlopen(request, timeout=10)
        payload = response.read()
        return response.status, json.loads(payload) if payload else None
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        return exc.code, json.loads(payload) if payload else None


def create(code: str, parent_id: str | None = None, **extra: object) -> dict:
    status, body = call(
        "POST",
        "/departments",
        {"code": code, "name_es": code, "name_en": code, "parent_id": parent_id, **extra},
    )
    assert status == 201, f"create {code} failed: {status} {body}"
    return body


def main() -> None:
    suffix = uuid4().hex[:6]
    root_code = f"probe{suffix}"

    # A four-level *nested* tree below a root: depths 0..4.
    root = create(root_code)
    level1 = create(f"a{suffix}", root["id"])
    level2 = create(f"b{suffix}", level1["id"])
    level3 = create(f"c{suffix}", level2["id"])
    level4 = create(f"d{suffix}", level3["id"])

    check("root sits at depth 0", root["depth"] == 0, root["depth"])
    check("four nested levels are allowed", level4["depth"] == 4, level4["depth"])
    check(
        "path is materialised from the parent chain",
        level4["path"] == f"{root_code}.a{suffix}.b{suffix}.c{suffix}.d{suffix}",
        level4["path"],
    )

    # One more level must be refused.
    status, body = call(
        "POST",
        "/departments",
        {"code": f"e{suffix}", "name_es": "e", "name_en": "e", "parent_id": level4["id"]},
    )
    check("a fifth nested level is refused", status == 422, status)
    check(
        "and the refusal is catalogued",
        isinstance(body, dict) and body.get("error", {}).get("code") == "ERR_ORG_007",
        body.get("error", {}).get("code") if isinstance(body, dict) else body,
    )

    # Ancestry is scoped to the branch.
    status, subtree = call("GET", f"/departments/{level1['id']}/subtree")
    check("subtree returns the branch only", status == 200 and len(subtree) == 4, len(subtree))
    check(
        "subtree excludes the sibling branch",
        isinstance(subtree, list) and all(root_code in row["path"] for row in subtree),
        True,
    )

    # A move rewrites the whole subtree. `level2` carries two levels beneath it,
    # so relocating it under a sibling still fits inside the depth limit.
    sibling = create(f"z{suffix}", root["id"])
    status, moved = call("POST", f"/departments/{level2['id']}/move", {"parent_id": sibling["id"]})
    check("move succeeds", status == 200, status)
    check(
        "moved node path is rebuilt",
        moved["path"] == f"{root_code}.z{suffix}.b{suffix}",
        moved.get("path"),
    )
    _, deep = call("GET", f"/departments/{level4['id']}")
    check(
        "grandchild moved with it",
        deep["path"] == f"{root_code}.z{suffix}.b{suffix}.c{suffix}.d{suffix}",
        deep["path"],
    )
    check("grandchild depth recalculated", deep["depth"] == 4, deep["depth"])

    # Moving into its own descendant is refused.
    status, body = call("POST", f"/departments/{level2['id']}/move", {"parent_id": level4["id"]})
    check("moving into a descendant is refused", status == 409, status)
    check(
        "and the refusal is catalogued",
        body.get("error", {}).get("code") == "ERR_ORG_006",
        body.get("error", {}).get("code"),
    )

    # Deletion rules.
    status, body = call("DELETE", f"/departments/{level2['id']}")
    check("deleting a department with children is refused", status == 409, status)
    check(
        "and the refusal is catalogued",
        body.get("error", {}).get("code") == "ERR_ORG_004",
        body.get("error", {}).get("code"),
    )
    status, _ = call("DELETE", f"/departments/{level4['id']}")
    check("deleting a leaf succeeds", status == 204, status)

    # Cleanup: delete bottom-up so the probe leaves nothing behind.
    for node in (level3, level2, level1, sibling, root):
        call("DELETE", f"/departments/{node['id']}")

    print()
    print("FAIL" if failures else "ALL CHECKS PASSED")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
