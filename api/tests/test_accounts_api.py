"""Account rules over HTTP against real PostgreSQL.

The claim this file exists to check is not "an account row was created" but
"the temporary password is genuinely unrecoverable": it must not appear in the
database, in the audit log, or in any later response.
"""

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import db_session
from app.main import app

ADMIN = {"X-Actor-Roles": "admin"}


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async def override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[db_session] = override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


@pytest.fixture
async def employee(session: AsyncSession) -> dict:
    """An employee with no account, created straight in the database."""
    employee_id = uuid4()
    await session.execute(
        text(
            """
            INSERT INTO employees (id, first_name, last_name, email, hire_date, status)
            VALUES (:id, 'Ana', 'Martín', :email, '2024-01-15', 'active')
            """
        ),
        {"id": employee_id, "email": f"ana{uuid4().hex[:6]}@empresa.es"},
    )
    return {"id": str(employee_id)}


async def create_account(client: AsyncClient, employee_id: str, username: str = "amartin"):
    response = await client.post(
        "/api/v1/accounts",
        json={"employee_id": employee_id, "username": username},
        headers=ADMIN,
    )
    return response


# --- creation --------------------------------------------------------------


async def test_creating_an_account_returns_a_one_time_password(
    client: AsyncClient, employee: dict
) -> None:
    response = await create_account(client, employee["id"])

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["temporary_password"].count("-") == 2
    assert body["must_change_password"] is True
    assert body["is_active"] is True


async def test_the_plaintext_password_is_never_stored(
    client: AsyncClient, session: AsyncSession, employee: dict
) -> None:
    """The hash is stored; the value handed to the administrator is not.

    Checked against the database rather than the API, because "we do not store
    it" is a claim about storage.
    """
    body = (await create_account(client, employee["id"])).json()
    plaintext = body["temporary_password"]

    for table in ("users", "audit_log"):
        rows = await session.execute(text(f"SELECT to_jsonb(t) FROM {table} t"))
        serialised = " ".join(str(row[0]) for row in rows)
        assert plaintext not in serialised, f"the temporary password appears in {table}"

    stored_hash = await session.scalar(text("SELECT password_hash FROM users LIMIT 1"))
    assert stored_hash.startswith("$argon2id$")
    assert plaintext not in stored_hash


async def test_the_password_cannot_be_read_back_through_the_api(
    client: AsyncClient, employee: dict
) -> None:
    created = (await create_account(client, employee["id"])).json()
    account_id = created["id"]
    plaintext = created["temporary_password"]

    fetched = await client.get(f"/api/v1/accounts/{account_id}", headers=ADMIN)
    listed = await client.get("/api/v1/accounts", headers=ADMIN)

    assert plaintext not in fetched.text
    assert plaintext not in listed.text
    # The field exists in the schema but carries nothing: a read never issues a
    # password, so there is no value to hand back.
    assert fetched.json()["temporary_password"] is None
    assert all(row["temporary_password"] is None for row in listed.json())


async def test_an_employee_cannot_have_two_accounts(client: AsyncClient, employee: dict) -> None:
    await create_account(client, employee["id"])

    response = await create_account(client, employee["id"], username="other")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ERR_ACC_003"


async def test_a_username_cannot_be_reused(
    client: AsyncClient, session: AsyncSession, employee: dict
) -> None:
    await create_account(client, employee["id"], username="shared")
    other_id = uuid4()
    await session.execute(
        text(
            """
            INSERT INTO employees (id, first_name, last_name, email, hire_date, status)
            VALUES (:id, 'Luis', 'Fernández', :email, '2024-02-01', 'active')
            """
        ),
        {"id": other_id, "email": f"luis{uuid4().hex[:6]}@empresa.es"},
    )

    response = await create_account(client, str(other_id), username="SHARED")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ERR_ACC_002"


async def test_a_terminated_employee_cannot_get_an_account(
    client: AsyncClient, session: AsyncSession, employee: dict
) -> None:
    await session.execute(
        text("UPDATE employees SET status = 'terminated' WHERE id = :id"),
        {"id": UUID(employee["id"])},
    )

    response = await create_account(client, employee["id"])

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ERR_ACC_004"


