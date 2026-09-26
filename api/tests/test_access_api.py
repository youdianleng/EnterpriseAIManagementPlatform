"""Authorisation over real HTTP: refusals, the audit trail, and cache freshness.

These are the assertions that would catch the failure this whole ticket exists to
prevent: an endpoint that answers without asking the kernel, or a snapshot that
stays valid after the thing it describes has changed.
"""

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest

from tests.support.platform import Platform, running_platform


@pytest.fixture
async def platform(settings) -> AsyncIterator[Platform]:
    async with running_platform(settings) as running:
        yield running


async def principal_of(platform: Platform, user_id: str):
    """The principal the kernel would build for this account, cache included."""
    from app.domain.access.snapshot import resolve_principal

    async with platform.factory() as session:
        return await resolve_principal(session, UUID(user_id))


# --- refusals --------------------------------------------------------------


async def test_an_unauthenticated_request_is_refused(platform: Platform) -> None:
    """No session cookie at all: 401, not a list of everything."""
    response = await platform.client.get("/api/v1/departments")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "ERR_SES_001"


async def test_an_employee_cannot_manage_the_organisation(platform: Platform) -> None:
    actor = await platform.account(roles=("employee",))

    response = await actor.post(
        "/api/v1/departments",
        json={"code": "nope", "name_es": "x", "name_en": "x"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ERR_AUTH_002"


async def test_an_employee_cannot_list_accounts(platform: Platform) -> None:
    actor = await platform.account(roles=("employee",))

    response = await actor.get("/api/v1/accounts")

    assert response.status_code == 403


async def test_an_employee_can_read_the_organisation_tree(platform: Platform) -> None:
    """Reading structure is deliberately broad: everyone needs to see it."""
    await platform.department("rrhh")
    actor = await platform.account(roles=("employee",))

    response = await actor.get("/api/v1/departments")

    assert response.status_code == 200


async def test_a_refusal_is_audited_with_the_action_that_was_attempted(
    platform: Platform,
) -> None:
    """"Who tried and was told no" is half of what an incident review asks."""
    actor = await platform.account(roles=("employee",))

    await actor.post(
        "/api/v1/departments", json={"code": "nope", "name_es": "x", "name_en": "x"}
    )

    rows = await platform.sql(
        "SELECT actor_user_id, after, ip_address FROM audit_log "
        "WHERE action = 'access.refused'"
    )
    assert len(rows) == 1
    recorded = rows[0]
    assert str(recorded[0]) == actor.user_id
    assert recorded[1]["action"] == "department.manage"
    assert recorded[1]["allowed"] is False
    assert recorded[1]["reasons"] == ["role_lacks_permission"]
    assert recorded[2] is not None


async def test_the_audit_record_names_the_endpoint(platform: Platform) -> None:
    actor = await platform.account(roles=("employee",))

    await actor.get("/api/v1/accounts")

    rows = await platform.sql("SELECT after FROM audit_log WHERE action = 'access.refused'")
    assert rows[0][0]["path"] == "/api/v1/accounts"
    assert rows[0][0]["method"] == "GET"


async def test_a_refusal_does_not_reveal_whether_the_resource_exists(
    platform: Platform,
) -> None:
    """403 rather than 404: the caller is not entitled to know either way."""
    actor = await platform.account(roles=("employee",))

    response = await actor.get(f"/api/v1/accounts/{uuid4()}")

    assert response.status_code == 403


# --- the snapshot ----------------------------------------------------------


async def test_the_principal_carries_the_callers_departments(platform: Platform) -> None:
    """Driven through the API: a colleague is visible, an outsider is not."""
    mine = await platform.department("mine")
    theirs = await platform.department("theirs")
    position_mine = await platform.position(mine, "tech")
    position_theirs = await platform.position(theirs, "analyst")

    colleague = await platform.account(roles=("employee",))
    await platform.assign(colleague.employee_id, mine, position_mine)
    outsider = await platform.employee()
    await platform.assign(outsider, theirs, position_theirs)

    as_colleague = await platform.account(roles=("employee",))
    await platform.assign(as_colleague.employee_id, mine, position_mine)

    visible = await as_colleague.get(f"/api/v1/employees/{colleague.employee_id}")
    hidden = await as_colleague.get(f"/api/v1/employees/{outsider}")

    assert visible.status_code == 200
    assert visible.json()["visibility"] == "directory"
    assert hidden.status_code == 200
    # Reachable, but minimally: no email, no location.
    assert hidden.json()["visibility"] == "minimal"


async def test_moving_an_employee_between_departments_takes_effect_at_once(
    platform: Platform,
) -> None:
    """Not after the snapshot's TTL.

    The employee's departments change, and the very next request must reflect it.
    The organisation module already bumps a structure version on department
    edits; an assignment change is the other input that has to count.
    """
    first = await platform.department("first")
    second = await platform.department("second")
    subject = await platform.employee()
    await platform.assign(subject, first, await platform.position(first, "a"))

    viewer = await platform.account(roles=("employee",))
    await platform.assign(viewer.employee_id, first, await platform.position(first, "b"))
    before = await viewer.get(f"/api/v1/employees/{subject}")
    assert before.json()["visibility"] == "directory"

    # Move the viewer away, ending the assignment that gave access.
    await platform.sql(
        "UPDATE employee_assignments SET end_date = '2025-01-01' WHERE employee_id = :id",
        {"id": viewer.employee_id},
    )
    second_position = await platform.position(second, "c")
    await platform.assign(viewer.employee_id, second, second_position)

    after = await viewer.get(f"/api/v1/employees/{subject}")

    assert after.json()["visibility"] == "minimal", (
        "the snapshot still described the old departments"
    )


async def test_deactivating_an_account_ends_access_without_a_ttl_wait(
    platform: Platform,
) -> None:
    actor = await platform.account(roles=("employee",))
    assert (await actor.get("/api/v1/departments")).status_code == 200

    await platform.sql(
        "UPDATE users SET is_active = false, session_epoch = session_epoch + 1 WHERE id = :id",
        {"id": actor.user_id},
    )

    assert (await actor.get("/api/v1/departments")).status_code == 401


async def test_a_granted_role_takes_effect_at_once(platform: Platform) -> None:
    """Roles are part of the cache key, not only of the snapshot.

    Driven by a direct write because no endpoint grants roles yet (ticket 08b),
    but the mechanism under test is the same one that endpoint will use: the
    snapshot of the *next* request must be built from the new roles, without
    waiting for the TTL.
    """
    actor = await platform.account(roles=("employee",))
    assert (await actor.get("/api/v1/accounts")).status_code == 403

    await platform.sql(
        "UPDATE users SET roles = CAST(:roles AS jsonb) WHERE id = :id",
        {"roles": '["admin"]', "id": actor.user_id},
    )

    assert (await actor.get("/api/v1/accounts")).status_code == 200


async def test_a_clearance_change_takes_effect_at_once(platform: Platform) -> None:
    """Clearance is derived from the department, so the department is what changes.

    Asserted on the resolved principal rather than over HTTP: nothing reads
    clearance over HTTP yet — the document and retrieval rules that do arrive in
    tickets 12 and 35 — and a test that waited for those would not be testing the
    cache at all.
    """
    parent = await platform.department("direccion")
    child = await platform.department("id", parent_id=parent)
    actor = await platform.account(roles=("employee",))
    await platform.assign(actor.employee_id, parent, await platform.position(parent, "lead"))

    assert (await principal_of(platform, actor.user_id)).clearance_level == "low"

    admin = await platform.account(roles=("admin",))

    # The department the person actually sits in goes to medium first: it proves
    # the middle rank maps to itself, which a rank-0 bug would also have hidden.
    response = await admin.patch(
        f"/api/v1/departments/{parent}", json={"clearance_level": "medium"}
    )
    assert response.status_code == 200, response.text
    assert (await principal_of(platform, actor.user_id)).clearance_level == "medium"

    # Then the *child*, through the real endpoint: the person's clearance is the
    # highest among the departments their assignment reaches, descendants
    # included. It goes through the API rather than through SQL because the write
    # is the thing that has to invalidate — a direct UPDATE bypasses the structure
    # version and is what `invalidate_user` is for.
    response = await admin.patch(
        f"/api/v1/departments/{child}", json={"clearance_level": "high"}
    )
    assert response.status_code == 200, response.text

    assert (await principal_of(platform, actor.user_id)).clearance_level == "high", (
        "the snapshot still described the old clearance"
    )


async def test_a_second_request_reuses_the_cached_snapshot(platform: Platform) -> None:
    """The cache is real, not decorative: one entry appears for the caller."""
    actor = await platform.account(roles=("employee",))

    await actor.get("/api/v1/departments")
    await actor.get("/api/v1/departments")

    keys = await platform.sql("SELECT count(*) FROM users WHERE id = :id", {"id": actor.user_id})
    assert keys[0][0] == 1

    import redis.asyncio as redis

    from app.config import get_settings

    client = redis.from_url(get_settings().redis_url, decode_responses=True)
    try:
        cached = [key async for key in client.scan_iter(match=f"perm:user:{actor.user_id}:*")]
    finally:
        await client.aclose()
    assert len(cached) == 1, f"expected one snapshot entry, found {cached}"


async def test_the_security_matrix_over_real_requests(platform: Platform) -> None:
    """Each role against each endpoint, end to end.

    The kernel's own matrix is exhaustive; this one proves the endpoints are
    actually wired to it, which is the part a unit test cannot see.
    """
    await platform.department("rrhh")

    cases = [
        # (roles, method, path, expected status)
        (("employee",), "GET", "/api/v1/departments", 200),
        (("employee",), "POST", "/api/v1/departments", 403),
        (("employee",), "GET", "/api/v1/positions", 200),
        (("employee",), "POST", "/api/v1/positions", 403),
        (("employee",), "GET", "/api/v1/employees/directory", 200),
        (("employee",), "POST", "/api/v1/employees", 403),
        (("employee",), "GET", "/api/v1/accounts", 403),
    ]

    for roles, method, path, expected in cases:
        actor = await platform.account(roles=roles)
        payload = (
            {"code": f"x{uuid4().hex[:6]}", "name_es": "x", "name_en": "x"}
            if method == "POST" and path.endswith("departments")
            else (
                {"code": "x", "title_es": "x", "title_en": "x", "department_id": str(uuid4())}
                if method == "POST" and path.endswith("positions")
                else (
                    {
                        "first_name": "A",
                        "last_name": "B",
                        "email": f"{uuid4().hex[:6]}@empresa.es",
                        "hire_date": "2024-01-15",
                    }
                    if method == "POST"
                    else None
                )
            )
        )
        response = await actor.call(method, path, json=payload) if payload else await actor.call(
            method, path
        )
        assert response.status_code == expected, (
            f"{roles} {method} {path} -> {response.status_code}, expected {expected}"
        )
        await actor.close()
