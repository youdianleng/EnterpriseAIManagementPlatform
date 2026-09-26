"""Employee profile and assignment behaviour over HTTP against real PostgreSQL.

Focus: the visibility rule on the wire — that a withheld field is *absent* from
the JSON rather than present and null — plus the assignment ordering that later
decides who approves what.
"""

from collections.abc import AsyncIterator
from datetime import date
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import db_session
from app.domain.employee.service import resolve_viewer_context
from app.domain.employee.visibility import project_directory_row
from app.main import app
from app.repositories.employee import PostgresEmployeeRepository
from app.repositories.org import PostgresDepartmentRepository

MANAGER_HEADERS = {"X-Actor-Roles": "hr"}


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async def override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[db_session] = override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


async def make_department(session: AsyncSession, code: str, parent_id: UUID | None = None):
    response = await session.execute(
        text(
            """
            INSERT INTO departments (id, code, name_es, name_en, parent_id, path, depth,
                                     clearance_level, is_active)
            VALUES (:id, :code, :code, :code, :parent_id, 'tmp', 0, 'low', true)
            RETURNING id
            """
        ),
        {"id": uuid4(), "code": code, "parent_id": parent_id},
    )
    department_id = response.scalar_one()
    # Path is derived state; the API normally computes it, so set it here.
    await session.execute(
        text("UPDATE departments SET path = CAST(:path AS ltree) WHERE id = :id"),
        {"path": code, "id": department_id},
    )
    return department_id


async def make_position(session: AsyncSession, department_id: UUID, code: str = "tech"):
    position_id = uuid4()
    await session.execute(
        text(
            """
            INSERT INTO job_positions (id, code, title_es, title_en, department_id,
                                       is_managerial, is_active)
            VALUES (:id, :code, :code, :code, :department_id, false, true)
            """
        ),
        {"id": position_id, "code": code, "department_id": department_id},
    )
    return position_id


