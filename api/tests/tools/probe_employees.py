"""Acceptance probe for ticket 07.

Drives the employee API over real HTTP against the running stack: the closed
field set, multi-position assignments, the effective approver, and the
visibility line between a colleague, the person and HR.

Positions are inserted straight into the database because the catalogue UI
arrives in ticket 08; everything else goes through HTTP.

Run inside the compose network:
    docker compose exec -T api python /app/tests/tools/probe_employees.py
"""

import asyncio
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from uuid import UUID, uuid4

# Run as a script from /app/tests/tools, so the package root is not on the path.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import build_engine  # noqa: E402

BASE = "http://localhost:8000/api/v1"

failures: list[str] = []


def check(label: str, condition: bool, observed: object = "") -> None:
    print(f"[{'ok  ' if condition else 'FAIL'}] {label}: {observed}")
    if not condition:
        failures.append(label)


def call(
    method: str,
    path: str,
    body: dict | None = None,
    *,
    actor: str | None = None,
    roles: str = "hr",
) -> tuple[int, object]:
    headers = {"X-Actor-Roles": roles, "Content-Type": "application/json"}
    if actor:
        headers["X-Actor-Employee"] = actor
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        response = urllib.request.urlopen(request, timeout=10)
        payload = response.read()
        return response.status, json.loads(payload) if payload else None
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        return exc.code, json.loads(payload) if payload else None


async def with_session(callback):
    engine = build_engine(get_settings())
    try:
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as session:
            return await callback(session)
    finally:
        await engine.dispose()


def sql(statement: str, params: dict) -> None:
    async def run(session):
        await session.execute(text(statement), params)
        await session.commit()

    asyncio.run(with_session(run))


def create_position(department_id: str, code: str) -> str:
    position_id = str(uuid4())
    sql(
        "INSERT INTO job_positions (id, code, title_es, title_en, department_id,"
        " is_managerial, is_active) VALUES (:id, :code, :code, :code, :dept, false, true)",
        {"id": position_id, "code": code, "dept": department_id},
    )
    return position_id


def set_department_manager(department_id: str, employee_id: str) -> None:
    sql(
        "UPDATE departments SET manager_employee_id = :e WHERE id = :d",
        {"e": employee_id, "d": department_id},
    )


def create_employee(
    email: str, first: str = "Ana", last: str = "Martín", **extra: object
) -> dict:
    status, body = call(
        "POST",
        "/employees",
        {
            "first_name": first,
            "last_name": last,
            "email": email,
            "hire_date": "2024-01-15",
            **extra,
        },
    )
    assert status == 201, f"create {email} failed: {status} {body}"
    return body


def attach(employee_id: str, department_id: str, position_id: str) -> tuple[int, object]:
    return call(
        "POST",
        f"/employees/{employee_id}/assignments",
        {
            "department_id": department_id,
            "job_position_id": position_id,
            "start_date": "2024-01-15",
        },
    )


