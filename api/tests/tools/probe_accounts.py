"""Acceptance probe for ticket 09.

    docker compose exec -T api python /app/tests/tools/probe_accounts.py

The claim worth driving over real HTTP is not "an account was created" but
"the temporary password is unrecoverable": it must not reappear in any later
response, and it must not exist in the database or the audit log either. The
database check runs through a separate connection for that reason.

Account administration is now session-authenticated like everything else, so the
probe bootstraps an administrator: one employee and account written directly,
then a real login. An earlier version sent an `X-Actor-Roles` header, which no
longer exists — the refusal it produced was `ERR_SES_001`, not a permission
error, and the probe reported it as "an account is created: 401".
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
from app.core.security import hash_password
from app.db import build_engine  # noqa: E402

BASE = "http://localhost:8000/api/v1"
ADMIN = {"Content-Type": "application/json"}

failures: list[str] = []


def check(label: str, condition: bool, observed: object = "") -> None:
    print(f"[{'ok  ' if condition else 'FAIL'}] {label}: {observed}")
    if not condition:
        failures.append(label)


class Session:
    """A caller that keeps cookies, like a browser does."""

    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )

    def call(self, method: str, path: str, body: dict | None = None, headers: dict | None = None):
        merged = dict(ADMIN)
        merged.update(headers or {})
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(BASE + path, data=data, method=method, headers=merged)
        try:
            response = self.opener.open(request, timeout=10)
            payload = response.read()
            return response.status, json.loads(payload) if payload else None
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            return exc.code, json.loads(payload) if payload else None


#: Filled in by `bootstrap_admin`; the module-level default keeps `call` usable
#: before that for the login request itself.
SESSION = Session()
call = SESSION.call


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


def assign_to(employee_id: str, *, clearance_level: str) -> None:
    """Put the employee in a department created at a given clearance.

    Through the API rather than SQL, because the inheritance under test is
    triggered by a real account creation reading real rows.
    """
    status, department = call(
        "POST",
        "/departments",
        {
            "code": f"clr{uuid4().hex[:6]}",
            "name_es": "claridad",
            "name_en": "clearance",
            "clearance_level": clearance_level,
        },
    )
    assert status == 201, f"could not create the department: {status} {department}"

    status, position = call(
        "POST",
        "/positions",
        {
            "code": f"pos{uuid4().hex[:6]}",
            "title_es": "puesto",
            "title_en": "position",
            "department_id": department["id"],
        },
    )
    assert status == 201, f"could not create the position: {status} {position}"

    status, assignment = call(
        "POST",
        f"/employees/{employee_id}/assignments",
        {
            "department_id": department["id"],
            "job_position_id": position["id"],
            "start_date": "2024-01-15",
        },
    )
    assert status == 201, f"could not assign the employee: {status} {assignment}"


def create_plain_account(username: str) -> str:
    """An employee account with no administrative role, ready to sign in.

    Created through the API by the administrator, then the temporary password is
    replaced through the real forced-change flow. That keeps the probe honest:
    the account reaches a usable state the same way a person's would, and the
    change-password path gets exercised as a side effect.
    """
    status, created = call(
        "POST", "/accounts", {"employee_id": make_employee(), "username": username}
    )
    assert status == 201, f"could not create {username}: {status} {created}"

    session = Session()
    status, _ = session.call(
        "POST",
        "/auth/login",
        {"username": username, "password": created["temporary_password"]},
    )
    assert status == 200, f"{username} could not sign in: {status}"

    status, _ = session.call(
        "POST",
        "/auth/change-password",
        {
            "current_password": created["temporary_password"],
            "new_password": "Str0ng!Password1",
        },
    )
    assert status == 200, f"{username} could not change its password: {status}"
    return username


def bootstrap_admin() -> str:
    """Create an administrator and sign in, returning the username.

    Written straight to the database because there is no other way to get the
    first administrator: the endpoint that creates accounts needs one.

    The `roles` cast is jsonb, and the password is a real Argon2id hash — the
    database enforces both, which is the point of those constraints.
    """
    employee_id = uuid4()
    user_id = uuid4()
    username = f"admin{uuid4().hex[:8]}"

    run_sql(
        """
        INSERT INTO employees (id, first_name, last_name, email, hire_date, status)
        VALUES (:id, 'Admin', 'Root', :email, '2020-01-01', 'active')
        """,
        {"id": employee_id, "email": f"{username}@empresa.es"},
    )
    run_sql(
        """
        INSERT INTO users (id, employee_id, username, password_hash, must_change_password,
                           is_active, session_epoch, roles)
        VALUES (:id, :employee_id, :username, :hash, false, true, 1,
                CAST('["admin"]' AS jsonb))
        """,
        {
            "id": user_id,
            "employee_id": employee_id,
            "username": username,
            "hash": hash_password("Str0ng!Password1"),
        },
    )

    status, _ = SESSION.call(
        "POST",
        "/auth/login",
        {"username": username, "password": "Str0ng!Password1"},
    )
    assert status == 200, f"the bootstrap administrator could not sign in: {status}"
    return username


def main() -> None:
    suffix = uuid4().hex[:6]

    # Start from a clean slate. The login throttle lives in Redis and outlives the
    # process, and leftover rows would change what this run observes — a probe
    # that only passes on a pristine machine is a probe that stops being run.
    import redis

    from app.throttle import FAILED_ATTEMPTS_KEY, normalise

    client = redis.Redis.from_url(get_settings().redis_url, decode_responses=True)
    try:
        for name in (
            f"probe{suffix}",
            f"other{suffix}",
            f"gone{suffix}",
            f"nope{suffix}",
            f"inherits{suffix}",
        ):
            client.delete(FAILED_ATTEMPTS_KEY.format(username=normalise(name)))
    finally:
        client.close()
    for statement in ("DELETE FROM audit_log", "DELETE FROM users", "DELETE FROM employees"):
        run_sql(statement, {})

    admin_username = bootstrap_admin()
    check("an administrator can sign in", bool(admin_username), admin_username[:10] + "…")

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

    # --- inherited clearance (DESIGN §10.5) --------------------------------
    inherited_employee = make_employee()
    assign_to(inherited_employee, clearance_level="high")
    status, inherited = call(
        "POST",
        "/accounts",
        {"employee_id": inherited_employee, "username": f"inherits{suffix}"},
    )
    check("an account is created for an assigned employee", status == 201, status)
    check(
        "and it starts at its department's clearance, not at the floor",
        inherited.get("clearance_level") == "high",
        inherited.get("clearance_level"),
    )
    stored = run_sql(
        "SELECT clearance_level FROM users WHERE id = :id", {"id": UUID(inherited["id"])}
    )
    check("which is what the row stores", bool(stored) and stored[0][0] == "high", stored)

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
    # A signed-in caller without the administrator role. The earlier version sent
    # an `X-Actor-Roles` header, which no longer exists: the refusal it produced
    # was about the missing session, not about the role, so it proved nothing.
    employee_session = Session()
    employee_username = create_plain_account(f"plain{suffix}")
    status, _ = employee_session.call(
        "POST", "/auth/login", {"username": employee_username, "password": "Str0ng!Password1"}
    )
    assert status == 200, f"the plain employee could not sign in: {status}"

    status, body = employee_session.call(
        "POST",
        "/accounts",
        {"employee_id": make_employee(), "username": f"nope{suffix}"},
    )
    check("creating an account needs an administrator", status == 403, status)
    check(
        "and the refusal is the permission one, not a session one",
        body.get("error", {}).get("code") == "ERR_AUTH_002",
        body.get("error", {}).get("code"),
    )

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

    # --- one path to a password change -------------------------------------
    # Ticket 09 briefly exposed the self-service change here, next to the reset
    # an administrator performs. That was a second implementation of one rule,
    # guarded differently and audited under a second action name, so it is gone:
    # changing your own password is a session operation, it lives in `auth`, and
    # `probe_auth.py` drives it end to end. This check keeps it from creeping back.
    status, _ = call(
        "POST",
        f"/accounts/{account_id}/change-password",
        {
            "current_password": reset["temporary_password"],
            "new_password": "Str0ng!Password1",
        },
    )
    check("changing a password is not an account-scoped operation", status == 404, status)

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
    # Order matters: every foreign key in this schema is RESTRICT, so referencing
    # rows go first. Positions and departments are created by the clearance checks
    # above and would otherwise accumulate one set per run.
    for statement in (
        "DELETE FROM audit_log",
        "DELETE FROM users",
        "DELETE FROM employee_assignments",
        "DELETE FROM employee_private",
        "DELETE FROM employees",
        "DELETE FROM job_positions",
        "UPDATE departments SET manager_employee_id = NULL",
        "DELETE FROM departments",
    ):
        run_sql(statement, {})
    check("no rows are left behind", run_sql("SELECT count(*) FROM employees", {})[0][0] == 0)

    print()
    print("FAIL" if failures else "ALL CHECKS PASSED")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
