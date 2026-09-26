"""Login, sessions, the forced password change and lockout.

**Why this file does not use the rolled-back session fixture.** Authentication
writes real rows and then reads them back across separate requests. The unit of
work for a login is not a request — the session is created in Redis, the account
row is read in a different transaction, and the guard re-reads it on the next
request. A per-test transaction that never commits is invisible to all of that,
so these tests commit and clean up explicitly.

An earlier version used the shared rolled-back fixture and failed with
"account missing or disabled": the account existed only in the fixture's
uncommitted transaction, so every request legitimately could not see it.
"""

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.v1.auth import SESSION_COOKIE
from app.core.security import hash_password
from app.db import build_engine
from app.main import app
from app.throttle import MAX_FAILED_ATTEMPTS

# Account administration is session-authenticated like everything else, so the
# tests that need an administrator sign one in. The `admin` fixture below is that
# administrator; a header would bypass the mechanism under test.
KNOWN_PASSWORD = "Str0ng!Password1"


@pytest.fixture
async def committer(settings) -> AsyncIterator[async_sessionmaker]:
    """A session factory that commits, plus a clean database around the test.

    Rows written here are what the API reads, so they have to be committed.
    Cleanup runs outside the test's own transaction for the same reason.
    """
    engine = build_engine(settings, settings.test_database_url)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def wipe() -> None:
        async with factory() as session:
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
                await session.execute(text(statement))
            await session.commit()

    await wipe()
    try:
        yield factory
    finally:
        await wipe()
        await engine.dispose()


@pytest.fixture(autouse=True)
async def api_uses_the_test_database(
    settings, committer: async_sessionmaker
) -> AsyncIterator[None]:
    """Point the application at the test database and clear the throttle counters.

    Two things this has to fix, both of which produced wrong results before:

    1. `conftest` sets `DATABASE_URL` with `setdefault`, but the container already
       exports it pointing at the development database, so the default never
       applies. The application would read `eam` while the fixtures wrote
       `eam_test` — which surfaced as "unknown username" for an account the test
       had just created.
    2. The login throttle counts per username in Redis, and the shared
       `redis_client` fixture only flushes when a test asks for it. Without this,
       failures from one test lock the account in the next, and a test expecting
       "wrong password" gets "locked out" instead.

    Overriding `db_session` here rather than per request is what makes the whole
    module consistent: the guard and the routes resolve the same session.
    """
    import redis.asyncio as redis

    from app.api.v1.deps import db_session

    async def override() -> AsyncIterator:
        async with committer() as session:
            yield session

    app.dependency_overrides[db_session] = override
    client = redis.from_url(settings.redis_url, decode_responses=True)
    await client.flushdb()
    try:
        yield
    finally:
        await client.flushdb()
        await client.aclose()
        app.dependency_overrides.pop(db_session, None)


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def grant_account(
    committer: async_sessionmaker,
    *,
    must_change_password: bool = False,
    is_active: bool = True,
) -> dict:
    """An employee with an account, committed so every request can see it."""
    employee_id = uuid4()
    user_id = uuid4()
    username = f"user{uuid4().hex[:6]}"
    async with committer() as session:
        await session.execute(
            text(
                """
                INSERT INTO employees (id, first_name, last_name, email, hire_date, status)
                VALUES (:id, 'Ana', 'Martín', :email, '2024-01-15', 'active')
                """
            ),
            {"id": employee_id, "email": f"{username}@empresa.es"},
        )
        await session.execute(
            text(
                """
                INSERT INTO users (id, employee_id, username, password_hash,
                                   must_change_password, is_active, session_epoch)
                VALUES (:id, :employee_id, :username, :password_hash, :must_change, :active, 1)
                """
            ),
            {
                "id": user_id,
                "employee_id": employee_id,
                "username": username,
                "password_hash": hash_password(KNOWN_PASSWORD),
                "must_change": must_change_password,
                "active": is_active,
            },
        )
        await session.commit()
    return {
        "user_id": str(user_id),
        "employee_id": str(employee_id),
        "username": username,
        "password": KNOWN_PASSWORD,
    }


@pytest.fixture
async def account(committer: async_sessionmaker) -> dict:
    return await grant_account(committer)


@pytest.fixture
async def pending_account(committer: async_sessionmaker) -> dict:
    return await grant_account(committer, must_change_password=True)


