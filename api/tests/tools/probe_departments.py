"""Acceptance probe for ticket 06.

Drives the department API over real HTTP against the running stack: the seed
hierarchy shape (4 levels), ltree subtree scoping, the single-statement subtree
rewrite on a move, the depth limit, and the catalogued errors.

Structure changes are a signed-in administrator's work now that the actor headers
are gone, so the probe bootstraps one through `support` and never fakes a caller.

Run inside the compose network:
    docker compose exec -T api python /app/tests/tools/probe_departments.py
"""

from uuid import uuid4

from support import Browser, administrator, check, finish, wipe


def create(browser: Browser, code: str, parent_id: str | None = None, **extra: object) -> dict:
    status, body = browser.call(
        "POST",
        "/departments",
        {"code": code, "name_es": code, "name_en": code, "parent_id": parent_id, **extra},
    )
    assert status == 201, f"create {code} failed: {status} {body}"
    return body


def main() -> None:
    admin = administrator()
    suffix = uuid4().hex[:6]
    root_code = f"probe{suffix}"

    # A four-level *nested* tree below a root: depths 0..4.
    root = create(admin, root_code)
    level1 = create(admin, f"a{suffix}", root["id"])
    level2 = create(admin, f"b{suffix}", level1["id"])
    level3 = create(admin, f"c{suffix}", level2["id"])
    level4 = create(admin, f"d{suffix}", level3["id"])

    check("root sits at depth 0", root["depth"] == 0, root["depth"])
    check("four nested levels are allowed", level4["depth"] == 4, level4["depth"])
    check(
        "path is materialised from the parent chain",
        level4["path"] == f"{root_code}.a{suffix}.b{suffix}.c{suffix}.d{suffix}",
        level4["path"],
    )

    # One more level must be refused.
    status, body = admin.call(
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
    status, subtree = admin.call("GET", f"/departments/{level1['id']}/subtree")
    check("subtree returns the branch only", status == 200 and len(subtree) == 4, len(subtree))
    check(
        "subtree excludes the sibling branch",
        isinstance(subtree, list) and all(root_code in row["path"] for row in subtree),
        True,
    )

    # A move rewrites the whole subtree. `level2` carries two levels beneath it,
    # so relocating it under a sibling still fits inside the depth limit.
    sibling = create(admin, f"z{suffix}", root["id"])
    status, moved = admin.call(
        "POST", f"/departments/{level2['id']}/move", {"parent_id": sibling["id"]}
    )
    check("move succeeds", status == 200, status)
    check(
        "moved node path is rebuilt",
        moved["path"] == f"{root_code}.z{suffix}.b{suffix}",
        moved.get("path"),
    )
    _, deep = admin.call("GET", f"/departments/{level4['id']}")
    check(
        "grandchild moved with it",
        deep["path"] == f"{root_code}.z{suffix}.b{suffix}.c{suffix}.d{suffix}",
        deep["path"],
    )
    check("grandchild depth recalculated", deep["depth"] == 4, deep["depth"])

    # Moving into its own descendant is refused.
    status, body = admin.call(
        "POST", f"/departments/{level2['id']}/move", {"parent_id": level4["id"]}
    )
    check("moving into a descendant is refused", status == 409, status)
    check(
        "and the refusal is catalogued",
        body.get("error", {}).get("code") == "ERR_ORG_006",
        body.get("error", {}).get("code"),
    )

    # Deletion rules.
    status, body = admin.call("DELETE", f"/departments/{level2['id']}")
    check("deleting a department with children is refused", status == 409, status)
    check(
        "and the refusal is catalogued",
        body.get("error", {}).get("code") == "ERR_ORG_004",
        body.get("error", {}).get("code"),
    )
    status, _ = admin.call("DELETE", f"/departments/{level4['id']}")
    check("deleting a leaf succeeds", status == 204, status)

    # Cleanup: delete bottom-up so the probe leaves nothing behind, then drop the
    # rows its own administrator occupies — a signed-in caller is a real employee,
    # a real user and an audit trail, and the tree delete above does not reach
    # them.
    for node in (level3, level2, level1, sibling, root):
        admin.call("DELETE", f"/departments/{node['id']}")
    wipe()

    finish()


if __name__ == "__main__":
    main()
