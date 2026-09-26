"""Position catalogue over HTTP against real PostgreSQL."""

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import db_session
from app.main import app

STRUCTURE_HEADERS = {"X-Actor-Roles": "hr"}


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async def override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[db_session] = override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


async def make_department(client: AsyncClient, code: str) -> dict:
    response = await client.post(
        "/api/v1/departments",
        json={"code": code, "name_es": code, "name_en": code},
        headers=STRUCTURE_HEADERS,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def create_position(client: AsyncClient, department_id: str, code: str, **extra: object):
    response = await client.post(
        "/api/v1/positions",
        json={
            "code": code,
            "title_es": f"{code} es",
            "title_en": f"{code} en",
            "department_id": department_id,
            **extra,
        },
        headers=STRUCTURE_HEADERS,
    )
    return response


async def test_a_position_carries_its_department(client: AsyncClient) -> None:
    department = await make_department(client, "rrhh")

    response = await create_position(client, department["id"], "tech")

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["department_code"] == "rrhh"
    assert body["is_managerial"] is False
    assert body["is_active"] is True
    assert body["active_assignment_count"] == 0


async def test_codes_are_unique_per_department_not_globally(client: AsyncClient) -> None:
    first = await make_department(client, "rrhh")
    second = await make_department(client, "finanzas")

    assert (await create_position(client, first["id"], "manager")).status_code == 201
    # The same code under another department is legitimate.
    assert (await create_position(client, second["id"], "manager")).status_code == 201
    # Twice in one department is not.
    duplicate = await create_position(client, first["id"], "manager")
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "ERR_POS_002"


async def test_a_position_needs_a_real_department(client: AsyncClient) -> None:
    response = await create_position(client, str(uuid4()), "tech")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ERR_POS_003"


async def test_an_inactive_department_cannot_gain_positions(client: AsyncClient) -> None:
    department = await make_department(client, "rrhh")
    await client.patch(
        f"/api/v1/departments/{department['id']}",
        json={"is_active": False},
        headers=STRUCTURE_HEADERS,
    )

    response = await create_position(client, department["id"], "tech")

    assert response.status_code == 422


async def test_the_catalogue_can_be_filtered_by_department(client: AsyncClient) -> None:
    first = await make_department(client, "rrhh")
    second = await make_department(client, "finanzas")
    await create_position(client, first["id"], "tech")
    await create_position(client, second["id"], "analyst")

    response = await client.get("/api/v1/positions", params={"department_id": first["id"]})

    assert [row["code"] for row in response.json()] == ["tech"]


async def test_a_position_in_use_cannot_be_deleted(
    client: AsyncClient, session: AsyncSession
) -> None:
    """Deleting it would leave live assignments pointing at nothing."""
    department = await make_department(client, "rrhh")
    position = (await create_position(client, department["id"], "tech")).json()
    employee = (
        await client.post(
            "/api/v1/employees",
            json={
                "first_name": "Ana",
                "last_name": "Martín",
                "email": "ana@empresa.es",
                "hire_date": "2024-01-15",
            },
            headers=STRUCTURE_HEADERS,
        )
    ).json()
    await client.post(
        f"/api/v1/employees/{employee['id']}/assignments",
        json={
            "department_id": department["id"],
            "job_position_id": position["id"],
            "start_date": "2024-01-15",
        },
        headers=STRUCTURE_HEADERS,
    )

    # The list view exposes why, so an operator is not left guessing.
    listed = await client.get("/api/v1/positions", params={"department_id": department["id"]})
    assert listed.json()[0]["active_assignment_count"] == 1

    response = await client.delete(
        f"/api/v1/positions/{position['id']}", headers=STRUCTURE_HEADERS
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ERR_POS_004"


async def test_deactivating_retires_a_position_that_is_in_use(
    client: AsyncClient,
) -> None:
    """The supported path for replacing a role: keep history resolving."""
    department = await make_department(client, "rrhh")
    position = (await create_position(client, department["id"], "tech")).json()
    employee = (
        await client.post(
            "/api/v1/employees",
            json={
                "first_name": "Ana",
                "last_name": "Martín",
                "email": "ana@empresa.es",
                "hire_date": "2024-01-15",
            },
            headers=STRUCTURE_HEADERS,
        )
    ).json()
    await client.post(
        f"/api/v1/employees/{employee['id']}/assignments",
        json={
            "department_id": department["id"],
            "job_position_id": position["id"],
            "start_date": "2024-01-15",
        },
        headers=STRUCTURE_HEADERS,
    )

    response = await client.post(
        f"/api/v1/positions/{position['id']}/deactivate", headers=STRUCTURE_HEADERS
    )

    assert response.status_code == 200
    assert response.json()["is_active"] is False
    # The existing assignment still resolves.
    profile = await client.get(f"/api/v1/employees/{employee['id']}", headers=STRUCTURE_HEADERS)
    assert profile.json()["assignments"][0]["job_title_es"] == "tech es"


async def test_a_deactivated_position_cannot_take_new_assignments(
    client: AsyncClient,
) -> None:
    department = await make_department(client, "rrhh")
    position = (await create_position(client, department["id"], "tech")).json()
    await client.post(
        f"/api/v1/positions/{position['id']}/deactivate", headers=STRUCTURE_HEADERS
    )
    employee = (
        await client.post(
            "/api/v1/employees",
            json={
                "first_name": "Ana",
                "last_name": "Martín",
                "email": "ana@empresa.es",
                "hire_date": "2024-01-15",
            },
            headers=STRUCTURE_HEADERS,
        )
    ).json()

    response = await client.post(
        f"/api/v1/employees/{employee['id']}/assignments",
        json={
            "department_id": department["id"],
            "job_position_id": position["id"],
            "start_date": "2024-01-15",
        },
        headers=STRUCTURE_HEADERS,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ERR_EMP_006"


async def test_an_unused_position_can_be_deleted(client: AsyncClient) -> None:
    department = await make_department(client, "rrhh")
    position = (await create_position(client, department["id"], "tech")).json()

    response = await client.delete(
        f"/api/v1/positions/{position['id']}", headers=STRUCTURE_HEADERS
    )

    assert response.status_code == 204
    assert (await client.get(f"/api/v1/positions/{position['id']}")).status_code == 404


async def test_writes_require_a_structure_role(client: AsyncClient) -> None:
    department = await make_department(client, "rrhh")

    response = await client.post(
        "/api/v1/positions",
        json={
            "code": "tech",
            "title_es": "x",
            "title_en": "x",
            "department_id": department["id"],
        },
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ERR_AUTH_002"


async def test_reads_are_open_until_authentication_lands(client: AsyncClient) -> None:
    response = await client.get("/api/v1/positions")

    assert response.status_code == 200


async def test_unknown_fields_are_refused(client: AsyncClient) -> None:
    """Same closed-contract rule as the employee payload."""
    department = await make_department(client, "rrhh")

    response = await client.post(
        "/api/v1/positions",
        json={
            "code": "tech",
            "title_es": "x",
            "title_en": "x",
            "department_id": department["id"],
            "salary_band": "B2",
        },
        headers=STRUCTURE_HEADERS,
    )

    assert response.status_code == 422


async def test_inactive_positions_can_be_excluded(client: AsyncClient) -> None:
    department = await make_department(client, "rrhh")
    live = (await create_position(client, department["id"], "live")).json()
    retired = (await create_position(client, department["id"], "retired")).json()
    await client.post(
        f"/api/v1/positions/{retired['id']}/deactivate", headers=STRUCTURE_HEADERS
    )

    everything = await client.get("/api/v1/positions")
    active_only = await client.get("/api/v1/positions", params={"include_inactive": "false"})

    assert len(everything.json()) == 2
    assert [row["id"] for row in active_only.json()] == [live["id"]]


async def test_the_managerial_flag_round_trips(client: AsyncClient) -> None:
    department = await make_department(client, "rrhh")

    created = (await create_position(client, department["id"], "head", is_managerial=True)).json()

    assert created["is_managerial"] is True
    fetched = await client.get(f"/api/v1/positions/{created['id']}")
    assert fetched.json()["is_managerial"] is True
