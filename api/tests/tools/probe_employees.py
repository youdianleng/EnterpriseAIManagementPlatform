"""Acceptance probe for ticket 07.

Drives the employee API over real HTTP against the running stack: the closed
field set, multi-position assignments, the effective approver, and the
visibility line between a colleague, the person and HR.

Positions are inserted straight into the database: this probe is about people,
and the catalogue has its own probe.

The actor headers are gone, so every viewer these claims are about is a real
sign-in — the administrator who does the managing, HR, the person themselves and
a colleague. Managing and reading are separate roles in the kernel (an
administrator may correct a record and is deliberately not privileged to read its
withheld fields), which is why the profile claims are read by HR.

Run inside the compose network:
    docker compose exec -T api python /app/tests/tools/probe_employees.py
"""

import json
from uuid import UUID, uuid4

from support import (
    Browser,
    actor,
    administrator,
    check,
    finish,
    make_employee,
    run_sql,
    wipe,
)


def create_position(department_id: str, code: str) -> str:
    position_id = str(uuid4())
    run_sql(
        "INSERT INTO job_positions (id, code, title_es, title_en, department_id,"
        " is_managerial, is_active) VALUES (:id, :code, :code, :code, :dept, false, true)",
        {"id": position_id, "code": code, "dept": department_id},
    )
    return position_id


def set_department_manager(department_id: str, employee_id: str) -> None:
    run_sql(
        "UPDATE departments SET manager_employee_id = :e WHERE id = :d",
        {"e": employee_id, "d": department_id},
    )