class SignedInAdmin:
    """A thin wrapper so a test reads as `await admin.post(...)`."""

    def __init__(self, client: AsyncClient) -> None:
        self.client = client

    async def post(self, path: str, **kwargs):
        return await self.client.post(path, **kwargs)

    async def get(self, path: str, **kwargs):
        return await self.client.get(path, **kwargs)


@pytest.fixture
async def admin(committer: async_sessionmaker) -> AsyncIterator[SignedInAdmin]:
    """A signed-in administrator, for the tests that manage someone else's account.

    Built here rather than in `tests/support/platform.py` because this module owns
    its own committing database, and mixing the two fixtures would give a test two
    different views of the same tables.
    """
    employee_id = uuid4()
    user_id = uuid4()
    username = f"admin{uuid4().hex[:8]}"

    async with committer() as session:
        await session.execute(
            text(
                """
                INSERT INTO employees (id, first_name, last_name, email, hire_date, status)
                VALUES (:id, 'Admin', 'Root', :email, '2020-01-01', 'active')
                """
            ),
            {"id": employee_id, "email": f"{username}@empresa.es"},
        )
        await session.execute(
            text(
                """
                INSERT INTO users (id, employee_id, username, password_hash,
                                   must_change_password, is_active, session_epoch, roles)
                VALUES (:id, :employee_id, :username, :password_hash, false, true, 1,
                        CAST('["admin"]' AS jsonb))
                """
            ),
            {
                "id": user_id,
                "employee_id": employee_id,
                "username": username,
                "password_hash": hash_password(KNOWN_PASSWORD),
            },
        )
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.post(
            "/api/v1/auth/login",
            json={"username": username, "password": KNOWN_PASSWORD},
        )
        assert response.status_code == 200, response.text
        yield SignedInAdmin(http)


async def read_audit(committer: async_sessionmaker, action: str) -> list[dict]:
    async with committer() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT actor_user_id, ip_address, after, reason FROM audit_log "
                    "WHERE action = :action ORDER BY id"
                ),
                {"action": action},
            )
        ).all()
    return [
        {"actor": row[0], "ip": row[1], "after": row[2], "reason": row[3]} for row in rows
    ]


async def sign_in(client: AsyncClient, username: str, password: str):
    return await client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )


# --- signing in ------------------------------------------------------------


async def test_a_correct_password_opens_a_session(client: AsyncClient, account: dict) -> None:
    response = await sign_in(client, account["username"], account["password"])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["username"] == account["username"]
    assert body["must_change_password"] is False


async def test_the_session_cookie_is_http_only_and_opaque(
    client: AsyncClient, account: dict
) -> None:
    """Browser script must not be able to read the token, and the token must not
    carry anything about the account."""
    response = await sign_in(client, account["username"], account["password"])

    header = response.headers["set-cookie"]
    assert "httponly" in header.lower()
    assert "samesite=lax" in header.lower()
    token = response.cookies[SESSION_COOKIE]
    assert account["username"] not in token
    assert account["user_id"] not in token
    # Opaque: high-entropy and URL-safe.
    assert len(token) >= 32


async def test_an_unknown_username_and_a_wrong_password_are_indistinguishable(
    client: AsyncClient, account: dict
) -> None:
    """Telling them apart would reveal which usernames exist."""
    unknown = await sign_in(client, "nobody-here", "whatever")
    wrong = await sign_in(client, account["username"], "wrong-password")

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["error"]["code"] == wrong.json()["error"]["code"]
    assert unknown.json()["error"]["message"] == wrong.json()["error"]["message"]


async def test_a_disabled_account_cannot_sign_in(
    client: AsyncClient, account: dict, admin: SignedInAdmin
) -> None:
    await admin.post(f"/api/v1/accounts/{account['user_id']}/deactivate")

    response = await sign_in(client, account["username"], account["password"])

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ERR_ACC_008"


async def test_login_is_audited_with_the_client_address(
    client: AsyncClient, committer: async_sessionmaker, account: dict
) -> None:
    await sign_in(client, account["username"], account["password"])

    entries = await read_audit(committer, "auth.login_succeeded")
    assert len(entries) == 1
    entry = entries[0]
    assert str(entry["actor"]) == account["user_id"]
    assert entry["ip"] is not None  # the test client's address
    assert entry["after"]["username"] == account["username"]


async def test_a_failed_login_is_audited(
    client: AsyncClient, committer: async_sessionmaker, account: dict
) -> None:
    """A trail of successes only cannot answer "was somebody trying to get in"."""
    await sign_in(client, account["username"], "wrong-password")

    entries = await read_audit(committer, "auth.login_failed")
    assert len(entries) == 1
    assert entries[0]["after"]["reason"] == "password_mismatch"


