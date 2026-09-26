"""Department tree against a real PostgreSQL, through the HTTP API.

These assert the ltree behaviour the domain tests can only simulate: index-backed
ancestry, the single-statement subtree rewrite on a move, and the partial unique
index on codes. Callers are real signed-in accounts, so the module also covers the
kernel's role checks on these routes. The platform fixture commits and wipes
rather than rolling back, because the endpoints read committed rows through their
own sessions — a per-test transaction would be invisible to them.
"""

from tests.support.platform import Platform


async def create(
    platform: Platform, code: str, parent_id: str | None = None, **extra: object
) -> dict:
    """Create a department over HTTP as an administrator, and return its body.

    `name_es`/`name_en` mirror the code, unlike `platform.department`, so tests can
    assert on names they can predict.
    """
    admin = await platform.account(roles=("admin",))
    response = await admin.post(
        "/api/v1/departments",
        json={"code": code, "name_es": code, "name_en": code, "parent_id": parent_id, **extra},
    )
    assert response.status_code == 201, response.text
    return response.json()


# --- ltree behaviour -------------------------------------------------------


async def test_subtree_query_is_scoped_to_the_branch(platform: Platform) -> None:
    company = await create(platform, "company")
    engineering = await create(platform, "engineering", company["id"])
    await create(platform, "backend", engineering["id"])
    await create(platform, "finance", company["id"])

    reader = await platform.account(roles=("employee",))
    response = await reader.get(f"/api/v1/departments/{engineering['id']}/subtree")

    assert response.status_code == 200
    assert {row["code"] for row in response.json()} == {"engineering", "backend"}


async def test_subtree_can_exclude_the_department_itself(platform: Platform) -> None:
    company = await create(platform, "company")
    await create(platform, "engineering", company["id"])

    reader = await platform.account(roles=("employee",))
    response = await reader.get(
        f"/api/v1/departments/{company['id']}/subtree", params={"include_self": "false"}
    )

    assert [row["code"] for row in response.json()] == ["engineering"]


async def test_move_rewrites_the_whole_subtree_in_one_statement(platform: Platform) -> None:
    company = await create(platform, "company")
    ops = await create(platform, "ops", company["id"])
    engineering = await create(platform, "engineering", company["id"])
    backend = await create(platform, "backend", engineering["id"])
    # Renamed local: "platform" is a department code here, not the fixture.
    platform_team = await create(platform, "platform", backend["id"])

    admin = await platform.account(roles=("admin",))
    response = await admin.post(
        f"/api/v1/departments/{engineering['id']}/move",
        json={"parent_id": ops["id"]},
    )

    assert response.status_code == 200, response.text
    assert response.json()["path"] == "company.ops.engineering"

    # Grandchildren moved too, and depths were recomputed by the same UPDATE.
    deep = await admin.get(f"/api/v1/departments/{platform_team['id']}")
    assert deep.json()["path"] == "company.ops.engineering.backend.platform"
    assert deep.json()["depth"] == 4


async def test_deep_ancestry_query_stays_correct_across_levels(platform: Platform) -> None:
    """The permission story depends on this: a root must see every descendant."""
    company = await create(platform, "company")
    level = company
    for name in ("a", "b", "c"):
        level = await create(platform, name, level["id"])

    reader = await platform.account(roles=("employee",))
    response = await reader.get(f"/api/v1/departments/{company['id']}/subtree")

    assert len(response.json()) == 4


# --- constraints -----------------------------------------------------------


async def test_duplicate_code_is_rejected_with_a_catalogued_error(platform: Platform) -> None:
    await create(platform, "company")

    admin = await platform.account(roles=("admin",))
    response = await admin.post(
        "/api/v1/departments",
        json={"code": "company", "name_es": "x", "name_en": "x"},
    )

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "ERR_ORG_002"
    assert error["message_key"] == "errors.department_code_taken"
    assert error["request_id"]


