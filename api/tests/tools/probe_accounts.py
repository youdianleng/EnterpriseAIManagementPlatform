"""Acceptance probe for ticket 09.

    docker compose exec -T api python /app/tests/tools/probe_accounts.py

The claim worth driving over real HTTP is not "an account was created" but
"the temporary password is unrecoverable": it must not reappear in any later
response, and it must not exist in the database or the audit log either. The
database check runs through a separate connection for that reason.
"""

import asyncio
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
from app.db import build_engine  # noqa: E402

BASE = "http://localhost:8000/api/v1"
ADMIN = {"X-Actor-Roles": "admin", "Content-Type": "application/json"}

failures: list[str] = []


def check(label: str, condition: bool, observed: object = "") -> None:
    print(f"[{'ok  ' if condition else 'FAIL'}] {label}: {observed}")
    if not condition:
        failures.append(label)


def call(method: str, path: str, body: dict | None = None, headers: dict | None = None):
    merged = dict(headers or ADMIN)
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method, headers=merged)
    try:
        response = urllib.request.urlopen(request, timeout=10)
        payload = response.read()
        return response.status, json.loads(payload) if payload else None
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        return exc.code, json.loads(payload) if payload else None


def run_sql(statement: str, params: dict):
    async def main():
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

    return asyncio.run(main())


def make_employee(status: str = "active") -> str:
    employee_id = uuid4()
    run_sql(
        """
        INSERT INTO employees (id, first_name, last_name, email, hire_date, status)
        VALUES (:id, 'Ana', 'Martín', :email, '2024-01-15', :status)
        """,
        {"id": employee_id, "email": f"probe{uuid4().hex[:8]}@empresa.es", "status": status},
    )
    return str(employee_id)


