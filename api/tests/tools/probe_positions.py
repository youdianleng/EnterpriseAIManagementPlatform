"""Acceptance probe for the position catalogue half of ticket 08.

    docker compose exec -T api python /app/tests/tools/probe_positions.py

The role-assignment half of ticket 08 needs a users table, which arrives with
ticket 09; this probe covers the catalogue.

Maintaining the catalogue is a signed-in administrator's work now that the actor
headers are gone. The one claim about a person's profile is read by a signed-in HR
actor, because an administrator is deliberately not privileged for reads
(`employee.visibility`), and the refusal claim is made by a signed-in employee —
the two roles the old headers used to fake.
"""

from uuid import UUID, uuid4

from support import Browser, actor, administrator, check, finish, run_sql, wipe


def department(browser: Browser, code: str) -> dict:
    status, body = browser.call(
        "POST", "/departments", {"code": code, "name_es": code, "name_en": code}
    )
    assert status == 201, f"department {code}: {status} {body}"
    return body


def position(browser: Browser, department_id: str, code: str, **extra: object):
    return browser.call(
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
    admin = administrator()
    hr = actor("hr")
    employee_actor = actor("employee")
    suffix = uuid4().hex[:6]
    first = department(admin, f"a{suffix}")
    second = department(admin, f"b{suffix}")

    status, tech = position(admin, first["id"], "tech", is_managerial=False)
    check("a position is created under its department", status == 201, status)
    check(
        "with the department echoed back",
        tech["department_code"] == f"a{suffix}",
        tech["department_code"],
    )
    check("and the managerial flag recorded", tech["is_managerial"] is False, tech["is_managerial"])

    status, head = position(admin, first["id"], "head", is_managerial=True)
    check("a managerial position can be created", status == 201, status)
    check("and the flag survives", head["is_managerial"] is True, head["is_managerial"])

    status, _ = position(admin, first["id"], "tech")
    check("a duplicate code in the same department is refused", status == 409, status)

    status, _ = position(admin, second["id"], "tech")
    check("the same code in another department is allowed", status == 201, status)

    status, _ = position(admin, str(uuid4()), "orphan")
    check("a position needs a real department", status == 422, status)

    status, listed = admin.call("GET", f"/positions?department_id={first['id']}")
    check(
        "the catalogue filters by department",
        status == 200 and len(listed) == 2,
        len(listed or []),
    )
    status, _ = admin.call(
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
    status, employee = admin.call(
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
    admin.call(
        "POST",
        f"/employees/{employee['id']}/assignments",
        {
            "department_id": first["id"],
            "job_position_id": tech["id"],
            "start_date": "2024-01-15",
        },
    )

    status, listed = admin.call("GET", f"/positions?department_id={first['id']}")
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

    status, body = admin.call("DELETE", f"/positions/{tech['id']}")
    check("a position in use cannot be deleted", status == 409, status)
    check(
        "and the refusal points at deactivation",
        body.get("error", {}).get("code") == "ERR_POS_004",
        body.get("error", {}).get("code"),
    )

    status, retired = admin.call("POST", f"/positions/{tech['id']}/deactivate")
    check(
        "deactivating retires it instead",
        status == 200 and retired["is_active"] is False,
        status,
    )

    # Read by HR: this is a claim about what a profile shows, and an
    # administrator's projection deliberately drops the assignment list.
    status, profile = hr.call("GET", f"/employees/{employee['id']}")
    check(
        "the existing assignment still resolves its title",
        profile["assignments"][0]["job_title_es"] == "tech es",
        profile["assignments"][0]["job_title_es"],
    )

    status, _ = admin.call(
        "POST",
        f"/employees/{employee['id']}/assignments",
        {
            "department_id": first["id"],
            "job_position_id": head["id"],
            "start_date": "2025-01-01",
        },
    )
    check("a different active position still accepts new assignments", status == 201, status)

    status, body = admin.call("DELETE", f"/positions/{head['id']}")
    check("a position with a live assignment cannot be deleted", status == 409, status)

    status, _ = employee_actor.call("DELETE", f"/positions/{head['id']}")
    check("deleting requires a structure role", status == 403, status)

    # Cleanup. Assignments must go before their position and employee, and
    # positions before their department, because every one of those foreign keys
    # is RESTRICT. Cleanup goes through SQL rather than the API because the API
    # deliberately has no "delete an employee" endpoint yet.
    employee_ids = [UUID(employee["id"])]
    department_ids = [UUID(first["id"]), UUID(second["id"])]
    run_sql("DELETE FROM employee_assignments WHERE employee_id = ANY(:ids)", {"ids": employee_ids})
    run_sql("DELETE FROM employees WHERE id = ANY(:ids)", {"ids": employee_ids})
    run_sql("DELETE FROM job_positions WHERE department_id = ANY(:ids)", {"ids": department_ids})
    for department_row in (first, second):
        status, body = admin.call("DELETE", f"/departments/{department_row['id']}")
        check(
            f"department {department_row['code']} is removed",
            status == 204,
            f"{status} {body if status != 204 else ''}",
        )

    leftover = admin.call("GET", f"/positions?department_id={first['id']}")[1]
    check("no positions are left behind", leftover == [], leftover)

    # The three signed-in callers are real employee and user rows that the
    # statements above do not reach.
    wipe()

    finish()


if __name__ == "__main__":
    main()