async def test_move_into_descendant_is_rejected(platform: Platform) -> None:
    company = await create(platform, "company")
    engineering = await create(platform, "engineering", company["id"])

    admin = await platform.account(roles=("admin",))
    response = await admin.post(
        f"/api/v1/departments/{company['id']}/move",
        json={"parent_id": engineering["id"]},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ERR_ORG_006"


async def test_delete_with_children_is_rejected(platform: Platform) -> None:
    company = await create(platform, "company")
    await create(platform, "engineering", company["id"])

    admin = await platform.account(roles=("admin",))
    response = await admin.delete(f"/api/v1/departments/{company['id']}")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ERR_ORG_004"


async def test_delete_removes_an_empty_department(platform: Platform) -> None:
    company = await create(platform, "company")

    admin = await platform.account(roles=("admin",))
    response = await admin.delete(f"/api/v1/departments/{company['id']}")

    assert response.status_code == 204
    assert (await admin.get(f"/api/v1/departments/{company['id']}")).status_code == 404


async def test_writes_require_structure_roles(platform: Platform) -> None:
    """Deny by default: being signed in is not the same as being entitled."""
    employee = await platform.account(roles=("employee",))

    response = await employee.post(
        "/api/v1/departments", json={"code": "x", "name_es": "x", "name_en": "x"}
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ERR_AUTH_002"


async def test_reads_require_a_session(platform: Platform) -> None:
    await create(platform, "company")

    anonymous = await platform.client.get("/api/v1/departments")
    assert anonymous.status_code == 401
    assert anonymous.json()["error"]["code"] == "ERR_SES_001"

    # Reading structure is deliberately broad, but never anonymous.
    reader = await platform.account(roles=("employee",))
    assert (await reader.get("/api/v1/departments")).status_code == 200


# --- tree rendering --------------------------------------------------------


async def test_tree_endpoint_returns_nested_nodes_and_metadata(platform: Platform) -> None:
    company = await create(platform, "company")
    engineering = await create(platform, "engineering", company["id"])
    await create(platform, "backend", engineering["id"])
    await create(platform, "finance", company["id"])

    reader = await platform.account(roles=("employee",))
    response = await reader.get("/api/v1/departments")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 4
    assert body["max_depth"] == 2
    assert len(body["roots"]) == 1
    root = body["roots"][0]
    assert root["department"]["code"] == "company"
    assert [child["department"]["code"] for child in root["children"]] == [
        "engineering",
        "finance",
    ]


async def test_inactive_departments_can_be_filtered_out(platform: Platform) -> None:
    company = await create(platform, "company")
    legacy = await create(platform, "legacy", company["id"])

    admin = await platform.account(roles=("admin",))
    await admin.patch(
        f"/api/v1/departments/{legacy['id']}",
        json={"is_active": False},
    )

    all_rows = await admin.get("/api/v1/departments", params={"include_inactive": "true"})
    active_only = await admin.get("/api/v1/departments", params={"include_inactive": "false"})

    assert all_rows.json()["total"] == 2
    assert active_only.json()["total"] == 1


async def test_patch_only_touches_supplied_fields(platform: Platform) -> None:
    company = await create(platform, "company", clearance_level="medium")

    admin = await platform.account(roles=("admin",))
    response = await admin.patch(
        f"/api/v1/departments/{company['id']}",
        json={"name_en": "Company"},
    )

    body = response.json()
    assert body["name_en"] == "Company"
    assert body["name_es"] == "company"  # untouched
    assert body["clearance_level"] == "medium"  # untouched


async def test_clearance_level_is_validated_by_the_database(platform: Platform) -> None:
    company = await create(platform, "company")

    admin = await platform.account(roles=("admin",))
    response = await admin.patch(
        f"/api/v1/departments/{company['id']}",
        json={"clearance_level": "top-secret"},
    )

    assert response.status_code == 422


async def test_writes_invalidate_the_cached_tree_version(platform: Platform) -> None:
    """Structure is cached, so a write must bump the stamp rather than wait."""
    from app.cache import current_org_tree_version

    await create(platform, "company")
    after_first = await current_org_tree_version()
    await create(platform, "engineering")
    after_second = await current_org_tree_version()

    assert after_second > after_first

# --- the fallback approver --------------------------------------------------


async def test_a_department_manager_can_be_appointed_and_removed(
    platform: Platform,
) -> None:
    """The approval engine's fallback has to be reachable from the product.

    It was readable and not writable before this endpoint, which would have left
    the documented fallback existing only in seed data.
    """
    admin = await platform.account(roles=("admin",))
    department = await platform.department("rrhh")
    position = await platform.position(department, "tecnico")
    head = await platform.employee()
    await platform.assign(head, department, position)

    appointed = await admin.put(
        f"/api/v1/departments/{department}/manager", json={"employee_id": head}
    )
    assert appointed.status_code == 200, appointed.text
    assert appointed.json()["manager_employee_id"] == head

    removed = await admin.put(
        f"/api/v1/departments/{department}/manager", json={"employee_id": None}
    )
    assert removed.status_code == 200
    assert removed.json()["manager_employee_id"] is None


async def test_a_department_manager_must_work_there(platform: Platform) -> None:
    """An approval route pointing at an outsider is worse than no route."""
    admin = await platform.account(roles=("admin",))
    department = await platform.department("rrhh")
    outsider = await platform.employee()

    response = await admin.put(
        f"/api/v1/departments/{department}/manager", json={"employee_id": outsider}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ERR_ORG_008"


async def test_the_manager_change_is_audited(platform: Platform) -> None:
    admin = await platform.account(roles=("admin",))
    department = await platform.department("rrhh")
    head = await platform.employee()
    await platform.assign(head, department, await platform.position(department, "tecnico"))

    await admin.put(f"/api/v1/departments/{department}/manager", json={"employee_id": head})

    rows = await platform.sql(
        "SELECT before, after FROM audit_log WHERE action = 'department.updated'"
    )
    assert rows[-1][0]["manager_employee_id"] is None
    assert rows[-1][1]["manager_employee_id"] == head
