"""Shared plumbing for the acceptance probes.

A probe drives the *running* stack over a socket — real uvicorn, real PostgreSQL,
real Redis — which is what makes it different from a pytest test that talks to the
app in-process. Several probes had grown their own copy of this plumbing, and the
copies drifted: three of them still sent an `X-Actor-Roles` header that tickets 10
and 11 removed, so they had been reporting `401 no session cookie` as if it were a
product failure.

Everything here is about *reaching* the stack. Claims about the product belong in
the probe that makes them.
"""

import asyncio
import http.cookiejar
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db import build_engine  # noqa: E402

BASE = "http://localhost:8000/api/v1"
JSON = {"Content-Type": "application/json"}

#: The password every probe account is given once its forced change is done.
PASSWORD = "Str0ng!Password1"

failures: list[str] = []


def check(label: str, condition: bool, observed: object = "") -> None:
    """Record one claim. Truncated for display only — matching happens on the
    full value, because a cut-off message once hid the constraint name a probe
    was looking for."""
    print(f"[{'ok  ' if condition else 'FAIL'}] {label}: {str(observed)[:160]}")
    if not condition:
        failures.append(label)


def finish() -> None:
    print()
    print("FAIL" if failures else "ALL CHECKS PASSED")
    sys.exit(1 if failures else 0)


class Browser:
    """A caller that keeps cookies, the way a browser does."""

    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )
        self.username: str | None = None

    def call(
        self, method: str, path: str, body: dict | None = None, headers: dict | None = None
    ) -> tuple[int, object]:
        merged = dict(JSON)
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

    def sign_in(self, username: str, password: str = PASSWORD) -> bool:
        status, _ = self.call("POST", "/auth/login", {"username": username, "password": password})
        if status == 200:
            self.username = username
        return status == 200


def run_sql(statement: str, params: dict | None = None):
    """One committed statement, on the owner connection.

    Two deliberate choices. It uses its own connection because a claim like "the
    password is not stored" is a claim about storage, and reading it through the
    application's session would be answered from its identity map. And it uses
    the *owner* connection because a probe is not a request: the restricted role
    exists so that requests cannot rewrite the audit trail or read withheld rows
    without context, and a probe that had to work around that would be testing
    the workaround.
    """

    async def main():
        settings = get_settings()
        engine = build_engine(settings, settings.database_url)
        try:
            factory = async_sessionmaker(bind=engine, expire_on_commit=False)
            async with factory() as session:
                result = await session.execute(text(statement), params or {})
                rows = result.fetchall() if result.returns_rows else []
                await session.commit()
                return rows
        finally:
            await engine.dispose()

    return asyncio.run(main())


def make_employee(status: str = "active", roles: tuple[str, ...] = ()) -> tuple[str, str]:
    """An employee, and — when roles are given — an account that holds them.

    Employees and accounts are written directly because there is no way to obtain
    the *first* administrator through the API: the endpoint that creates accounts
    requires one. Everything after that goes through the endpoints.
    """
    employee_id = uuid4()
    username = f"probe{uuid4().hex[:8]}"
    run_sql(
        """
        INSERT INTO employees (id, first_name, last_name, email, hire_date, status)
        VALUES (:id, 'Ana', 'Martín', :email, '2024-01-15', :status)
        """,
        {"id": employee_id, "email": f"{username}@empresa.es", "status": status},
    )
    if not roles:
        return str(employee_id), ""

    run_sql(
        """
        INSERT INTO users (id, employee_id, username, password_hash, must_change_password,
                           is_active, session_epoch, roles)
        VALUES (:id, :employee_id, :username, :hash, false, true, 1, CAST(:roles AS jsonb))
        """,
        {
            "id": uuid4(),
            "employee_id": employee_id,
            "username": username,
            "hash": hash_password(PASSWORD),
            "roles": json.dumps(sorted(set(roles) | {"employee"})),
        },
    )
    return str(employee_id), username


def actor(*roles: str) -> Browser:
    """A signed-in caller holding exactly these roles."""
    _, username = make_employee(roles=tuple(roles) or ("employee",))
    browser = Browser()
    assert browser.sign_in(username), f"the {roles} probe actor could not sign in"
    return browser


def administrator() -> Browser:
    """A signed-in administrator. The first one is written, not created."""
    return actor("admin")


def wipe() -> None:
    """Remove every row a probe could have created.

    Order matters: every foreign key in this schema is RESTRICT, so referencing
    rows go first. A probe that leaves rows behind stops being run, because the
    second run no longer measures the same thing as the first.

    **This is destructive to the development database**, demo data included: the
    probes drive the running stack, and that stack is pointed at development.
    Reload the demo dataset afterwards with `python -m app.seed`, which is
    idempotent and takes a few seconds.
    """
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
        run_sql(statement)


def clear_login_counters(usernames: list[str]) -> None:
    """Drop the lockout counters a probe tripped on purpose.

    They live in Redis and outlive the process, so without this the *next* run
    starts locked out and measures the lockout instead of what it meant to check.
    """
    import redis

    from app.throttle import FAILED_ATTEMPTS_KEY, normalise

    client = redis.Redis.from_url(get_settings().redis_url, decode_responses=True)
    try:
        for username in usernames:
            client.delete(FAILED_ATTEMPTS_KEY.format(username=normalise(username)))
    finally:
        client.close()


__all__ = [
    "BASE",
    "JSON",
    "PASSWORD",
    "Browser",
    "actor",
    "administrator",
    "check",
    "clear_login_counters",
    "failures",
    "finish",
    "make_employee",
    "run_sql",
    "wipe",
]
