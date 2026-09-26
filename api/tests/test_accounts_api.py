"""Account rules over HTTP against real PostgreSQL.

The claim this file exists to check is not "an account row was created" but
"the temporary password is genuinely unrecoverable": it must not appear in the
database, in the audit log, or in any later response.

Account management is administrative, and authorisation now runs through the
session kernel, so these tests sign in a real administrator through
`tests/support/platform.py` rather than sending an actor header. That harness
commits, which is what the endpoints actually read — see its module docstring.
"""

from uuid import uuid4

import pytest

from tests.support.platform import Actor, Platform


@pytest.fixture
async def admin(platform: Platform) -> Actor:
    """A signed-in administrator, which is what every account endpoint requires."""
    return await platform.admin()


async def create_account(admin: Actor, employee_id: str, username: str = "amartin"):
    return await admin.post(
        "/api/v1/accounts",
        json={"employee_id": employee_id, "username": username},
    )


# --- creation --------------------------------------------------------------


async def test_creating_an_account_returns_a_one_time_password(
    platform: Platform, admin: Actor
) -> None:
    employee_id = await platform.employee()

    response = await create_account(admin, employee_id)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["temporary_password"].count("-") == 2
    assert body["must_change_password"] is True
    assert body["is_active"] is True


async def test_the_plaintext_password_is_never_stored(platform: Platform, admin: Actor) -> None:
    """The hash is stored; the value handed to the administrator is not.

    Checked against the database rather than the API, because "we do not store
    it" is a claim about storage.
    """
    employee_id = await platform.employee()
    created = (await create_account(admin, employee_id)).json()
    plaintext = created["temporary_password"]

    for table in ("users", "audit_log"):
        rows = await platform.sql(f"SELECT to_jsonb(t) FROM {table} t")
        serialised = " ".join(str(row[0]) for row in rows)
        assert plaintext not in serialised, f"the temporary password appears in {table}"

    stored_hash = await platform.scalar(
        "SELECT password_hash FROM users WHERE id = :id", {"id": created["id"]}
    )
    assert stored_hash.startswith("$argon2id$")
    assert plaintext not in stored_hash


async def test_the_password_cannot_be_read_back_through_the_api(
    platform: Platform, admin: Actor
) -> None:
    employee_id = await platform.employee()
    created = (await create_account(admin, employee_id)).json()
    account_id = created["id"]
    plaintext = created["temporary_password"]

    fetched = await admin.get(f"/api/v1/accounts/{account_id}")
    listed = await admin.get("/api/v1/accounts")

    assert plaintext not in fetched.text
    assert plaintext not in listed.text
    # The field exists in the schema but carries nothing: a read never issues a
    # password, so there is no value to hand back.
    assert fetched.json()["temporary_password"] is None
    assert all(row["temporary_password"] is None for row in listed.json())


async def test_an_employee_cannot_have_two_accounts(platform: Platform, admin: Actor) -> None:
    employee_id = await platform.employee()
    await create_account(admin, employee_id)

    response = await create_account(admin, employee_id, username="other")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ERR_ACC_003"


async def test_a_username_cannot_be_reused(platform: Platform, admin: Actor) -> None:
    employee_id = await platform.employee()
    await create_account(admin, employee_id, username="shared")
    other_id = await platform.employee()

    response = await create_account(admin, other_id, username="SHARED")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ERR_ACC_002"


async def test_a_terminated_employee_cannot_get_an_account(
    platform: Platform, admin: Actor
) -> None:
    employee_id = await platform.employee()
    await platform.sql(
        "UPDATE employees SET status = 'terminated' WHERE id = :id", {"id": employee_id}
    )

    response = await create_account(admin, employee_id)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ERR_ACC_004"


async def test_an_unknown_employee_is_refused(platform: Platform, admin: Actor) -> None:
    response = await create_account(admin, str(uuid4()))

    assert response.status_code == 422