# --- lockout ---------------------------------------------------------------


async def test_five_failures_lock_the_account(
    client: AsyncClient, account: dict
) -> None:
    for _ in range(MAX_FAILED_ATTEMPTS):
        await sign_in(client, account["username"], "wrong-password")

    response = await sign_in(client, account["username"], account["password"])

    assert response.status_code == 423
    assert response.json()["error"]["code"] == "ERR_AUTH_003"


async def test_the_lockout_message_states_the_remaining_time(
    client: AsyncClient, account: dict
) -> None:
    for _ in range(MAX_FAILED_ATTEMPTS):
        await sign_in(client, account["username"], "wrong-password")

    response = await sign_in(client, account["username"], account["password"])

    assert "seconds remaining" in response.json()["error"]["detail"]


async def test_a_successful_login_clears_the_failure_count(
    client: AsyncClient, account: dict
) -> None:
    for _ in range(MAX_FAILED_ATTEMPTS - 1):
        await sign_in(client, account["username"], "wrong-password")

    good = await sign_in(client, account["username"], account["password"])
    assert good.status_code == 200

    # The counter is gone, so four more failures do not trip the lock.
    for _ in range(MAX_FAILED_ATTEMPTS - 1):
        await sign_in(client, account["username"], "wrong-password")
    still_open = await sign_in(client, account["username"], account["password"])
    assert still_open.status_code == 200


async def test_the_lockout_counter_ignores_username_casing(
    client: AsyncClient, account: dict
) -> None:
    """`Ana` and `ana` are the same account, so they share one counter."""
    for _ in range(MAX_FAILED_ATTEMPTS):
        await sign_in(client, account["username"].upper(), "wrong-password")

    response = await sign_in(client, account["username"], account["password"])

    assert response.status_code == 423


# --- the forced password change --------------------------------------------



async def test_login_reports_that_a_change_is_pending(
    client: AsyncClient, pending_account: dict
) -> None:
    response = await sign_in(client, pending_account["username"], pending_account["password"])

    assert response.status_code == 200
    assert response.json()["must_change_password"] is True


async def test_every_other_endpoint_is_refused_while_a_change_is_pending(
    client: AsyncClient, pending_account: dict
) -> None:
    """The gate is what makes this a requirement rather than a suggestion."""
    await sign_in(client, pending_account["username"], pending_account["password"])

    for path in ("/api/v1/departments", "/api/v1/employees/directory", "/api/v1/positions"):
        response = await client.get(path)
        assert response.status_code == 403, (
            f"{path} -> {response.status_code} {response.json().get('error', {}).get('detail')}"
        )
        assert response.json()["error"]["code"] == "ERR_SES_002", path


async def test_the_session_endpoint_still_answers_while_a_change_is_pending(
    client: AsyncClient, pending_account: dict
) -> None:
    """It is the call that explains why everything else is refused."""
    await sign_in(client, pending_account["username"], pending_account["password"])

    response = await client.get("/api/v1/auth/session")

    assert response.status_code == 200
    assert response.json()["must_change_password"] is True


