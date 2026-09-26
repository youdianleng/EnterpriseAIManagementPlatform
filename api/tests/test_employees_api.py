"""Employee profile and assignment behaviour over HTTP against real PostgreSQL.

Focus: the visibility rule on the wire — that a withheld field is *absent* from
the JSON rather than present and null — plus the assignment ordering that later
decides who approves what.

Callers are real accounts with their own sessions: who is asking is decided by
login and the access kernel, not by a role header on the request.
"""

from datetime import date
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.employee.service import resolve_viewer_context
from app.domain.employee.visibility import project_directory_row
from app.repositories.employee import PostgresEmployeeRepository
from app.repositories.org import PostgresDepartmentRepository
from tests.support.platform import Platform


async def assign(
    platform: Platform, employee_id: str, department_id: str, position_id: str, **extra: object
) -> dict:
    """Attach a position and return the profile as the writer may see it.

    The write is made by an HR actor rather than an administrator: an
    administrator is not privileged in the visibility rule, so the profile it
    gets back withholds the assignment list that these tests assert on.
    """
    actor = await platform.account(roles=("hr",))
    response = await actor.post(
        f"/api/v1/employees/{employee_id}/assignments",
        json={
            "department_id": department_id,
            "job_position_id": position_id,
            "start_date": "2024-01-15",
            **extra,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


# --- the closed field set --------------------------------------------------


async def test_the_api_cannot_be_asked_to_store_forbidden_fields(platform: Platform) -> None:
    """Extra keys are not silently dropped: the schema does not have them, so a
    client trying to send an ID number gets a validation error rather than a
    quiet success."""
    admin = await platform.account(roles=("admin",))

    response = await admin.post(
        "/api/v1/employees",
        json={
            "first_name": "Ana",
            "last_name": "Martín",
            "email": "ana@empresa.es",
            "hire_date": "2024-01-15",
            "national_id": "12345678Z",
        },
    )

    assert response.status_code == 422, response.text


async def test_private_payload_rejects_unknown_fields(platform: Platform) -> None:
    admin = await platform.account(roles=("admin",))
    employee = await platform.employee()

    response = await admin.put(
        f"/api/v1/employees/{employee}/private",
        json={"iban": "ES0000000000000000000000"},
    )

    assert response.status_code == 422


# --- visibility on the wire ------------------------------------------------


async def test_a_colleague_sees_directory_fields_but_not_withheld_ones(platform: Platform) -> None:
    admin = await platform.account(roles=("admin",))
    department = await platform.department("rrhh")
    position = await platform.position(department, "tech")
    ana = await platform.employee(email="ana@empresa.es")
    await platform.assign(ana, department, position)
    await admin.put(
        f"/api/v1/employees/{ana}/private",
        json={"address_line": "Calle Mayor 1", "employee_no": "E-0042"},
    )
    # A colleague is a second signed-in person holding a position in the same
    # department; nothing else makes them a colleague.
    colleague = await platform.account(roles=("employee",))
    await platform.assign(colleague.employee_id, department, position)

    response = await colleague.get(f"/api/v1/employees/{ana}")

    assert response.status_code == 200
    body = response.json()
    assert body["visibility"] == "directory"
    assert body["email"] == "ana@empresa.es"
    # Absent, not null: a client cannot mistake "withheld" for "not recorded".
    assert "private" not in body
    assert "employee_no" not in response.text
    assert "Calle Mayor" not in response.text


async def test_the_person_sees_their_own_withheld_fields(platform: Platform) -> None:
    admin = await platform.account(roles=("admin",))
    department = await platform.department("rrhh")
    position = await platform.position(department, "tech")
    # The subject needs a session of their own, which only an account provides.
    ana = await platform.account(roles=("employee",))
    await platform.assign(ana.employee_id, department, position)
    await admin.put(
        f"/api/v1/employees/{ana.employee_id}/private",
        json={"address_line": "Calle Mayor 1", "employee_no": "E-0042"},
    )

    response = await ana.get("/api/v1/employees/me")

    assert response.status_code == 200
    body = response.json()
    assert body["visibility"] == "self"
    assert body["private"]["address_line"] == "Calle Mayor 1"
    assert body["private"]["employee_no"] == "E-0042"


@pytest.mark.parametrize("role", ["hr", "finance", "compliance"])
async def test_privileged_roles_see_withheld_fields(platform: Platform, role: str) -> None:
    admin = await platform.account(roles=("admin",))
    department = await platform.department(f"dept{role}")
    position = await platform.position(department, "tech")
    ana = await platform.employee(email=f"ana-{role}@empresa.es")
    await platform.assign(ana, department, position)
    await admin.put(
        f"/api/v1/employees/{ana}/private",
        json={"employee_no": f"E-{role}"},
    )
    # The viewer shares no department with the subject: privilege alone suffices.
    viewer = await platform.account(roles=(role,))

    response = await viewer.get(f"/api/v1/employees/{ana}")

    assert response.status_code == 200
    assert response.json()["private"]["employee_no"] == f"E-{role}"


async def test_the_directory_withholds_email_outside_the_department(
    platform: Platform, session: AsyncSession
) -> None:
    """The contact list is readable by everyone; only colleagues get an address."""
    mine = await platform.department("mine")
    theirs = await platform.department("theirs")
    me = await platform.account(roles=("employee",))
    await platform.assign(me.employee_id, mine, await platform.position(mine, "tech"))
    stranger = await platform.employee(email="stranger@empresa.es")
    await platform.assign(stranger, theirs, await platform.position(theirs, "analyst"))

    response = await me.get("/api/v1/employees/directory")

    rows = {row["employee_id"]: row for row in response.json()}

    # What the rule says, for the same rows, using the same entries the endpoint
    # read. Comparing the two separates "the rule is wrong" from "the response
    # does not match the rule".
    entries = await PostgresEmployeeRepository(session).list_directory()
    context = await resolve_viewer_context(
        employee_id=UUID(me.employee_id),
        roles=frozenset({"employee"}),
        clearance_level="low",
        departments=PostgresDepartmentRepository(session),
        employee_repository=PostgresEmployeeRepository(session),
    )
    expected_email = {
        str(entry.employee_id): ("email" in project_directory_row(context, entry))
        for entry in entries
    }

    assert {key: ("email" in row) for key, row in rows.items()} == expected_email
    # The viewer's own address, taken from the fixture rather than hardcoded: the
    # fixture generates it, and a literal here would assert a value it never made.
    assert rows[me.employee_id]["email"] == me.email
    # Absent, not null: "not allowed to know" must not read as "not recorded".
    assert "email" not in rows[stranger]


# --- assignments -----------------------------------------------------------


async def test_the_first_assignment_becomes_primary(platform: Platform) -> None:
    department = await platform.department("rrhh")
    position = await platform.position(department, "tech")
    ana = await platform.employee()

    body = await assign(platform, ana, department, position)

    assert len(body["assignments"]) == 1
    assert body["assignments"][0]["is_primary"] is True


async def test_a_second_assignment_is_not_primary(platform: Platform) -> None:
    first = await platform.department("rrhh")
    second = await platform.department("finanzas")
    first_position = await platform.position(first, "tech")
    second_position = await platform.position(second, "analyst")
    ana = await platform.employee()
    await assign(platform, ana, first, first_position)

    body = await assign(platform, ana, second, second_position)

    primaries = [a for a in body["assignments"] if a["is_primary"]]
    assert len(primaries) == 1
    assert primaries[0]["department_id"] == first


async def test_an_employee_can_hold_positions_in_two_departments(platform: Platform) -> None:
    first = await platform.department("rrhh")
    second = await platform.department("finanzas")
    ana = await platform.employee()
    await assign(platform, ana, first, await platform.position(first, "tech"))
    body = await assign(platform, ana, second, await platform.position(second, "analyst"))

    assert len(body["assignments"]) == 2
    assert {a["department_code"] for a in body["assignments"]} == {"rrhh", "finanzas"}


async def test_a_client_cannot_promote_an_assignment(platform: Platform) -> None:
    """`is_primary` is not an input: promotion is an administrative action."""
    admin = await platform.account(roles=("admin",))
    department = await platform.department("rrhh")
    position = await platform.position(department, "tech")
    ana = await platform.employee()

    response = await admin.post(
        f"/api/v1/employees/{ana}/assignments",
        json={
            "department_id": department,
            "job_position_id": position,
            "start_date": "2024-01-15",
            "is_primary": True,
        },
    )

    assert response.status_code == 422


async def test_promoting_a_position_is_administrative(platform: Platform) -> None:
    hr = await platform.account(roles=("hr",))
    employee = await platform.account(roles=("employee",))
    first = await platform.department("rrhh")
    second = await platform.department("finanzas")
    ana = await platform.employee()
    await assign(platform, ana, first, await platform.position(first, "tech"))
    body = await assign(platform, ana, second, await platform.position(second, "analyst"))
    target = next(a for a in body["assignments"] if a["department_code"] == "finanzas")

    # Without a managing role the promotion is refused.
    refused = await employee.put(f"/api/v1/employees/{ana}/assignments/{target['id']}/primary")
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "ERR_AUTH_002"

    promoted = await hr.put(f"/api/v1/employees/{ana}/assignments/{target['id']}/primary")
    assert promoted.status_code == 200
    primaries = [a for a in promoted.json()["assignments"] if a["is_primary"]]
    assert len(primaries) == 1
    assert primaries[0]["department_code"] == "finanzas"


async def test_the_effective_approver_falls_back_to_the_department_manager(
    platform: Platform,
) -> None:
    """A client should not have to know the fallback rule to render a route."""
    department = await platform.department("rrhh")
    position = await platform.position(department, "tech")
    boss = await platform.employee(email="boss@empresa.es")
    ana = await platform.employee()
    await platform.sql(
        "UPDATE departments SET manager_employee_id = :boss WHERE id = :id",
        {"boss": boss, "id": department},
    )

    body = await assign(platform, ana, department, position)

    # The explicit approver was not supplied, so it is absent rather than null;
    # the resolved fallback is what a client renders.
    assert "manager_employee_id" not in body["assignments"][0]
    assert body["assignments"][0]["effective_approver_employee_id"] == boss


async def test_an_explicit_approver_overrides_the_department_manager(platform: Platform) -> None:
    department = await platform.department("rrhh")
    position = await platform.position(department, "tech")
    boss = await platform.employee(email="boss@empresa.es")
    lead = await platform.employee(email="lead@empresa.es")
    ana = await platform.employee()
    await platform.sql(
        "UPDATE departments SET manager_employee_id = :boss WHERE id = :id",
        {"boss": boss, "id": department},
    )

    body = await assign(platform, ana, department, position, manager_employee_id=lead)

    assert body["assignments"][0]["effective_approver_employee_id"] == lead


# --- error paths -----------------------------------------------------------


async def test_duplicate_email_returns_a_catalogued_conflict(platform: Platform) -> None:
    admin = await platform.account(roles=("admin",))
    await platform.employee(email="ana@empresa.es")

    response = await admin.post(
        "/api/v1/employees",
        json={
            "first_name": "Luis",
            "last_name": "Fernández",
            "email": "ana@empresa.es",
            "hire_date": "2024-02-01",
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ERR_EMP_002"


async def test_writes_require_a_managing_role(platform: Platform) -> None:
    actor = await platform.account(roles=("employee",))

    response = await actor.post(
        "/api/v1/employees",
        json={
            "first_name": "Ana",
            "last_name": "Martín",
            "email": "ana@empresa.es",
            "hire_date": "2024-01-15",
        },
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ERR_AUTH_002"


async def test_unknown_employee_is_a_catalogued_not_found(platform: Platform) -> None:
    actor = await platform.account(roles=("employee",))

    response = await actor.get(f"/api/v1/employees/{uuid4()}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ERR_EMP_001"


async def test_me_without_a_session_is_unauthenticated(platform: Platform) -> None:
    """No session at all: the profile endpoint refuses before it looks anything up."""
    response = await platform.client.get("/api/v1/employees/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "ERR_SES_001"


async def test_termination_before_hire_is_rejected(platform: Platform) -> None:
    admin = await platform.account(roles=("admin",))

    response = await admin.post(
        "/api/v1/employees",
        json={
            "first_name": "Ana",
            "last_name": "Martín",
            "email": "ana@empresa.es",
            "hire_date": "2024-06-01",
            "termination_date": "2024-01-01",
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ERR_EMP_004"


async def test_ending_the_only_position_is_rejected(platform: Platform) -> None:
    hr = await platform.account(roles=("hr",))
    department = await platform.department("rrhh")
    position = await platform.position(department, "tech")
    ana = await platform.employee()
    body = await assign(platform, ana, department, position)

    response = await hr.delete(
        f"/api/v1/employees/{ana}/assignments/{body['assignments'][0]['id']}",
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ERR_EMP_010"


async def test_an_invalid_email_is_rejected(platform: Platform) -> None:
    admin = await platform.account(roles=("admin",))

    response = await admin.post(
        "/api/v1/employees",
        json={
            "first_name": "Ana",
            "last_name": "Martín",
            "email": "not-an-email",
            "hire_date": "2024-01-15",
        },
    )

    assert response.status_code == 422


async def test_terminated_employees_can_be_excluded_from_the_directory(
    platform: Platform,
) -> None:
    admin = await platform.account(roles=("admin",))
    department = await platform.department("rrhh")
    position = await platform.position(department, "tech")
    ana = await platform.employee()
    await platform.assign(ana, department, position)
    await admin.patch(
        f"/api/v1/employees/{ana}",
        json={"status": "terminated", "termination_date": "2026-01-01"},
    )
    # Only people holding a position are listed, so this viewer — like every
    # account the fixture itself creates — does not change the counts below.
    viewer = await platform.account(roles=("employee",))

    everything = await viewer.get(
        "/api/v1/employees/directory", params={"include_terminated": "true"}
    )
    active_only = await viewer.get("/api/v1/employees/directory")

    assert len(everything.json()) == 1
    assert active_only.json() == []


async def test_dates_survive_the_round_trip(platform: Platform) -> None:
    # Read as HR: `hire_date` is withheld from an administrator's view.
    hr = await platform.account(roles=("hr",))
    created = await platform.employee(hire_date="2024-03-01")

    fetched = await hr.get(f"/api/v1/employees/{created}")

    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["hire_date"] == "2024-03-01"


def test_the_module_imports() -> None:
    """Guards against a typo making the whole router unimportable."""
    from app.api.v1 import employees

    assert employees.router is not None
    assert date(2024, 1, 1) < date(2024, 1, 2)