async def create_employee(
    client: AsyncClient, email: str = "ana@empresa.es", **extra: object
) -> dict:
    response = await client.post(
        "/api/v1/employees",
        json={
            "first_name": "Ana",
            "last_name": "Martín",
            "email": email,
            "hire_date": "2024-01-15",
            **extra,
        },
        headers=MANAGER_HEADERS,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def assign(
    client: AsyncClient,
    employee_id: str,
    department_id: UUID,
    position_id: UUID,
    **extra: object,
) -> dict:
    response = await client.post(
        f"/api/v1/employees/{employee_id}/assignments",
        json={
            "department_id": str(department_id),
            "job_position_id": str(position_id),
            "start_date": "2024-01-15",
            **extra,
        },
        headers=MANAGER_HEADERS,
    )
    assert response.status_code == 201, response.text
    return response.json()


# --- the closed field set --------------------------------------------------


async def test_the_api_cannot_be_asked_to_store_forbidden_fields(
    client: AsyncClient, session: AsyncSession
) -> None:
    """Extra keys are not silently dropped: the schema does not have them, so a
    client trying to send an ID number gets a validation error rather than a
    quiet success."""
    response = await client.post(
        "/api/v1/employees",
        json={
            "first_name": "Ana",
            "last_name": "Martín",
            "email": "ana@empresa.es",
            "hire_date": "2024-01-15",
            "national_id": "12345678Z",
        },
        headers=MANAGER_HEADERS,
    )

    assert response.status_code == 422, response.text


async def test_private_payload_rejects_unknown_fields(
    client: AsyncClient, session: AsyncSession
) -> None:
    employee = await create_employee(client)

    response = await client.put(
        f"/api/v1/employees/{employee['id']}/private",
        json={"iban": "ES0000000000000000000000"},
        headers=MANAGER_HEADERS,
    )

    assert response.status_code == 422


# --- visibility on the wire ------------------------------------------------


async def test_a_colleague_sees_directory_fields_but_not_withheld_ones(
    client: AsyncClient, session: AsyncSession
) -> None:
    department = await make_department(session, "rrhh")
    position = await make_position(session, department)
    ana = await create_employee(client)
    await assign(client, ana["id"], department, position)
    await client.put(
        f"/api/v1/employees/{ana['id']}/private",
        json={"address_line": "Calle Mayor 1", "employee_no": "E-0042"},
        headers=MANAGER_HEADERS,
    )
    colleague = await create_employee(client, email="luis@empresa.es")
    await assign(client, colleague["id"], department, position)

    response = await client.get(
        f"/api/v1/employees/{ana['id']}",
        headers={
            "X-Actor-Roles": "employee",
            "X-Actor-Employee": colleague["id"],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["visibility"] == "directory"
    assert body["email"] == "ana@empresa.es"
    # Absent, not null: a client cannot mistake "withheld" for "not recorded".
    assert "private" not in body
    assert "employee_no" not in response.text
    assert "Calle Mayor" not in response.text


async def test_the_person_sees_their_own_withheld_fields(
    client: AsyncClient, session: AsyncSession
) -> None:
    department = await make_department(session, "rrhh")
    position = await make_position(session, department)
    ana = await create_employee(client)
    await assign(client, ana["id"], department, position)
    await client.put(
        f"/api/v1/employees/{ana['id']}/private",
        json={"address_line": "Calle Mayor 1", "employee_no": "E-0042"},
        headers=MANAGER_HEADERS,
    )

    response = await client.get(
        "/api/v1/employees/me",
        headers={"X-Actor-Roles": "employee", "X-Actor-Employee": ana["id"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["visibility"] == "self"
    assert body["private"]["address_line"] == "Calle Mayor 1"
    assert body["private"]["employee_no"] == "E-0042"


@pytest.mark.parametrize("role", ["hr", "finance", "compliance"])
async def test_privileged_roles_see_withheld_fields(
    client: AsyncClient, session: AsyncSession, role: str
) -> None:
    department = await make_department(session, f"dept{role}")
    position = await make_position(session, department)
    ana = await create_employee(client, email=f"ana-{role}@empresa.es")
    await assign(client, ana["id"], department, position)
    await client.put(
        f"/api/v1/employees/{ana['id']}/private",
        json={"employee_no": f"E-{role}"},
        headers=MANAGER_HEADERS,
    )

    response = await client.get(
        f"/api/v1/employees/{ana['id']}",
        headers={"X-Actor-Roles": role, "X-Actor-Employee": str(uuid4())},
    )

    assert response.json()["private"]["employee_no"] == f"E-{role}"


async def test_the_directory_withholds_email_outside_the_department(
    client: AsyncClient, session: AsyncSession
) -> None:
    """The contact list is readable by everyone; only colleagues get an address."""
    mine = await make_department(session, "mine")
    theirs = await make_department(session, "theirs")
    me = await create_employee(client, email="me@empresa.es")
    await assign(client, me["id"], mine, await make_position(session, mine))
    stranger = await create_employee(client, email="stranger@empresa.es")
    await assign(client, stranger["id"], theirs, await make_position(session, theirs))

    response = await client.get(
        "/api/v1/employees/directory",
        headers={"X-Actor-Roles": "employee", "X-Actor-Employee": me["id"]},
    )

    rows = {row["employee_id"]: row for row in response.json()}

    # What the rule says, for the same rows, using the same entries the endpoint
    # read. Comparing the two separates "the rule is wrong" from "the response
    # does not match the rule".
    entries = await PostgresEmployeeRepository(session).list_directory()
    context = await resolve_viewer_context(
        employee_id=UUID(me["id"]),
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
    assert rows[me["id"]]["email"] == "me@empresa.es"
    # Absent, not null: "not allowed to know" must not read as "not recorded".
    assert "email" not in rows[stranger["id"]]


# --- assignments -----------------------------------------------------------


async def test_the_first_assignment_becomes_primary(
    client: AsyncClient, session: AsyncSession
) -> None:
    department = await make_department(session, "rrhh")
    position = await make_position(session, department)
    ana = await create_employee(client)

    body = await assign(client, ana["id"], department, position)

    assert len(body["assignments"]) == 1
    assert body["assignments"][0]["is_primary"] is True


async def test_a_second_assignment_is_not_primary(
    client: AsyncClient, session: AsyncSession
) -> None:
    first = await make_department(session, "rrhh")
    second = await make_department(session, "finanzas")
    first_position = await make_position(session, first)
    second_position = await make_position(session, second, code="analyst")
    ana = await create_employee(client)
    await assign(client, ana["id"], first, first_position)

    body = await assign(client, ana["id"], second, second_position)

    primaries = [a for a in body["assignments"] if a["is_primary"]]
    assert len(primaries) == 1
    assert primaries[0]["department_id"] == str(first)


async def test_an_employee_can_hold_positions_in_two_departments(
    client: AsyncClient, session: AsyncSession
) -> None:
    first = await make_department(session, "rrhh")
    second = await make_department(session, "finanzas")
    ana = await create_employee(client)
    await assign(client, ana["id"], first, await make_position(session, first))
    body = await assign(
        client, ana["id"], second, await make_position(session, second, code="analyst")
    )

    assert len(body["assignments"]) == 2
    assert {a["department_code"] for a in body["assignments"]} == {"rrhh", "finanzas"}


async def test_a_client_cannot_promote_an_assignment(
    client: AsyncClient, session: AsyncSession
) -> None:
    """`is_primary` is not an input: promotion is an administrative action."""
    department = await make_department(session, "rrhh")
    position = await make_position(session, department)
    ana = await create_employee(client)

    response = await client.post(
        f"/api/v1/employees/{ana['id']}/assignments",
        json={
            "department_id": str(department),
            "job_position_id": str(position),
            "start_date": "2024-01-15",
            "is_primary": True,
        },
        headers=MANAGER_HEADERS,
    )

    assert response.status_code == 422


async def test_promoting_a_position_is_administrative(
    client: AsyncClient, session: AsyncSession
) -> None:
    first = await make_department(session, "rrhh")
    second = await make_department(session, "finanzas")
    ana = await create_employee(client)
    await assign(client, ana["id"], first, await make_position(session, first))
    body = await assign(
        client, ana["id"], second, await make_position(session, second, code="analyst")
    )
    target = next(a for a in body["assignments"] if a["department_code"] == "finanzas")

    # Without a managing role the promotion is refused.
    refused = await client.put(
        f"/api/v1/employees/{ana['id']}/assignments/{target['id']}/primary",
        headers={"X-Actor-Roles": "employee"},
    )
    assert refused.status_code == 403

    promoted = await client.put(
        f"/api/v1/employees/{ana['id']}/assignments/{target['id']}/primary",
        headers=MANAGER_HEADERS,
    )
    assert promoted.status_code == 200
    primaries = [a for a in promoted.json()["assignments"] if a["is_primary"]]
    assert len(primaries) == 1
    assert primaries[0]["department_code"] == "finanzas"


async def test_the_effective_approver_falls_back_to_the_department_manager(
    client: AsyncClient, session: AsyncSession
) -> None:
    """A client should not have to know the fallback rule to render a route."""
    department = await make_department(session, "rrhh")
    position = await make_position(session, department)
    boss = await create_employee(client, email="boss@empresa.es")
    ana = await create_employee(client)
    await session.execute(
        text("UPDATE departments SET manager_employee_id = :boss WHERE id = :id"),
        {"boss": UUID(boss["id"]), "id": department},
    )

    body = await assign(client, ana["id"], department, position)

    # The explicit approver was not supplied, so it is absent rather than null;
    # the resolved fallback is what a client renders.
    assert "manager_employee_id" not in body["assignments"][0]
    assert body["assignments"][0]["effective_approver_employee_id"] == boss["id"]


async def test_an_explicit_approver_overrides_the_department_manager(
    client: AsyncClient, session: AsyncSession
) -> None:
    department = await make_department(session, "rrhh")
    position = await make_position(session, department)
    boss = await create_employee(client, email="boss@empresa.es")
    lead = await create_employee(client, email="lead@empresa.es")
    ana = await create_employee(client)
    await session.execute(
        text("UPDATE departments SET manager_employee_id = :boss WHERE id = :id"),
        {"boss": UUID(boss["id"]), "id": department},
    )

    body = await assign(
        client, ana["id"], department, position, manager_employee_id=lead["id"]
    )

    assert body["assignments"][0]["effective_approver_employee_id"] == lead["id"]


# --- error paths -----------------------------------------------------------


async def test_duplicate_email_returns_a_catalogued_conflict(client: AsyncClient) -> None:
    await create_employee(client)

    response = await client.post(
        "/api/v1/employees",
        json={
            "first_name": "Luis",
            "last_name": "Fernández",
            "email": "ana@empresa.es",
            "hire_date": "2024-02-01",
        },
        headers=MANAGER_HEADERS,
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ERR_EMP_002"


async def test_writes_require_a_managing_role(client: AsyncClient) -> None:
    response = await client.post(
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


async def test_unknown_employee_is_a_catalogued_not_found(client: AsyncClient) -> None:
    response = await client.get(f"/api/v1/employees/{uuid4()}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ERR_EMP_001"


async def test_me_without_an_acting_employee_is_unauthenticated(client: AsyncClient) -> None:
    response = await client.get("/api/v1/employees/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "ERR_AUTH_001"


async def test_termination_before_hire_is_rejected(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/employees",
        json={
            "first_name": "Ana",
            "last_name": "Martín",
            "email": "ana@empresa.es",
            "hire_date": "2024-06-01",
            "termination_date": "2024-01-01",
        },
        headers=MANAGER_HEADERS,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ERR_EMP_004"


async def test_ending_the_only_position_is_rejected(
    client: AsyncClient, session: AsyncSession
) -> None:
    department = await make_department(session, "rrhh")
    position = await make_position(session, department)
    ana = await create_employee(client)
    body = await assign(client, ana["id"], department, position)

    response = await client.delete(
        f"/api/v1/employees/{ana['id']}/assignments/{body['assignments'][0]['id']}",
        headers=MANAGER_HEADERS,
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ERR_EMP_010"


async def test_an_invalid_email_is_rejected(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/employees",
        json={
            "first_name": "Ana",
            "last_name": "Martín",
            "email": "not-an-email",
            "hire_date": "2024-01-15",
        },
        headers=MANAGER_HEADERS,
    )

    assert response.status_code == 422


async def test_terminated_employees_can_be_excluded_from_the_directory(
    client: AsyncClient, session: AsyncSession
) -> None:
    department = await make_department(session, "rrhh")
    position = await make_position(session, department)
    ana = await create_employee(client)
    await assign(client, ana["id"], department, position)
    await client.patch(
        f"/api/v1/employees/{ana['id']}",
        json={"status": "terminated", "termination_date": "2026-01-01"},
        headers=MANAGER_HEADERS,
    )

    everything = await client.get(
        "/api/v1/employees/directory", params={"include_terminated": "true"}
    )
    active_only = await client.get("/api/v1/employees/directory")

    assert len(everything.json()) == 1
    assert active_only.json() == []


async def test_dates_survive_the_round_trip(client: AsyncClient) -> None:
    created = await create_employee(client, hire_date="2024-03-01")

    fetched = await client.get(
        f"/api/v1/employees/{created['id']}", headers=MANAGER_HEADERS
    )

    assert fetched.json()["hire_date"] == "2024-03-01"


def test_the_module_imports() -> None:
    """Guards against a typo making the whole router unimportable."""
    from app.api.v1 import employees

    assert employees.router is not None
    assert date(2024, 1, 1) < date(2024, 1, 2)
