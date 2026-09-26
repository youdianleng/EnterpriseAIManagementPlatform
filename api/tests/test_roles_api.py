"""Roles: the published catalogue, and granting them.

Two halves. The catalogue tables are a projection of `permissions.py`, so the
first half is the test that keeps them honest — if they ever disagree, the system
would be documenting a rule it does not apply. The second half is the grant path:
who may change a role, what is refused, and that a revocation takes effect on the
next request rather than whenever a cache entry expires.
"""

from uuid import UUID

import pytest

from tests.support.platform import Actor, Platform

ROLES_PATH = "/api/v1/roles"


async def publish(platform: Platform) -> None:
    """Rewrite the catalogue tables, exactly as application start does."""
    from app.domain.access.catalogue import sync_role_catalogue

    async with platform.factory() as session:
        await sync_role_catalogue(session)


async def roles_of(platform: Platform, account_id: str) -> set[str]:
    rows = await platform.sql("SELECT roles FROM users WHERE id = :id", {"id": account_id})
    return set(rows[0][0])


async def principal_of(platform: Platform, user_id: str):
    from app.domain.access.snapshot import resolve_principal

    async with platform.factory() as session:
        return await resolve_principal(session, UUID(user_id))


# --- the published catalogue ------------------------------------------------


async def test_the_published_catalogue_matches_the_code(platform: Platform) -> None:
    """The tables are a projection, and this is what makes that true.

    A row edit cannot change a permission — the kernel decides from `RULES` — so a
    divergence would be documentation that lies. Comparing both directions, because
    either one alone passes when the other has extra rows.
    """
    from app.domain.access.permissions import RULES

    await publish(platform)

    expected = {
        (role, str(action))
        for action, rule in RULES.items()
        if not rule.public
        for role in rule.roles
    }
    published = {
        (row[0], row[1])
        for row in await platform.sql("SELECT role, action FROM role_permissions")
    }

    assert published - expected == set(), "the table grants what the code does not"
    assert expected - published == set(), "the code grants what the table does not"


async def test_every_system_role_is_published_with_a_description(
    platform: Platform,
) -> None:
    from app.domain.access.principal import SYSTEM_ROLES

    await publish(platform)

    rows = await platform.sql("SELECT name, description FROM roles")
    described = {row[0]: row[1] for row in rows}

    assert set(described) == set(SYSTEM_ROLES)
    assert all(description.strip() for description in described.values())


async def test_the_manager_role_is_marked_as_derived(platform: Platform) -> None:
    """It comes from a managerial position; a reader should not have to guess."""
    await publish(platform)

    derived = await platform.sql("SELECT name FROM roles WHERE is_derived")

    assert [row[0] for row in derived] == ["manager"]


async def test_the_catalogue_is_readable_by_any_signed_in_user(platform: Platform) -> None:
    """What the system allows is not a secret."""
    await publish(platform)
    employee = await platform.account(roles=("employee",))

    response = await employee.get(ROLES_PATH)

    assert response.status_code == 200
    body = response.json()
    names = {item["name"] for item in body["items"]}
    assert names >= {"admin", "hr", "finance", "it", "compliance", "manager", "employee"}
    hr = next(item for item in body["items"] if item["name"] == "hr")
    assert "employee.read_withheld" in hr["actions"]


async def test_the_catalogue_is_not_available_to_an_anonymous_caller(
    platform: Platform,
) -> None:
    await publish(platform)

    response = await platform.client.get(ROLES_PATH)

    assert response.status_code == 401


async def test_an_unpublished_catalogue_says_so(platform: Platform) -> None:
    """An empty list would read as "no roles exist", which is not the same thing."""
    employee = await platform.account(roles=("employee",))

    response = await employee.get(ROLES_PATH)

    assert response.status_code == 404


# --- granting ---------------------------------------------------------------


async def test_an_administrator_can_grant_a_role(platform: Platform) -> None:
    admin = await platform.admin()
    subject = await platform.account(roles=("employee",))

    response = await admin.put(
        f"/api/v1/accounts/{subject.user_id}/roles",
        json={"roles": ["employee", "hr"]},
    )

    assert response.status_code == 200, response.text
    assert await roles_of(platform, subject.user_id) == {"employee", "hr"}


async def test_the_grant_is_audited_with_both_sides(platform: Platform) -> None:
    admin = await platform.admin()
    subject = await platform.account(roles=("employee",))

    await admin.put(
        f"/api/v1/accounts/{subject.user_id}/roles",
        json={"roles": ["finance"]},
    )

    rows = await platform.sql(
        "SELECT actor_user_id, before, after FROM audit_log WHERE action = 'user.roles_changed'"
    )
    assert len(rows) == 1
    assert str(rows[0][0]) == admin.user_id
    assert rows[0][1] == {"roles": ["employee"]}
    assert rows[0][2] == {"roles": ["finance"]}


