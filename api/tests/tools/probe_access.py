"""Acceptance probe for ticket 11.

Drives the authorization kernel over real HTTP with real sessions.

    docker compose exec -T api python /app/tests/tools/probe_access.py
"""

import asyncio
import http.cookiejar
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db import build_engine  # noqa: E402

BASE = "http://localhost:8000/api/v1"
PASSWORD = "Str0ng!Password1"

failures: list[str] = []


def check(label: str, condition: bool, observed: object = "") -> None:
    print(f"[{'ok  ' if condition else 'FAIL'}] {label}: {observed}")
    if not condition:
        failures.append(label)


async def _run(statement: str, params: dict):
    engine = build_engine(get_settings())
    try:
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as session:
            result = await session.execute(text(statement), params)
            rows = result.fetchall() if result.returns_rows else []
            await session.commit()
            return rows
    finally:
        await engine.dispose()


def sql(statement: str, params: dict | None = None):
    return asyncio.run(_run(statement, params or {}))


class Browser:
    def __init__(self) -> None:
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def call(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            BASE + path, data=data, method=method, headers={"Content-Type": "application/json"}
        )
        try:
            response = self.opener.open(request, timeout=10)
            payload = response.read()
            return response.status, json.loads(payload) if payload else None
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            return exc.code, json.loads(payload) if payload else None


def make_account(roles: list[str], *, department_id: str | None = None,
                 position_id: str | None = None) -> dict:
    employee_id = uuid4()
    user_id = uuid4()
    username = f"acc{uuid4().hex[:8]}"

    sql(
        """
        INSERT INTO employees (id, first_name, last_name, email, hire_date, status)
        VALUES (:id, 'Ada', 'Lovelace', :email, '2024-01-15', 'active')
        """,
        {"id": employee_id, "email": f"{username}@empresa.es"},
    )
    sql(
        """
        INSERT INTO users (id, employee_id, username, password_hash, must_change_password,
                           is_active, session_epoch, roles)
        VALUES (:id, :employee_id, :username, :hash, false, true, 1, CAST(:roles AS jsonb))
        """,
        {
            "id": user_id,
            "employee_id": employee_id,
            "username": username,
            "hash": hash_password(PASSWORD),
            "roles": json.dumps(roles),
        },
    )
    if department_id and position_id:
        sql(
            """
            INSERT INTO employee_assignments (id, employee_id, department_id, job_position_id,
                                              is_primary, is_part_time, start_date)
            VALUES (:id, :employee_id, :department_id, :position_id, true, false, '2024-01-15')
            """,
            {
                "id": uuid4(),
                "employee_id": employee_id,
                "department_id": department_id,
                "position_id": position_id,
            },
        )

    browser = Browser()
    status, _ = browser.call(
        "POST", "/auth/login", {"username": username, "password": PASSWORD}
    )
    assert status == 200, f"sign-in failed for {roles}: {status}"
    return {"user_id": str(user_id), "employee_id": str(employee_id), "browser": browser}