def create_employee(
    admin: Browser, email: str, first: str = "Ana", last: str = "Martín", **extra: object
) -> dict:
    status, body = admin.call(
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


def attach(
    admin: Browser, employee_id: str, department_id: str, position_id: str
) -> tuple[int, object]:
    return admin.call(
        "POST",
        f"/employees/{employee_id}/assignments",
        {
            "department_id": department_id,
            "job_position_id": position_id,
            "start_date": "2024-01-15",
        },
    )


def signed_in_employee() -> tuple[str, str, Browser]:
    """An employee holding a real account, signed in: (employee_id, email, caller).

    The account is written rather than created through `/accounts`, which needs an
    administrator and a forced password change; the session is a real one either
    way, which is what the visibility claims below are about.
    """
    employee_id, username = make_employee(roles=("employee",))
    browser = Browser()
    assert browser.sign_in(username), f"{username} could not sign in"
    email = run_sql("SELECT email FROM employees WHERE id = :id", {"id": UUID(employee_id)})[0][0]
    return employee_id, email, browser


def main() -> None:
    suffix = uuid4().hex[:6]
    admin = administrator()
    hr = actor("hr")

    _, first_department = admin.call(
        "POST", "/departments", {"code": f"p{suffix}", "name_es": "RRHH", "name_en": "HR"}
    )
    _, second_department = admin.call(
        "POST", "/departments", {"code": f"q{suffix}", "name_es": "Finanzas", "name_en": "Finance"}
    )
    check(
        "two departments exist for the multi-position case",
        bool(first_department and second_department),
    )

    position_one = create_position(first_department["id"], f"tech{suffix}")
    position_two = create_position(second_department["id"], f"analyst{suffix}")

    # --- the closed field set ---------------------------------------------
    status, body = admin.call(
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
    ana_id, ana_email, ana = signed_in_employee()
    status, _ = admin.call(
        "POST",
        "/employees",
        {
            "first_name": "Otra",
            "last_name": "Ana",
            "email": ana_email,
            "hire_date": "2024-01-15",
        },
    )
    check("a duplicate email is refused", status == 409, status)

    # --- multi-position ----------------------------------------------------
    # Attaching is the administrator's work; the assignment list it answers with
    # is the administrator's own projection, so these claims are read back by HR.
    status, _ = attach(admin, ana_id, first_department["id"], position_one)
    check("the first position is attached", status == 201, status)
    _, ana_profile = hr.call("GET", f"/employees/{ana_id}")
    check(
        "the first position becomes primary",
        ana_profile["assignments"][0]["is_primary"] is True,
        ana_profile["assignments"][0]["is_primary"],
    )

    status, _ = attach(admin, ana_id, second_department["id"], position_two)
    check("a second position in another department is attached", status == 201, status)
    _, ana_profile = hr.call("GET", f"/employees/{ana_id}")
    check(
        "the person holds two positions",
        len(ana_profile["assignments"]) == 2,
        len(ana_profile["assignments"]),
    )
    check(
        "still exactly one primary",
        sum(1 for a in ana_profile["assignments"] if a["is_primary"]) == 1,
    )
    check(
        "a client cannot ask for the primary flag",
        admin.call(
            "POST",
            f"/employees/{ana_id}/assignments",
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
    boss = create_employee(admin, f"boss{suffix}@empresa.es", "Luis", "Jefe")
    set_department_manager(first_department["id"], boss["id"])

    _, ana_profile = hr.call("GET", f"/employees/{ana_id}")
    primary = next(a for a in ana_profile["assignments"] if a["is_primary"])
    check(
        "the approver falls back to the department manager",
        primary["effective_approver_employee_id"] == boss["id"],
        primary["effective_approver_employee_id"],
    )

    # --- visibility --------------------------------------------------------
    status, _ = admin.call(
        "PUT",
        f"/employees/{ana_id}/private",
        {"address_line": "Calle Mayor 1", "employee_no": f"E-{suffix}"},
    )
    check("withheld details are stored", status == 200, status)

    _, as_hr = hr.call("GET", f"/employees/{ana_id}")
    check(
        "HR sees the withheld details",
        as_hr.get("private", {}).get("employee_no") == f"E-{suffix}",
        as_hr.get("private", {}).get("employee_no"),
    )

    _, as_self = ana.call("GET", "/employees/me")
    check(
        "the person sees their own withheld details",
        as_self.get("private", {}).get("address_line") == "Calle Mayor 1",
        as_self.get("private", {}).get("address_line"),
    )

    colleague_id, colleague_email, colleague = signed_in_employee()
    attach(admin, colleague_id, first_department["id"], position_one)

    status, seen_by_colleague = colleague.call("GET", f"/employees/{ana_id}")
    check("a colleague sees the record", status == 200, status)
    check(
        "a colleague sees the directory fields",
        seen_by_colleague.get("email") == ana_email,
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
        boss["id"] not in {row["employee_id"] for row in hr.call("GET", "/employees/directory")[1]},
    )

    _, directory = colleague.call("GET", "/employees/directory")
    by_id = {row["employee_id"]: row for row in directory}
    check(
        "the colleague's own row carries an email",
        by_id.get(colleague_id, {}).get("email") == colleague_email,
        by_id.get(colleague_id, {}).get("email"),
    )
    # `ana` shares the colleague's department, so her address is visible.
    check(
        "a row inside the viewer's department carries an email",
        by_id.get(ana_id, {}).get("email") == ana_email,
        by_id.get(ana_id, {}).get("email"),
    )

    # A person in another department: their row shows, their address does not.
    outsider = create_employee(admin, f"outsider{suffix}@empresa.es", "Otro", "Departamento")
    attach(admin, outsider["id"], second_department["id"], position_two)
    _, directory = colleague.call("GET", "/employees/directory")
    by_id = {row["employee_id"]: row for row in directory}
    check(
        "a row outside the viewer's department carries no email",
        "email" not in by_id.get(outsider["id"], {"email": None}),
        sorted(by_id.get(outsider["id"], {})),
    )

    # --- cleanup -----------------------------------------------------------
    # Order matters: the foreign keys here are RESTRICT, so the referencing rows
    # go first. Assignments before employees, employees before departments.
    # Cleanup uses SQL because the API deliberately has no "delete an employee"
    # endpoint, and a probe that leaves rows behind makes the next run's
    # expectations depend on the previous one.
    ids = [UUID(employee_id) for employee_id in (ana_id, colleague_id, boss["id"], outsider["id"])]
    run_sql("DELETE FROM employee_assignments WHERE employee_id = ANY(:ids)", {"ids": ids})
    run_sql(
        "UPDATE departments SET manager_employee_id = NULL WHERE id = ANY(:ids)",
        {"ids": [UUID(first_department["id"]), UUID(second_department["id"])]},
    )
    run_sql("DELETE FROM employees WHERE id = ANY(:ids)", {"ids": ids})
    run_sql(
        "DELETE FROM job_positions WHERE department_id = ANY(:ids)",
        {"ids": [UUID(first_department["id"]), UUID(second_department["id"])]},
    )
    for department_row in (first_department, second_department):
        status, body = admin.call("DELETE", f"/departments/{department_row['id']}")
        check(
            f"department {department_row['code']} is removed",
            status == 204,
            f"{status} {body if status != 204 else ''}",
        )

    leftover = hr.call("GET", "/employees/directory")[1]
    check("no employees are left behind", leftover == [], len(leftover or []))

    # The four signed-in callers are real employee and user rows that the
    # statements above do not reach.
    wipe()

    finish()


if __name__ == "__main__":
    main()
