"""Acceptance probe for the position catalogue half of ticket 08.

    docker compose exec -T api python /app/tests/tools/probe_positions.py

The role-assignment half of ticket 08 needs a users table, which arrives with
ticket 09; this probe covers the catalogue.
"""

import asyncio
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4

# Run as a script from /app/tests/tools, so the package root is not on the path.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import build_engine  # noqa: E402

BASE = "http://localhost:8000/api/v1"
HR = {"X-Actor-Roles": "hr", "Content-Type": "application/json"}

failures: list[str] = []


def check(label: str, condition: bool, observed: object = "") -> None:
    print(f"[{'ok  ' if condition else 'FAIL'}] {label}: {observed}")
    if not condition:
        failures.append(label)


def call(method: str, path: str, body: dict | None = None, headers: dict | None = None):
    merged = dict(headers or HR)
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method, headers=merged)
    try:
        response = urllib.request.urlopen(request, timeout=10)
        payload = response.read()
        return response.status, json.loads(payload) if payload else None
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        return exc.code, json.loads(payload) if payload else None


def department(code: str) -> dict:
    status, body = call("POST", "/departments", {"code": code, "name_es": code, "name_en": code})
    assert status == 201, f"department {code}: {status} {body}"
    return body


def sql(statement: str, params: dict) -> None:
    """Direct SQL for cleanup.

    The API deliberately has no "delete an employee" endpoint, and every foreign
    key involved is RESTRICT, so teardown has to go around it.
    """

    async def run(session) -> None:
        await session.execute(text(statement), params)
        await session.commit()

    async def with_engine() -> None:
        engine = build_engine(get_settings())
        try:
            factory = async_sessionmaker(bind=engine, expire_on_commit=False)
            async with factory() as session:
                await run(session)
        finally:
            await engine.dispose()

    asyncio.run(with_engine())


def position(department_id: str, code: str, **extra: object):
    return call(
        "POST",
        "/positions",
        {
            "code": code,
            "title_es": f"{code} es",
            "title_en": f"{code} en",
            "department_id": department_id,
            **extra,
        },
    )


def main() -> None:
    suffix = uuid4().hex[:6]
    first = department(f"a{suffix}")
    second = department(f"b{suffix}")

    status, tech = position(first["id"], "tech", is_managerial=False)
    check("a position is created under its department", status == 201, status)
    check(
        "with the department echoed back",
        tech["department_code"] == f"a{suffix}",
        tech["department_code"],
    )
    check("and the managerial flag recorded", tech["is_managerial"] is False, tech["is_managerial"])

    status, head = position(first["id"], "head", is_managerial=True)
    check("a managerial position can be created", status == 201, status)
    check("and the flag survives", head["is_managerial"] is True, head["is_managerial"])

    status, _ = position(first["id"], "tech")
    check("a duplicate code in the same department is refused", status == 409, status)

    status, _ = position(second["id"], "tech")
    check("the same code in another department is allowed", status == 201, status)

    status, _ = position(str(uuid4()), "orphan")
    check("a position needs a real department", status == 422, status)

    status, listed = call("GET", f"/positions?department_id={first['id']}")
    check(
        "the catalogue filters by department",
        status == 200 and len(listed) == 2,
        len(listed or []),
    )
    status, _ = call(
        "POST",
        "/positions",
        {
            "code": "x",
            "title_es": "x",
            "title_en": "x",
            "department_id": first["id"],
            "salary_band": "B2",
        },
    )
    check("an unknown field is refused rather than dropped", status == 422, status)

    # In use: assign someone, then try to delete.
    status, employee = call(
        "POST",
        "/employees",
        {
            "first_name": "Ana",
            "last_name": "Martín",
            "email": f"ana{suffix}@empresa.es",
            "hire_date": "2024-01-15",
        },
    )
    check("an employee exists for the in-use case", status == 201, status)
    call(
        "POST",
        f"/employees/{employee['id']}/assignments",
        {
            "department_id": first["id"],
            "job_position_id": tech["id"],
            "start_date": "2024-01-15",
        },
    )

    status, listed = call("GET", f"/positions?department_id={first['id']}")
    tech_row = next(row for row in listed if row["code"] == "tech")
    check(
        "the catalogue reports the live assignment count",
        tech_row["active_assignment_count"] == 1,
        tech_row["active_assignment_count"],
    )
    check(
        "and the total, which is what blocks deletion",
        tech_row["total_assignment_count"] == 1,
        tech_row["total_assignment_count"],
    )

    status, body = call("DELETE", f"/positions/{tech['id']}")
    check("a position in use cannot be deleted", status == 409, status)
    check(
        "and the refusal points at deactivation",
        body.get("error", {}).get("code") == "ERR_POS_004",
        body.get("error", {}).get("code"),
    )

    status, retired = call("POST", f"/positions/{tech['id']}/deactivate")
    check(
        "deactivating retires it instead",
        status == 200 and retired["is_active"] is False,
        status,
    )

    status, profile = call("GET", f"/employees/{employee['id']}")
    check(
        "the existing assignment still resolves its title",
        profile["assignments"][0]["job_title_es"] == "tech es",
        profile["assignments"][0]["job_title_es"],
    )

    status, _ = call(
        "POST",
        f"/employees/{employee['id']}/assignments",
        {
            "department_id": first["id"],
            "job_position_id": head["id"],
            "start_date": "2025-01-01",
        },
    )
    check("a different active position still accepts new assignments", status == 201, status)

    status, body = call("DELETE", f"/positions/{head['id']}")
    check("a position with a live assignment cannot be deleted", status == 409, status)

    status, _ = call("DELETE", f"/positions/{head['id']}", headers={"X-Actor-Roles": "employee"})
    check("deleting requires a structure role", status == 403, status)

    # Cleanup. Assignments must go before their position and employee, and
    # positions before their department, because every one of those foreign keys
    # is RESTRICT. Cleanup goes through SQL rather than the API because the API
    # deliberately has no "delete an employee" endpoint yet.
    sql("DELETE FROM employee_assignments WHERE employee_id = :id", {"id": employee["id"]})
    sql("DELETE FROM employees WHERE id = :id", {"id": employee["id"]})
    sql(
        "DELETE FROM job_positions WHERE department_id = ANY(:ids)",
        {"ids": [first["id"], second["id"]]},
    )
    for department_row in (first, second):
        status, body = call("DELETE", f"/departments/{department_row['id']}")
        check(
            f"department {department_row['code']} is removed",
            status == 204,
            f"{status} {body if status != 204 else ''}",
        )

    leftover = call("GET", f"/positions?department_id={first['id']}")[1]
    check("no positions are left behind", leftover == [], leftover)

    print()
    print("FAIL" if failures else "ALL CHECKS PASSED")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