def main() -> None:
    suffix = uuid4().hex[:6]

    _, first_department = call(
        "POST", "/departments", {"code": f"p{suffix}", "name_es": "RRHH", "name_en": "HR"}
    )
    _, second_department = call(
        "POST", "/departments", {"code": f"q{suffix}", "name_es": "Finanzas", "name_en": "Finance"}
    )
    check(
        "two departments exist for the multi-position case",
        bool(first_department and second_department),
    )

    position_one = create_position(first_department["id"], f"tech{suffix}")
    position_two = create_position(second_department["id"], f"analyst{suffix}")

    # --- the closed field set ---------------------------------------------
    status, body = call(
        "POST",
        "/employees",
        {
            "first_name": "Ana",
            "last_name": "Martín",
            "email": f"rejected{suffix}@empresa.es",
            "hire_date": "2024-01-15",
            "national_id": "12345678Z",
        },
    )
    check("a forbidden field is refused, not silently dropped", status == 422, status)

    # --- create ------------------------------------------------------------
    ana = create_employee(f"ana{suffix}@empresa.es", city="Madrid")
    status, _ = call(
        "POST",
        "/employees",
        {
            "first_name": "Otra",
            "last_name": "Ana",
            "email": f"ana{suffix}@empresa.es",
            "hire_date": "2024-01-15",
        },
    )
    check("a duplicate email is refused", status == 409, status)

    # --- multi-position ----------------------------------------------------
    status, ana = attach(ana["id"], first_department["id"], position_one)
    check("the first position is attached", status == 201, status)
    check(
        "the first position becomes primary",
        ana["assignments"][0]["is_primary"] is True,
        ana["assignments"][0]["is_primary"],
    )

    status, ana = attach(ana["id"], second_department["id"], position_two)
    check("a second position in another department is attached", status == 201, status)
    check("the person holds two positions", len(ana["assignments"]) == 2, len(ana["assignments"]))
    check(
        "still exactly one primary",
        sum(1 for a in ana["assignments"] if a["is_primary"]) == 1,
    )
    check(
        "a client cannot ask for the primary flag",
        call(
            "POST",
            f"/employees/{ana['id']}/assignments",
            {
                "department_id": first_department["id"],
                "job_position_id": position_one,
                "start_date": "2026-01-01",
                "is_primary": True,
            },
        )[0]
        == 422,
    )

    # --- approver ----------------------------------------------------------
    boss = create_employee(f"boss{suffix}@empresa.es", "Luis", "Jefe")
    set_department_manager(first_department["id"], boss["id"])

    _, ana = call("GET", f"/employees/{ana['id']}")
    primary = next(a for a in ana["assignments"] if a["is_primary"])
    check(
        "the approver falls back to the department manager",
        primary["effective_approver_employee_id"] == boss["id"],
        primary["effective_approver_employee_id"],
    )

    # --- visibility --------------------------------------------------------
    status, _ = call(
        "PUT",
        f"/employees/{ana['id']}/private",
        {"address_line": "Calle Mayor 1", "employee_no": f"E-{suffix}"},
    )
    check("withheld details are stored", status == 200, status)

    _, as_hr = call("GET", f"/employees/{ana['id']}")
    check(
        "HR sees the withheld details",
        as_hr.get("private", {}).get("employee_no") == f"E-{suffix}",
        as_hr.get("private", {}).get("employee_no"),
    )

    _, as_self = call("GET", "/employees/me", actor=ana["id"], roles="employee")
    check(
        "the person sees their own withheld details",
        as_self.get("private", {}).get("address_line") == "Calle Mayor 1",
        as_self.get("private", {}).get("address_line"),
    )

    colleague = create_employee(f"marta{suffix}@empresa.es", "Marta", "Compañera")
    attach(colleague["id"], first_department["id"], position_one)

    status, seen_by_colleague = call(
        "GET", f"/employees/{ana['id']}", actor=colleague["id"], roles="employee"
    )
    check("a colleague sees the record", status == 200, status)
    check(
        "a colleague sees the directory fields",
        seen_by_colleague.get("email") == f"ana{suffix}@empresa.es",
        seen_by_colleague.get("email"),
    )
    check(
        "a colleague does NOT see the withheld block",
        "private" not in seen_by_colleague,
        sorted(seen_by_colleague),
    )
    check(
        "nor the staff number anywhere in the payload",
        f"E-{suffix}" not in json.dumps(seen_by_colleague),
        "absent",
    )

    # --- the directory withholds email outside the department --------------
    # `boss` holds no position, so it is absent from the directory entirely:
    # the contact list lists people who have somewhere to be.
    check(
        "someone with no position is not in the directory",
        boss["id"] not in {row["employee_id"] for row in call("GET", "/employees/directory")[1]},
    )

    _, directory = call(
        "GET", "/employees/directory", actor=colleague["id"], roles="employee"
    )
    by_id = {row["employee_id"]: row for row in directory}
    check(
        "the colleague's own row carries an email",
        by_id.get(colleague["id"], {}).get("email") == f"marta{suffix}@empresa.es",
        by_id.get(colleague["id"], {}).get("email"),
    )
    # `ana` shares the colleague's department, so her address is visible.
    check(
        "a row inside the viewer's department carries an email",
        by_id.get(ana["id"], {}).get("email") == f"ana{suffix}@empresa.es",
        by_id.get(ana["id"], {}).get("email"),
    )

    # A person in another department: their row shows, their address does not.
    outsider = create_employee(f"outsider{suffix}@empresa.es", "Otro", "Departamento")
    attach(outsider["id"], second_department["id"], position_two)
    _, directory = call(
        "GET", "/employees/directory", actor=colleague["id"], roles="employee"
    )
    by_id = {row["employee_id"]: row for row in directory}
    check(
        "a row outside the viewer's department carries no email",
        "email" not in by_id.get(outsider["id"], {"email": None}),
        sorted(by_id.get(outsider["id"], {})),
    )

    # --- cleanup -----------------------------------------------------------
    for employee in (ana, colleague, boss, outsider):
        sql("DELETE FROM employees WHERE id = :id", {"id": UUID(employee["id"])})
    for department in (first_department, second_department):
        call("DELETE", f"/departments/{department['id']}")

    print()
    print("FAIL" if failures else "ALL CHECKS PASSED")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