async def test_creating_an_account_requires_an_administrator(platform: Platform) -> None:
    employee_id = await platform.employee()
    employee = await platform.account(roles=("employee",))

    response = await employee.post(
        "/api/v1/accounts",
        json={"employee_id": employee_id, "username": "amartin"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ERR_AUTH_002"


async def test_reading_accounts_requires_an_administrator(platform: Platform) -> None:
    """Not even HR: an account list is administrative, not personnel data."""
    anonymous = await platform.client.get("/api/v1/accounts")
    employee = await platform.account(roles=("employee",))
    as_employee = await employee.get("/api/v1/accounts")
    hr = await platform.account(roles=("hr",))
    as_hr = await hr.get("/api/v1/accounts")

    # No session at all is a 401; a session without the role is a 403.
    assert anonymous.status_code == 401
    assert anonymous.json()["error"]["code"] == "ERR_SES_001"
    assert as_employee.status_code == 403
    assert as_hr.status_code == 403


# --- disabling -------------------------------------------------------------


async def test_disabling_bumps_the_session_epoch(platform: Platform, admin: Actor) -> None:
    """The epoch is what makes disabling immediate.

    A session carries the epoch it was issued under, so bumping it invalidates
    every older session without the server having to find them.
    """
    employee_id = await platform.employee()
    created = (await create_account(admin, employee_id)).json()
    account_id = created["id"]
    assert created["session_epoch"] == 1

    response = await admin.post(
        f"/api/v1/accounts/{account_id}/deactivate",
        json={"reason": "left the company"},
    )

    assert response.status_code == 200
    assert response.json()["is_active"] is False
    assert response.json()["session_epoch"] == 2


async def test_disabling_records_the_reason_in_the_audit_log(
    platform: Platform, admin: Actor
) -> None:
    employee_id = await platform.employee()
    account_id = (await create_account(admin, employee_id)).json()["id"]

    await admin.post(
        f"/api/v1/accounts/{account_id}/deactivate",
        json={"reason": "left the company"},
    )

    rows = await platform.sql(
        "SELECT action, reason, before, after FROM audit_log "
        "WHERE action = 'account.deactivated' ORDER BY id DESC LIMIT 1"
    )
    assert rows, "the deactivation was not audited"
    row = rows[0]
    assert row[0] == "account.deactivated"
    assert row[1] == "left the company"
    assert row[2] == {"is_active": True}
    assert row[3] == {"is_active": False}


async def test_disabling_twice_is_refused(platform: Platform, admin: Actor) -> None:
    employee_id = await platform.employee()
    account_id = (await create_account(admin, employee_id)).json()["id"]
    await admin.post(f"/api/v1/accounts/{account_id}/deactivate")

    response = await admin.post(f"/api/v1/accounts/{account_id}/deactivate")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ERR_ACC_005"


async def test_a_disabled_account_can_be_reactivated(platform: Platform, admin: Actor) -> None:
    employee_id = await platform.employee()
    account_id = (await create_account(admin, employee_id)).json()["id"]
    await admin.post(f"/api/v1/accounts/{account_id}/deactivate")

    response = await admin.post(f"/api/v1/accounts/{account_id}/reactivate")

    assert response.status_code == 200
    assert response.json()["is_active"] is True


async def test_the_revocation_epoch_reaches_redis(
    platform: Platform, admin: Actor, redis_client
) -> None:
    """The cache call is what later makes an existing cookie stop working."""
    from app.cache import SESSION_EPOCH_KEY

    employee_id = await platform.employee()
    account_id = (await create_account(admin, employee_id)).json()["id"]

    await admin.post(f"/api/v1/accounts/{account_id}/deactivate")

    recorded = await redis_client.get(SESSION_EPOCH_KEY.format(user_id=account_id))
    assert recorded == "2"


# --- password reset --------------------------------------------------------


async def test_reset_issues_a_new_password_and_ends_sessions(
    platform: Platform, admin: Actor
) -> None:
    employee_id = await platform.employee()
    created = (await create_account(admin, employee_id)).json()
    account_id = created["id"]

    response = await admin.post(
        f"/api/v1/accounts/{account_id}/reset-password",
        json={"reason": "forgotten"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["temporary_password"] != created["temporary_password"]
    assert body["must_change_password"] is True
    assert body["session_epoch"] == 2


async def test_reset_is_audited_without_the_password(platform: Platform, admin: Actor) -> None:
    employee_id = await platform.employee()
    account_id = (await create_account(admin, employee_id)).json()["id"]

    body = (await admin.post(f"/api/v1/accounts/{account_id}/reset-password")).json()

    rows = await platform.sql("SELECT to_jsonb(t) FROM audit_log t")
    serialised = " ".join(str(row[0]) for row in rows)
    assert body["temporary_password"] not in serialised
    assert "account.password_reset" in serialised

# Self-service password change is not tested here. It is one operation with one
# implementation, in uth: it replaces the session cookie, so it cannot be
# expressed without a session. 	est_auth_api.py covers the wrong current
# password, the weak new password and the ending of other sessions.
# --- the policy surface ----------------------------------------------------


async def test_the_password_policy_is_published(platform: Platform) -> None:
    """The UI states the rule; it does not restate it."""
    response = await platform.client.get("/api/v1/accounts/password-policy")

    assert response.status_code == 200
    body = response.json()
    assert body["minimum_length"] == 8
    assert set(body["required_classes"]) == {"lower", "upper", "digit", "special"}


# --- schema guards ---------------------------------------------------------


async def test_the_password_hash_constraint_rejects_a_foreign_format(platform: Platform) -> None:
    """A plaintext or SHA-256 value cannot be stored even by a mistaken migration."""
    employee_id = await platform.employee()

    with pytest.raises(Exception) as excinfo:
        await platform.sql(
            """
            INSERT INTO users (id, employee_id, username, password_hash,
                               must_change_password, is_active, session_epoch)
            VALUES (:id, :employee_id, 'plain', 'not-a-hash', true, true, 1)
            """,
            {"id": uuid4(), "employee_id": employee_id},
        )

    assert "argon2id" in str(excinfo.value)


async def test_unknown_fields_are_refused(platform: Platform, admin: Actor) -> None:
    employee_id = await platform.employee()

    response = await admin.post(
        "/api/v1/accounts",
        json={"employee_id": employee_id, "username": "amartin", "password": "chosen-by-admin"},
    )

    assert response.status_code == 422
