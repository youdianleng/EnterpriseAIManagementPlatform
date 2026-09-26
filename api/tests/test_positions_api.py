"""Position catalogue over HTTP against real PostgreSQL.

Callers are real accounts with real sessions: writes go through a managing actor,
reads through any signed-in employee, and the kernel decides rather than a header.
"""

from uuid import uuid4

from tests.support.platform import Platform


async def make_department(platform: Platform, code: str) -> str:
    """Scaffolding: these tests are about positions, not departments."""
    return await platform.department(code)


async def create_position(platform: Platform, department_id: str, code: str, **extra: object):
    """The raw response, because the rejection cases assert on the error code."""
    admin = await platform.account(roles=("admin",))
    return await admin.post(
        "/api/v1/positions",
        json={
            "code": code,
            "title_es": f"{code} es",
            "title_en": f"{code} en",
            "department_id": department_id,
            **extra,
        },
    )


async def test_a_position_carries_its_department(platform: Platform) -> None:
    department = await make_department(platform, "rrhh")

    response = await create_position(platform, department, "tech")

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["department_code"] == "rrhh"
    assert body["is_managerial"] is False
    assert body["is_active"] is True
    assert body["active_assignment_count"] == 0


async def test_codes_are_unique_per_department_not_globally(platform: Platform) -> None:
    first = await make_department(platform, "rrhh")
    second = await make_department(platform, "finanzas")

    assert (await create_position(platform, first, "manager")).status_code == 201
    # The same code under another department is legitimate.
    assert (await create_position(platform, second, "manager")).status_code == 201
    # Twice in one department is not.
    duplicate = await create_position(platform, first, "manager")
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "ERR_POS_002"


async def test_a_position_needs_a_real_department(platform: Platform) -> None:
    response = await create_position(platform, str(uuid4()), "tech")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ERR_POS_003"


async def test_an_inactive_department_cannot_gain_positions(platform: Platform) -> None:
    department = await make_department(platform, "rrhh")
    admin = await platform.account(roles=("admin",))
    await admin.patch(f"/api/v1/departments/{department}", json={"is_active": False})

    response = await create_position(platform, department, "tech")

    assert response.status_code == 422


async def test_the_catalogue_can_be_filtered_by_department(platform: Platform) -> None:
    first = await make_department(platform, "rrhh")
    second = await make_department(platform, "finanzas")
    await create_position(platform, first, "tech")
    await create_position(platform, second, "analyst")
    reader = await platform.account(roles=("employee",))

    response = await reader.get("/api/v1/positions", params={"department_id": first})

    assert response.status_code == 200, response.text
    assert [row["code"] for row in response.json()] == ["tech"]


async def test_a_position_in_use_cannot_be_deleted(platform: Platform) -> None:
    """Deleting it would leave live assignments pointing at nothing."""
    department = await make_department(platform, "rrhh")
    position = (await create_position(platform, department, "tech")).json()
    employee = await platform.employee(email="ana@empresa.es")
    await platform.assign(employee, department, position["id"])

    # The list view exposes why, so an operator is not left guessing.
    reader = await platform.account(roles=("employee",))
    listed = await reader.get("/api/v1/positions", params={"department_id": department})
    assert listed.status_code == 200, listed.text
    assert listed.json()[0]["active_assignment_count"] == 1

    admin = await platform.account(roles=("admin",))
    response = await admin.delete(f"/api/v1/positions/{position['id']}")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ERR_POS_004"


async def test_deactivating_retires_a_position_that_is_in_use(platform: Platform) -> None:
    """The supported path for replacing a role: keep history resolving."""
    department = await make_department(platform, "rrhh")
    position = (await create_position(platform, department, "tech")).json()
    holder = await platform.account(roles=("employee",))
    await platform.assign(holder.employee_id, department, position["id"])

    admin = await platform.account(roles=("admin",))
    response = await admin.post(f"/api/v1/positions/{position['id']}/deactivate")

    assert response.status_code == 200
    assert response.json()["is_active"] is False
    # The existing assignment still resolves. Read as the holder, because an
    # administrator is not privileged here and would see no assignments at all.
    profile = await holder.get("/api/v1/employees/me")
    assert profile.status_code == 200, profile.text
    assert profile.json()["assignments"][0]["job_title_es"] == "tech es"


async def test_a_deactivated_position_cannot_take_new_assignments(platform: Platform) -> None:
    department = await make_department(platform, "rrhh")
    position = (await create_position(platform, department, "tech")).json()
    admin = await platform.account(roles=("admin",))
    await admin.post(f"/api/v1/positions/{position['id']}/deactivate")
    employee = await platform.employee(email="ana@empresa.es")

    response = await admin.post(
        f"/api/v1/employees/{employee}/assignments",
        json={
            "department_id": department,
            "job_position_id": position["id"],
            "start_date": "2024-01-15",
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ERR_EMP_006"


async def test_an_unused_position_can_be_deleted(platform: Platform) -> None:
    department = await make_department(platform, "rrhh")
    position = (await create_position(platform, department, "tech")).json()
    admin = await platform.account(roles=("admin",))
    reader = await platform.account(roles=("employee",))

    response = await admin.delete(f"/api/v1/positions/{position['id']}")

    assert response.status_code == 204
    assert (await reader.get(f"/api/v1/positions/{position['id']}")).status_code == 404


async def test_writes_require_a_managing_role(platform: Platform) -> None:
    department = await make_department(platform, "rrhh")
    employee = await platform.account(roles=("employee",))

    response = await employee.post(
        "/api/v1/positions",
        json={
            "code": "tech",
            "title_es": "x",
            "title_en": "x",
            "department_id": department,
        },
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ERR_AUTH_002"


async def test_reads_require_a_session(platform: Platform) -> None:
    """Anonymous is no longer a caller; any signed-in employee still reads."""
    anonymous = await platform.client.get("/api/v1/positions")

    assert anonymous.status_code == 401
    assert anonymous.json()["error"]["code"] == "ERR_SES_001"

    employee = await platform.account(roles=("employee",))
    assert (await employee.get("/api/v1/positions")).status_code == 200


async def test_unknown_fields_are_refused(platform: Platform) -> None:
    """Same closed-contract rule as the employee payload."""
    department = await make_department(platform, "rrhh")

    response = await create_position(platform, department, "tech", salary_band="B2")

    assert response.status_code == 422


async def test_inactive_positions_can_be_excluded(platform: Platform) -> None:
    department = await make_department(platform, "rrhh")
    live = (await create_position(platform, department, "live")).json()
    retired = (await create_position(platform, department, "retired")).json()
    admin = await platform.account(roles=("admin",))
    await admin.post(f"/api/v1/positions/{retired['id']}/deactivate")
    reader = await platform.account(roles=("employee",))

    everything = await reader.get("/api/v1/positions")
    active_only = await reader.get("/api/v1/positions", params={"include_inactive": "false"})

    assert len(everything.json()) == 2
    assert [row["id"] for row in active_only.json()] == [live["id"]]


async def test_the_managerial_flag_round_trips(platform: Platform) -> None:
    department = await make_department(platform, "rrhh")

    created = (await create_position(platform, department, "head", is_managerial=True)).json()
    assert created["is_managerial"] is True

    reader = await platform.account(roles=("employee",))
    fetched = await reader.get(f"/api/v1/positions/{created['id']}")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["is_managerial"] is True