def main() -> None:
    # A clean slate: this probe creates real rows.
    for statement in (
        "DELETE FROM audit_log",
        "DELETE FROM users",
        "DELETE FROM employee_assignments",
        "DELETE FROM employees",
        "DELETE FROM job_positions",
        "UPDATE departments SET manager_employee_id = NULL",
        "DELETE FROM departments",
    ):
        sql(statement)

    # --- fixtures ----------------------------------------------------------
    admin = make_account(["admin"])
    employee = make_account(["employee"])
    outsider_role = make_account(["it"])

    # Personnel roles are not administrative ones: HR may manage people and still
    # may not manage accounts. Asserted rather than left implicit, because "which
    # role can do this" is the question the catalogue exists to answer.
    hr = make_account(["hr"])
    status, _ = hr["browser"].call("GET", "/accounts")
    check("a personnel role cannot list accounts", status == 403, status)

    status, department = admin["browser"].call(
        "POST", "/departments", {"code": "eng", "name_es": "Ingeniería", "name_en": "Engineering"}
    )
    check("an administrator can create a department", status == 201, status)
    department_id = department["id"] if status == 201 else str(uuid4())

    status, position = admin["browser"].call(
        "POST",
        "/positions",
        {
            "code": "tech",
            "title_es": "Técnica",
            "title_en": "Technician",
            "department_id": department_id,
        },
    )
    check("an administrator can create a position", status == 201, status)
    position_id = position["id"] if status == 201 else str(uuid4())

    # --- anonymous ---------------------------------------------------------
    anonymous = Browser()
    status, body = anonymous.call("GET", "/departments")
    check("an anonymous request is refused", status == 401, status)
    check(
        "with the session-invalid code",
        body.get("error", {}).get("code") == "ERR_SES_001",
        body.get("error", {}).get("code"),
    )

    # --- the role matrix over real requests --------------------------------
    status, _ = employee["browser"].call("GET", "/departments")
    check("an employee can read the organisation tree", status == 200, status)

    status, body = employee["browser"].call(
        "POST", "/departments", {"code": "nope", "name_es": "x", "name_en": "x"}
    )
    check("an employee cannot create a department", status == 403, status)
    check(
        "with the forbidden code",
        body.get("error", {}).get("code") == "ERR_AUTH_002",
        body.get("error", {}).get("code"),
    )

    status, _ = employee["browser"].call("GET", "/accounts")
    check("an employee cannot list accounts", status == 403, status)

    status, _ = admin["browser"].call("GET", "/accounts")
    check("an administrator can list accounts", status == 200, status)

    # --- the refusal is audited with the rule that fired --------------------
    # Found by content rather than taken as the first row: several refusals are
    # recorded in this run, and asserting on whichever happens to be first makes
    # the probe depend on the order the checks above are written in.
    rows = sql(
        "SELECT actor_user_id, entity_type, after, ip_address FROM audit_log "
        "WHERE action = 'access.refused'"
    )
    check("refusals are audited", len(rows) >= 3, len(rows))

    department_refusals = [
        row for row in rows if (row[2] or {}).get("action") == "department.manage"
    ]
    check(
        "the department refusal is among them",
        len(department_refusals) == 1,
        len(department_refusals),
    )
    if department_refusals:
        recorded = department_refusals[0]
        check(
            "the record names the acting user",
            str(recorded[0]) == employee["user_id"],
            str(recorded[0])[:8],
        )
        check(
            "and the reason it was refused",
            recorded[2].get("reasons") == ["role_lacks_permission"],
            recorded[2].get("reasons"),
        )
        check("and the client address", recorded[3] is not None, recorded[3])

    # --- the snapshot reflects the caller, not a default -------------------
    subject = make_account(["employee"], department_id=department_id, position_id=position_id)
    colleague = make_account(["employee"], department_id=department_id, position_id=position_id)

    status, seen_by_colleague = colleague["browser"].call(
        "GET", f"/employees/{subject['employee_id']}"
    )
    check("a colleague can read the record", status == 200, status)
    check(
        "and receives the directory projection",
        seen_by_colleague.get("visibility") == "directory",
        seen_by_colleague.get("visibility"),
    )

    status, seen_by_outsider = outsider_role["browser"].call(
        "GET", f"/employees/{subject['employee_id']}"
    )
    check(
        "someone outside the department receives the minimal projection",
        status == 200 and seen_by_outsider.get("visibility") == "minimal",
        seen_by_outsider.get("visibility"),
    )
    check(
        "which withholds the email",
        "email" not in seen_by_outsider,
        sorted(seen_by_outsider),
    )

    # --- a department move takes effect on the next request ----------------
    sql(
        "UPDATE employee_assignments SET end_date = '2025-01-01' WHERE employee_id = :id",
        {"id": UUID(colleague["employee_id"])},
    )
    status, ops_department = admin["browser"].call(
        "POST", "/departments", {"code": "ops", "name_es": "Ops", "name_en": "Ops"}
    )
    check("a second department exists for the move", status == 201, status)
    status, ops_position = admin["browser"].call(
        "POST",
        "/positions",
        {
            "code": "ops",
            "title_es": "Ops",
            "title_en": "Ops",
            "department_id": ops_department["id"],
        },
    )
    check("and a position in it", status == 201, status)
    sql(
        """
        INSERT INTO employee_assignments (id, employee_id, department_id, job_position_id,
                                          is_primary, is_part_time, start_date)
        VALUES (:id, :employee_id, :department_id, :position_id, true, false, '2025-01-01')
        """,
        {
            "id": uuid4(),
            "employee_id": UUID(colleague["employee_id"]),
            "department_id": UUID(ops_department["id"]),
            "position_id": UUID(ops_position["id"]),
        },
    )

    status, after_move = colleague["browser"].call("GET", f"/employees/{subject['employee_id']}")
    check(
        "moving the viewer away takes effect immediately, not after a TTL",
        after_move.get("visibility") == "minimal",
        after_move.get("visibility"),
    )

    # --- disabling an account ends access at once --------------------------
    sql(
        "UPDATE users SET is_active = false, session_epoch = session_epoch + 1 WHERE id = :id",
        {"id": UUID(subject["user_id"])},
    )
    status, _ = subject["browser"].call("GET", "/departments")
    check("disabling an account ends its access at once", status == 401, status)

    # --- cleanup -----------------------------------------------------------
    for statement in (
        "DELETE FROM audit_log",
        "DELETE FROM users",
        "DELETE FROM employee_assignments",
        "DELETE FROM employees",
        "DELETE FROM job_positions",
        "UPDATE departments SET manager_employee_id = NULL",
        "DELETE FROM departments",
    ):
        sql(statement)
    check("no rows are left behind", sql("SELECT count(*) FROM employees")[0][0] == 0)

    print()
    print("FAIL" if failures else "ALL CHECKS PASSED")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