async def test_the_password_can_be_changed_while_the_gate_is_up(
    client: AsyncClient, pending_account: dict
) -> None:
    await sign_in(client, pending_account["username"], pending_account["password"])

    response = await client.post(
        "/api/v1/auth/change-password",
        json={"current_password": pending_account["password"], "new_password": "N3w!Password2"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["must_change_password"] is False


async def test_the_gate_lifts_once_the_password_is_changed(
    client: AsyncClient, pending_account: dict
) -> None:
    await sign_in(client, pending_account["username"], pending_account["password"])
    await client.post(
        "/api/v1/auth/change-password",
        json={"current_password": pending_account["password"], "new_password": "N3w!Password2"},
    )

    response = await client.get("/api/v1/departments")

    assert response.status_code == 200


async def test_a_weak_new_password_is_refused_with_every_broken_rule(
    client: AsyncClient, pending_account: dict
) -> None:
    await sign_in(client, pending_account["username"], pending_account["password"])

    response = await client.post(
        "/api/v1/auth/change-password",
        json={"current_password": pending_account["password"], "new_password": "weak"},
    )

    assert response.status_code == 422
    detail = response.json()["error"]["detail"]
    for rule in ("too_short", "missing_upper", "missing_digit", "missing_special"):
        assert rule in detail


async def test_the_new_password_must_differ_from_the_current_one(
    client: AsyncClient, pending_account: dict
) -> None:
    await sign_in(client, pending_account["username"], pending_account["password"])

    response = await client.post(
        "/api/v1/auth/change-password",
        json={
            "current_password": pending_account["password"],
            "new_password": pending_account["password"],
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ERR_ACC_009"


async def test_the_wrong_current_password_is_refused(
    client: AsyncClient, pending_account: dict
) -> None:
    await sign_in(client, pending_account["username"], pending_account["password"])

    response = await client.post(
        "/api/v1/auth/change-password",
        json={"current_password": "not-it", "new_password": "N3w!Password2"},
    )

    assert response.status_code == 401


# --- session invalidation --------------------------------------------------


async def test_a_password_change_ends_other_sessions(
    client: AsyncClient, account: dict
) -> None:
    """The device making the change stays in; every other device is out."""
    from app.sessions import RedisSessionStore

    first = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    second = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    try:
        await sign_in(first, account["username"], account["password"])
        await sign_in(second, account["username"], account["password"])
        store = RedisSessionStore()
        assert await store.count_user_sessions(UUID(account["user_id"])) == 2

        changed = await first.post(
            "/api/v1/auth/change-password",
            json={"current_password": account["password"], "new_password": "N3w!Password2"},
        )
        assert changed.status_code == 200

        # The changer keeps working on this device.
        assert (await first.get("/api/v1/auth/session")).status_code == 200
        # The other device does not.
        refused = await second.get("/api/v1/auth/session")
        assert refused.status_code == 401
        assert refused.json()["error"]["code"] == "ERR_SES_001"
    finally:
        await first.aclose()
        await second.aclose()


async def test_disabling_an_account_ends_its_session(
    client: AsyncClient, account: dict, admin: SignedInAdmin
) -> None:
    """`is_active` alone would leave a session issued a minute ago still working."""
    await sign_in(client, account["username"], account["password"])
    assert (await client.get("/api/v1/auth/session")).status_code == 200

    await admin.post(f"/api/v1/accounts/{account['user_id']}/deactivate")

    refused = await client.get("/api/v1/auth/session")
    assert refused.status_code == 401
    assert refused.json()["error"]["code"] == "ERR_SES_001"


async def test_logout_ends_the_session(client: AsyncClient, account: dict) -> None:
    await sign_in(client, account["username"], account["password"])

    assert (await client.post("/api/v1/auth/logout")).status_code == 204

    assert (await client.get("/api/v1/auth/session")).status_code == 401


async def test_logout_without_a_session_is_a_no_op(client: AsyncClient) -> None:
    """Signing out when already signed out is not an error."""
    assert (await client.post("/api/v1/auth/logout")).status_code == 204


async def test_end_all_sessions_invalidates_every_device(
    client: AsyncClient, account: dict
) -> None:
    other = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    try:
        await sign_in(client, account["username"], account["password"])
        await sign_in(other, account["username"], account["password"])

        assert (await client.post("/api/v1/auth/sessions/end-all")).status_code == 204

        assert (await client.get("/api/v1/auth/session")).status_code == 401
        assert (await other.get("/api/v1/auth/session")).status_code == 401
    finally:
        await other.aclose()


async def test_a_password_reset_by_an_administrator_ends_the_session(
    client: AsyncClient, account: dict, admin: SignedInAdmin
) -> None:
    await sign_in(client, account["username"], account["password"])

    await admin.post(f"/api/v1/accounts/{account['user_id']}/reset-password")

    assert (await client.get("/api/v1/auth/session")).status_code == 401


async def test_a_request_without_a_cookie_is_refused(client: AsyncClient) -> None:
    response = await client.get("/api/v1/auth/session")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "ERR_SES_001"


async def test_an_unknown_cookie_is_refused(client: AsyncClient) -> None:
    client.cookies.set(SESSION_COOKIE, "not-a-real-session")

    response = await client.get("/api/v1/auth/session")

    assert response.status_code == 401


# --- the policy surface ----------------------------------------------------


async def test_the_password_policy_is_available_before_signing_in(
    client: AsyncClient,
) -> None:
    """The sign-in screen needs it to state the rule up front."""
    response = await client.get("/api/v1/auth/password-policy")

    assert response.status_code == 200
    assert response.json()["minimum_length"] == 8