@pytest.mark.parametrize("roles", [("hr",), ("finance",), ("it",), ("compliance",), ("employee",)])
async def test_only_an_administrator_can_grant(
    platform: Platform, roles: tuple[str, ...]
) -> None:
    actor = await platform.account(roles=roles)
    subject = await platform.account(roles=("employee",))

    response = await actor.put(
        f"/api/v1/accounts/{subject.user_id}/roles",
        json={"roles": ["admin"]},
    )

    assert response.status_code == 403
    assert await roles_of(platform, subject.user_id) == {"employee"}


async def test_an_unknown_role_is_refused(platform: Platform) -> None:
    admin = await platform.admin()
    subject = await platform.account(roles=("employee",))

    response = await admin.put(
        f"/api/v1/accounts/{subject.user_id}/roles",
        json={"roles": ["wizard"]},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ERR_ACC_010"
    assert await roles_of(platform, subject.user_id) == {"employee"}


async def test_an_empty_role_set_is_refused(platform: Platform) -> None:
    """No roles is not "no permissions", it is an account that can do nothing at all."""
    admin = await platform.admin()
    subject = await platform.account(roles=("employee",))

    response = await admin.put(f"/api/v1/accounts/{subject.user_id}/roles", json={"roles": []})

    assert response.status_code == 422
    assert await roles_of(platform, subject.user_id) == {"employee"}


async def test_the_last_administrator_keeps_the_role(platform: Platform) -> None:
    """Removing it would leave a system nobody can administer, and the person
    doing it is the one least able to notice."""
    admin = await platform.admin()
    other = await platform.account(roles=("employee",))

    response = await admin.put(
        f"/api/v1/accounts/{admin.user_id}/roles", json={"roles": ["employee"]}
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ERR_ACC_011"
    assert "admin" in await roles_of(platform, admin.user_id)

    # A second administrator makes the same request acceptable, which is what
    # "last" means.
    await admin.put(f"/api/v1/accounts/{other.user_id}/roles", json={"roles": ["admin"]})
    allowed = await admin.put(
        f"/api/v1/accounts/{admin.user_id}/roles", json={"roles": ["employee"]}
    )
    assert allowed.status_code == 200


async def test_granting_the_same_set_twice_is_refused(platform: Platform) -> None:
    """A no-op write that reports success hides the fact that nothing happened."""
    admin = await platform.admin()
    subject = await platform.account(roles=("employee",))

    response = await admin.put(
        f"/api/v1/accounts/{subject.user_id}/roles", json={"roles": ["employee"]}
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ERR_ACC_005"


# --- the change takes effect immediately ------------------------------------


async def test_a_granted_role_works_on_the_very_next_request(platform: Platform) -> None:
    admin = await platform.admin()
    subject = await platform.account(roles=("employee",))
    employee_id = await platform.employee()
    await admin.put(f"/api/v1/employees/{employee_id}/private", json={"employee_no": "E-1"})

    before = await subject.get(f"/api/v1/employees/{employee_id}")
    assert before.json().get("private") is None, "an employee cannot read withheld details"

    await admin.put(f"/api/v1/accounts/{subject.user_id}/roles", json={"roles": ["hr"]})

    after = await subject.get(f"/api/v1/employees/{employee_id}")
    assert after.json()["private"]["employee_no"] == "E-1"


async def test_a_revoked_role_stops_working_on_the_very_next_request(
    platform: Platform,
) -> None:
    """The hole this closes is a permission that lags behind the decision to
    remove it — a revocation with a timer on it."""
    admin = await platform.admin()
    subject = await platform.account(roles=("hr",))

    assert (await subject.get("/api/v1/accounts")).status_code == 403

    await admin.put(
        f"/api/v1/accounts/{subject.user_id}/roles", json={"roles": ["employee"]}
    )

    assert (await subject.get("/api/v1/accounts")).status_code == 403
    assert "hr" not in await roles_of(platform, subject.user_id)


async def test_a_revocation_removes_the_cached_snapshot(platform: Platform) -> None:
    """Belt and braces on the mechanism: the key changes *and* the entry is dropped."""
    admin = await platform.admin()
    subject = await platform.account(roles=("hr",))

    await subject.get("/api/v1/departments")
    assert await principal_of(platform, subject.user_id) is not None

    await admin.put(
        f"/api/v1/accounts/{subject.user_id}/roles", json={"roles": ["employee"]}
    )
    snapshot = await principal_of(platform, subject.user_id)

    assert snapshot.roles == frozenset({"employee"})


# --- the manager role is derived, not only granted --------------------------


async def test_a_managerial_position_confers_the_manager_role(platform: Platform) -> None:
    """No grant is involved: the position decides, which is where the requirement
    says the role comes from."""
    department = await platform.department("direccion")
    position = await platform.position(department, "dir-general", is_managerial=True)
    actor: Actor = await platform.account(roles=("employee",))
    await platform.assign(actor.employee_id, department, position)

    principal = await principal_of(platform, actor.user_id)

    assert "manager" in principal.roles
    assert await roles_of(platform, actor.user_id) == {"employee"}, "nothing was granted"
