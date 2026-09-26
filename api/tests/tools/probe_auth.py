"""Acceptance probe for ticket 10.

Drives login, the forced password change, lockout and session invalidation over
real HTTP with a real cookie jar.

    docker compose exec -T api python /app/tests/tools/probe_auth.py
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
from app.throttle import MAX_FAILED_ATTEMPTS  # noqa: E402

BASE = "http://localhost:8000/api/v1"
PASSWORD = "Str0ng!Password1"

failures: list[str] = []


def check(label: str, condition: bool, observed: object = "") -> None:
    print(f"[{'ok  ' if condition else 'FAIL'}] {label}: {observed}")
    if not condition:
        failures.append(label)


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


def clear_throttle(usernames: list[str]) -> None:
    """Drop the lockout counters this probe owns.

    The counters live in Redis and outlive the process, so without this a second
    run starts already locked — the probe would fail for a reason it created
    itself on the previous run.
    """
    import redis

    from app.throttle import FAILED_ATTEMPTS_KEY, normalise

    client = redis.Redis.from_url(get_settings().redis_url, decode_responses=True)
    try:
        keys = [FAILED_ATTEMPTS_KEY.format(username=normalise(name)) for name in usernames]
        if keys:
            client.delete(*keys)
    finally:
        client.close()


class Browser:
    """A caller that keeps cookies, like a browser does."""

    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
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

    def cookie(self):
        for entry in self.jar:
            if entry.name == "eam_session":
                return entry
        return None


def make_account(*, must_change: bool = False) -> dict:
    employee_id = uuid4()
    user_id = uuid4()
    username = f"probe{uuid4().hex[:8]}"
    run_sql(
        """
        INSERT INTO employees (id, first_name, last_name, email, hire_date, status)
        VALUES (:id, 'Ana', 'Martín', :email, '2024-01-15', 'active')
        """,
        {"id": employee_id, "email": f"{username}@empresa.es"},
    )
    run_sql(
        """
        INSERT INTO users (id, employee_id, username, password_hash,
                           must_change_password, is_active, session_epoch)
        VALUES (:id, :employee_id, :username, :hash, :must_change, true, 1)
        """,
        {
            "id": user_id,
            "employee_id": employee_id,
            "username": username,
            "hash": hash_password(PASSWORD),
            "must_change": must_change,
        },
    )
    return {"user_id": str(user_id), "username": username, "password": PASSWORD}


def main() -> None:
    # Start from a clean slate: this probe deliberately trips the lockout, and a
    # previous run's counter would otherwise refuse the very first sign-in.
    clear_sql = (
        "DELETE FROM audit_log",
        "DELETE FROM users",
        "DELETE FROM employees",
    )
    for statement in clear_sql:
        run_sql(statement, {})

    # --- signing in --------------------------------------------------------
    account = make_account()
    clear_throttle([account["username"]])
    browser = Browser()

    status, body = browser.call(
        "POST", "/auth/login", {"username": account["username"], "password": account["password"]}
    )
    check("a correct password signs in", status == 200, status)

    cookie = browser.cookie()
    check("a session cookie was set", cookie is not None)
    if cookie:
        check("the cookie is httpOnly", cookie.has_nonstandard_attr("HttpOnly"))
        samesite = (cookie.get_nonstandard_attr("SameSite") or "").lower()
        check("the cookie is SameSite=Lax", samesite == "lax", samesite or "unset")
        check(
            "the token is opaque",
            account["username"] not in cookie.value,
            cookie.value[:12] + "…",
        )

    status, session = browser.call("GET", "/auth/session")
    check(
        "the session reports the account",
        status == 200 and session["username"] == account["username"],
    )

    # --- failures are generic and counted ----------------------------------
    other = Browser()
    status, unknown = other.call(
        "POST", "/auth/login", {"username": "no-such-user", "password": "x"}
    )
    status2, wrong = other.call(
        "POST", "/auth/login", {"username": account["username"], "password": "wrong"}
    )
    # Compared field by field, not by whole body: `request_id` and `timestamp`
    # differ on every response and say nothing about whether the two are
    # distinguishable to a caller.
    check(
        "an unknown username and a wrong password look the same",
        (unknown["error"]["code"], unknown["error"]["message_key"], unknown["error"]["message"])
        == (wrong["error"]["code"], wrong["error"]["message_key"], wrong["error"]["message"]),
        unknown["error"]["code"],
    )
    check("and both are 401", status == status2 == 401, (status, status2))

    # --- lockout -----------------------------------------------------------
    locked = Browser()
    for _ in range(MAX_FAILED_ATTEMPTS):
        locked.call("POST", "/auth/login", {"username": account["username"], "password": "wrong"})
    status, body = locked.call(
        "POST", "/auth/login", {"username": account["username"], "password": account["password"]}
    )
    check("a correct password during lockout is still refused", status == 423, status)
    check(
        "and the message states the remaining time",
        "seconds remaining" in (body.get("error", {}).get("detail") or ""),
        body.get("error", {}).get("detail"),
    )

    # --- the forced change -------------------------------------------------
    pending = make_account(must_change=True)
    gated = Browser()
    status, body = gated.call(
        "POST", "/auth/login", {"username": pending["username"], "password": pending["password"]}
    )
    check(
        "sign-in succeeds but flags the pending change",
        status == 200 and body["must_change_password"] is True,
    )

    status, body = gated.call("GET", "/departments")
    check("every other endpoint is refused", status == 403, status)
    check(
        "with the password-change code",
        body.get("error", {}).get("code") == "ERR_SES_002",
        body.get("error", {}).get("code"),
    )

    status, body = gated.call("GET", "/auth/session")
    check("but the session endpoint still explains why", status == 200, status)

    status, body = gated.call(
        "POST",
        "/auth/change-password",
        {"current_password": pending["password"], "new_password": "weak"},
    )
    check("a weak password is refused", status == 422, status)
    detail = body.get("error", {}).get("detail") or ""
    check(
        "naming every broken rule",
        all(
            rule in detail
            for rule in ("too_short", "missing_upper", "missing_digit", "missing_special")
        ),
        detail,
    )

    status, body = gated.call(
        "POST",
        "/auth/change-password",
        {"current_password": pending["password"], "new_password": pending["password"]},
    )
    check("reusing the current password is refused", status == 422, status)
    check(
        "with its own code",
        body.get("error", {}).get("code") == "ERR_ACC_009",
        body.get("error", {}).get("code"),
    )

    status, body = gated.call(
        "POST",
        "/auth/change-password",
        {"current_password": pending["password"], "new_password": "N3w!Password2"},
    )
    check("a strong password is accepted", status == 200, status)
    check(
        "and the flag clears",
        body["must_change_password"] is False,
        body["must_change_password"],
    )

    status, _ = gated.call("GET", "/departments")
    check("the gate lifts", status == 200, status)

    # --- another device is signed out -------------------------------------
    second = Browser()
    second.call(
        "POST", "/auth/login", {"username": pending["username"], "password": "N3w!Password2"}
    )
    check("a second device signs in", second.cookie() is not None)

    gated.call(
        "POST",
        "/auth/change-password",
        {"current_password": "N3w!Password2", "new_password": "Th1rd!Password3"},
    )
    status, _ = gated.call("GET", "/auth/session")
    check("the device that changed the password stays signed in", status == 200, status)
    status, body = second.call("GET", "/auth/session")
    check("the other device is signed out", status == 401, status)
    check(
        "with the session-expired code",
        body.get("error", {}).get("code") == "ERR_SES_001",
        body.get("error", {}).get("code"),
    )

    # --- disabling ends sessions immediately ------------------------------
    victim = make_account()
    device = Browser()
    device.call(
        "POST",
        "/auth/login",
        {"username": victim["username"], "password": victim["password"]},
    )
    check("the victim is signed in", device.cookie() is not None)

    run_sql(
        "UPDATE users SET is_active = false, session_epoch = session_epoch + 1 WHERE id = :id",
        {"id": UUID(victim["user_id"])},
    )
    status, body = device.call("GET", "/auth/session")
    check("disabling the account ends its session at once", status == 401, status)

    # --- logging out -------------------------------------------------------
    status, _ = browser.call("POST", "/auth/logout")
    check("logout succeeds", status == 204, status)
    status, _ = browser.call("GET", "/auth/session")
    check("and the session is gone", status == 401, status)
    status, _ = Browser().call("POST", "/auth/logout")
    check("logging out without a session is a no-op", status == 204, status)

    # --- the audit trail ---------------------------------------------------
    rows = run_sql(
        "SELECT action, count(*) FROM audit_log GROUP BY action ORDER BY action", {}
    )
    recorded = {row[0]: row[1] for row in rows}
    for action in (
        "auth.login_succeeded",
        "auth.login_failed",
        "auth.logout",
        "auth.password_changed",
    ):
        check(f"{action} is recorded", recorded.get(action, 0) > 0, recorded.get(action, 0))

    rows = run_sql(
        "SELECT count(*) FROM audit_log "
        "WHERE action = 'auth.login_succeeded' AND ip_address IS NULL",
        {},
    )
    check("records carry the client address", rows[0][0] == 0, rows[0][0])

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