def main() -> None:
    suffix = uuid4().hex[:6]

    # --- creation ----------------------------------------------------------
    employee_id = make_employee()
    status, created = call(
        "POST", "/accounts", {"employee_id": employee_id, "username": f"probe{suffix}"}
    )
    check("an account is created", status == 201, status)
    check(
        "the response carries a one-time password",
        bool(created.get("temporary_password")),
        created.get("temporary_password"),
    )
    check(
        "and the account is flagged for a forced change",
        created["must_change_password"] is True,
    )
    plaintext = created["temporary_password"]
    account_id = created["id"]

    # --- unrecoverable -----------------------------------------------------
    status, fetched = call("GET", f"/accounts/{account_id}")
    check("reading the account does not return the password", plaintext not in json.dumps(fetched))
    check("nor does the list", plaintext not in json.dumps(call("GET", "/accounts")[1]))

    rows = run_sql("SELECT to_jsonb(t) FROM users t", {})
    check(
        "it is not stored in users",
        plaintext not in " ".join(str(row[0]) for row in rows),
    )
    rows = run_sql("SELECT to_jsonb(t) FROM audit_log t", {})
    check(
        "it is not stored in the audit log",
        plaintext not in " ".join(str(row[0]) for row in rows),
    )
    rows = run_sql("SELECT password_hash FROM users WHERE id = :id", {"id": UUID(account_id)})
    check(
        "what is stored is an argon2id hash",
        str(rows[0][0]).startswith("$argon2id$"),
        str(rows[0][0])[:20],
    )
    check("and it is not the plaintext", plaintext not in str(rows[0][0]))

    # --- one-to-one --------------------------------------------------------
    status, body = call(
        "POST", "/accounts", {"employee_id": employee_id, "username": f"other{suffix}"}
    )
    check("an employee cannot have a second account", status == 409, status)
    check(
        "and the refusal is catalogued",
        body.get("error", {}).get("code") == "ERR_ACC_003",
        body.get("error", {}).get("code"),
    )

    status, body = call(
        "POST", "/accounts", {"employee_id": make_employee(), "username": f"probe{suffix}"}
    )
    check("a username cannot be reused", status == 409, status)

    status, body = call(
        "POST",
        "/accounts",
        {"employee_id": make_employee("terminated"), "username": f"gone{suffix}"},
    )
    check("a terminated employee cannot get an account", status == 422, status)
    check(
        "and the refusal is catalogued",
        body.get("error", {}).get("code") == "ERR_ACC_004",
        body.get("error", {}).get("code"),
    )

    # --- authorisation -----------------------------------------------------
    status, _ = call(
        "POST",
        "/accounts",
        {"employee_id": make_employee(), "username": f"nope{suffix}"},
        headers={"X-Actor-Roles": "hr"},
    )
    check("creating an account needs an administrator", status == 403, status)

    # --- disabling ---------------------------------------------------------
    status, disabled = call(
        "POST", f"/accounts/{account_id}/deactivate", {"reason": "probe teardown"}
    )
    check("an account can be disabled", status == 200 and disabled["is_active"] is False, status)
    check(
        "disabling advances the session epoch",
        disabled["session_epoch"] == 2,
        disabled["session_epoch"],
    )
    status, body = call("POST", f"/accounts/{account_id}/deactivate")
    check("disabling twice is refused", status == 409, status)
    status, _ = call("POST", f"/accounts/{account_id}/reactivate")
    check("and it can be reactivated", status == 200, status)

    # --- reset -------------------------------------------------------------
    status, reset = call(
        "POST", f"/accounts/{account_id}/reset-password", {"reason": "forgotten"}
    )
    check("a reset issues a different password", reset["temporary_password"] != plaintext)
    check(
        "and advances the session epoch again",
        reset["session_epoch"] >= 3,
        reset["session_epoch"],
    )

    # --- self-service ------------------------------------------------------
    status, body = call(
        "POST",
        f"/accounts/{account_id}/change-password",
        {"current_password": "wrong", "new_password": "Str0ng!Password1"},
        headers={"Content-Type": "application/json"},
    )
    check("a wrong current password is refused", status == 422, status)
    check(
        "with the password-policy code",
        body.get("error", {}).get("code") == "ERR_ACC_006",
        body.get("error", {}).get("code"),
    )

    status, body = call(
        "POST",
        f"/accounts/{account_id}/change-password",
        {"current_password": reset["temporary_password"], "new_password": "weak"},
        headers={"Content-Type": "application/json"},
    )
    check("a weak new password is refused", status == 422, status)
    check(
        "naming every broken rule",
        "too_short" in (body.get("error", {}).get("detail") or ""),
        body.get("error", {}).get("detail"),
    )

    status, changed = call(
        "POST",
        f"/accounts/{account_id}/change-password",
        {
            "current_password": reset["temporary_password"],
            "new_password": "Str0ng!Password1",
        },
        headers={"Content-Type": "application/json"},
    )
    check("a strong new password is accepted", status == 200, status)
    check(
        "and the forced change is cleared",
        changed["must_change_password"] is False,
        changed["must_change_password"],
    )

    # --- the policy surface ------------------------------------------------
    status, policy = call("GET", "/accounts/password-policy")
    check(
        "the policy is published for the UI",
        status == 200 and policy["minimum_length"] == 8,
        policy,
    )

    # --- schema guard ------------------------------------------------------
    raised = False
    try:
        run_sql(
            """
            INSERT INTO users (id, employee_id, username, password_hash,
                               must_change_password, is_active, session_epoch)
            VALUES (:id, :employee_id, 'hashguard', 'not-a-hash', true, true, 1)
            """,
            {"id": uuid4(), "employee_id": UUID(employee_id)},
        )
    except Exception as exc:
        raised = "argon2id" in str(exc)
    check("the database refuses a non-Argon2id hash", raised)

    # --- cleanup -----------------------------------------------------------
    run_sql("DELETE FROM audit_log", {})
    run_sql("DELETE FROM users", {})
    run_sql("DELETE FROM employees", {})
    check("no rows are left behind", run_sql("SELECT count(*) FROM employees", {})[0][0] == 0)

    print()
    print("FAIL" if failures else "ALL CHECKS PASSED")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