async def test_an_unknown_employee_is_refused(client: AsyncClient) -> None:
    response = await create_account(client, str(uuid4()))

    assert response.status_code == 422


async def test_creating_an_account_requires_an_administrator(
    client: AsyncClient, employee: dict
) -> None:
    response = await client.post(
        "/api/v1/accounts",
        json={"employee_id": employee["id"], "username": "amartin"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ERR_AUTH_002"


async def test_reading_accounts_requires_an_administrator(client: AsyncClient) -> None:
    """Not even HR: an account list is administrative, not personnel data."""
    anonymous = await client.get("/api/v1/accounts")
    as_hr = await client.get("/api/v1/accounts", headers={"X-Actor-Roles": "hr"})

    assert anonymous.status_code == 403
    assert as_hr.status_code == 403


# --- disabling -------------------------------------------------------------


async def test_disabling_bumps_the_session_epoch(
    client: AsyncClient, employee: dict
) -> None:
    """The epoch is what makes disabling immediate.

    A session carries the epoch it was issued under, so bumping it invalidates
    every older session without the server having to find them.
    """
    created = (await create_account(client, employee["id"])).json()
    account_id = created["id"]
    assert created["session_epoch"] == 1

    response = await client.post(
        f"/api/v1/accounts/{account_id}/deactivate",
        json={"reason": "left the company"},
        headers=ADMIN,
    )

    assert response.status_code == 200
    assert response.json()["is_active"] is False
    assert response.json()["session_epoch"] == 2


async def test_disabling_records_the_reason_in_the_audit_log(
    client: AsyncClient, session: AsyncSession, employee: dict
) -> None:
    account_id = (await create_account(client, employee["id"])).json()["id"]

    await client.post(
        f"/api/v1/accounts/{account_id}/deactivate",
        json={"reason": "left the company"},
        headers=ADMIN,
    )

    row = (
        await session.execute(
            text(
                "SELECT action, reason, before, after FROM audit_log "
                "WHERE action = 'account.deactivated' ORDER BY id DESC LIMIT 1"
            )
        )
    ).first()
    assert row is not None
    assert row[0] == "account.deactivated"
    assert row[1] == "left the company"
    assert row[2] == {"is_active": True}
    assert row[3] == {"is_active": False}


async def test_disabling_twice_is_refused(client: AsyncClient, employee: dict) -> None:
    account_id = (await create_account(client, employee["id"])).json()["id"]
    await client.post(f"/api/v1/accounts/{account_id}/deactivate", headers=ADMIN)

    response = await client.post(f"/api/v1/accounts/{account_id}/deactivate", headers=ADMIN)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ERR_ACC_005"


async def test_a_disabled_account_can_be_reactivated(
    client: AsyncClient, employee: dict
) -> None:
    account_id = (await create_account(client, employee["id"])).json()["id"]
    await client.post(f"/api/v1/accounts/{account_id}/deactivate", headers=ADMIN)

    response = await client.post(f"/api/v1/accounts/{account_id}/reactivate", headers=ADMIN)

    assert response.status_code == 200
    assert response.json()["is_active"] is True


async def test_the_revocation_epoch_reaches_redis(
    client: AsyncClient, redis_client, employee: dict
) -> None:
    """The cache call is what later makes an existing cookie stop working."""
    from app.cache import SESSION_EPOCH_KEY

    account_id = (await create_account(client, employee["id"])).json()["id"]

    await client.post(f"/api/v1/accounts/{account_id}/deactivate", headers=ADMIN)

    recorded = await redis_client.get(SESSION_EPOCH_KEY.format(user_id=account_id))
    assert recorded == "2"


# --- password reset --------------------------------------------------------


async def test_reset_issues_a_new_password_and_ends_sessions(
    client: AsyncClient, employee: dict
) -> None:
    created = (await create_account(client, employee["id"])).json()
    account_id = created["id"]

    response = await client.post(
        f"/api/v1/accounts/{account_id}/reset-password",
        json={"reason": "forgotten"},
        headers=ADMIN,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["temporary_password"] != created["temporary_password"]
    assert body["must_change_password"] is True
    assert body["session_epoch"] == 2


async def test_reset_is_audited_without_the_password(
    client: AsyncClient, session: AsyncSession, employee: dict
) -> None:
    account_id = (await create_account(client, employee["id"])).json()["id"]

    body = (
        await client.post(
            f"/api/v1/accounts/{account_id}/reset-password", headers=ADMIN
        )
    ).json()

    rows = await session.execute(text("SELECT to_jsonb(t) FROM audit_log t"))
    serialised = " ".join(str(row[0]) for row in rows)
    assert body["temporary_password"] not in serialised
    assert "account.password_reset" in serialised


# --- self-service change ---------------------------------------------------


async def test_changing_a_password_requires_the_current_one(
    client: AsyncClient, employee: dict
) -> None:
    """The current password is what separates the account holder from someone
    who found an unlocked screen."""
    created = (await create_account(client, employee["id"])).json()
    account_id = created["id"]
    temporary = created["temporary_password"]

    wrong = await client.post(
        f"/api/v1/accounts/{account_id}/change-password",
        json={"current_password": "not-the-password", "new_password": "Str0ng!Password1"},
    )
    assert wrong.status_code == 422
    assert wrong.json()["error"]["code"] == "ERR_ACC_006"

    right = await client.post(
        f"/api/v1/accounts/{account_id}/change-password",
        json={"current_password": temporary, "new_password": "Str0ng!Password1"},
    )
    assert right.status_code == 200
    assert right.json()["must_change_password"] is False


async def test_a_weak_new_password_is_refused(client: AsyncClient, employee: dict) -> None:
    created = (await create_account(client, employee["id"])).json()

    response = await client.post(
        f"/api/v1/accounts/{created['id']}/change-password",
        json={"current_password": created["temporary_password"], "new_password": "weak"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ERR_ACC_006"
    # The detail names every broken rule so the UI can show one complete message.
    assert "too_short" in response.json()["error"]["detail"]


async def test_changing_a_password_ends_other_sessions(
    client: AsyncClient, employee: dict
) -> None:
    created = (await create_account(client, employee["id"])).json()

    response = await client.post(
        f"/api/v1/accounts/{created['id']}/change-password",
        json={
            "current_password": created["temporary_password"],
            "new_password": "Str0ng!Password1",
        },
    )

    assert response.json()["session_epoch"] == 2


# --- the policy surface ----------------------------------------------------


async def test_the_password_policy_is_published(client: AsyncClient) -> None:
    """The UI states the rule; it does not restate it."""
    response = await client.get("/api/v1/accounts/password-policy")

    assert response.status_code == 200
    body = response.json()
    assert body["minimum_length"] == 8
    assert set(body["required_classes"]) == {"lower", "upper", "digit", "special"}


# --- schema guards ---------------------------------------------------------


async def test_the_password_hash_constraint_rejects_a_foreign_format(
    session: AsyncSession, employee: dict
) -> None:
    """A plaintext or SHA-256 value cannot be stored even by a mistaken migration."""
    with pytest.raises(Exception) as excinfo:
        async with session.begin_nested():
            await session.execute(
                text(
                    """
                    INSERT INTO users (id, employee_id, username, password_hash,
                                       must_change_password, is_active, session_epoch)
                    VALUES (:id, :employee_id, 'plain', 'not-a-hash', true, true, 1)
                    """
                ),
                {"id": uuid4(), "employee_id": UUID(employee["id"])},
            )

    assert "argon2id" in str(excinfo.value)


async def test_unknown_fields_are_refused(client: AsyncClient, employee: dict) -> None:
    response = await client.post(
        "/api/v1/accounts",
        json={"employee_id": employee["id"], "username": "amartin", "password": "chosen-by-admin"},
        headers=ADMIN,
    )

    assert response.status_code == 422
