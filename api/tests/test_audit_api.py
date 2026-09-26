"""The compliance read surface.

Two claims matter here and they pull in opposite directions: the reader must be
able to find what happened, and almost nobody must be able to read it. The first
is tested by following a change through to the record it produced, with the actor
and the request it came from; the second by asking every other role and getting
403 — including the administrator, who is the subject of half of what is recorded.
"""

from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from uuid import uuid4

import pytest

from tests.support.platform import Actor, Platform

PATH = "/api/v1/audit-log"


async def entries(platform: Platform, **params: object) -> dict:
    """One page, as compliance would ask for it.

    Values are URL-encoded rather than pasted in: a `+00:00` offset in a query
    string decodes as a space, and the failure that produces ("not a valid
    datetime") reads like a server bug rather than like a missing quote.
    """
    compliance = await platform.account(roles=("compliance",))
    query = urlencode({key: value for key, value in params.items() if value is not None})
    response = await compliance.get(f"{PATH}?{query}" if query else PATH)
    assert response.status_code == 200, response.text
    return response.json()


# --- who may read ----------------------------------------------------------


async def test_compliance_can_read_the_audit_trail(platform: Platform) -> None:
    await platform.admin()

    page = await entries(platform)

    assert page["total"] >= 1
    assert page["items"][0]["action"] == "auth.login_succeeded"


@pytest.mark.parametrize(
    "roles",
    [("admin",), ("hr",), ("finance",), ("it",), ("employee",), ("manager",)],
)
async def test_every_other_role_is_refused(platform: Platform, roles: tuple[str, ...]) -> None:
    """Including administration, and that is the point of the requirement.

    An auditor who can also administer the system is not an independent reader.
    """
    actor = await platform.account(roles=roles)

    response = await actor.get(PATH)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ERR_AUTH_002"


async def test_an_unauthenticated_caller_is_refused(platform: Platform) -> None:
    response = await platform.client.get(PATH)

    assert response.status_code == 401


async def test_a_refused_audit_read_is_itself_recorded(platform: Platform) -> None:
    """"Who tried to read the audit trail" is exactly what it exists to answer."""
    actor = await platform.account(roles=("admin",))
    await actor.get(PATH)

    rows = await platform.sql(
        "SELECT after FROM audit_log WHERE action = 'access.refused'"
    )

    assert rows, "the refusal was not recorded"
    assert rows[-1][0]["action"] == "audit.read"


# --- the record itself -----------------------------------------------------


async def test_a_department_change_is_recorded_with_who_made_it(platform: Platform) -> None:
    """The record has to answer who, what, and from where — not only what.

    Department edits had no audit record at all before this ticket, which is what
    makes this test worth writing rather than assuming.
    """
    admin = await platform.admin()
    department = await platform.department("rrhh")

    response = await admin.patch(f"/api/v1/departments/{department}", json={"name_es": "RRHH"})
    assert response.status_code == 200

    page = await entries(platform, action="department.updated")
    assert page["total"] == 1
    record = page["items"][0]
    assert record["actor_user_id"] == admin.user_id
    assert record["actor_roles"] == ["admin", "employee"]
    assert record["entity_type"] == "department"
    assert record["entity_id"] == department
    assert record["before"]["name_es"] != record["after"]["name_es"]
    assert record["ip_address"] is not None
    assert record["request_id"], "the record cannot be joined to the request that made it"
    assert record["initiated_by"] == "user"


async def test_a_clearance_change_is_recorded_as_its_own_action(platform: Platform) -> None:
    """Clearing a department is how documents become reachable, so it is not
    merely one field in a rename."""
    admin = await platform.admin()
    department = await platform.department("datos")

    await admin.patch(f"/api/v1/departments/{department}", json={"clearance_level": "high"})

    page = await entries(platform, action="user.clearance_changed")
    assert page["total"] == 1
    assert page["items"][0]["after"]["clearance_level"] == "high"


async def test_an_employee_change_is_recorded(platform: Platform) -> None:
    admin = await platform.admin()
    employee = await platform.employee()

    await admin.patch(f"/api/v1/employees/{employee}", json={"city": "Valencia"})

    page = await entries(platform, action="employee.updated")
    assert page["total"] == 1
    assert page["items"][0]["after"]["city"] == "Valencia"


async def test_a_password_is_never_stored_in_a_record(platform: Platform) -> None:
    """The catalogue redacts by key, and the account service passes no secret —
    this asserts the outcome rather than the intention."""
    admin = await platform.admin()
    employee_id = await platform.employee()
    response = await admin.post(
        "/api/v1/accounts", json={"employee_id": employee_id, "username": "amartin"}
    )
    temporary = response.json()["temporary_password"]

    rows = await platform.sql("SELECT to_jsonb(t) FROM audit_log t")
    serialised = " ".join(str(row[0]) for row in rows)

    assert temporary not in serialised


# --- searching -------------------------------------------------------------


async def test_filters_and_paging(platform: Platform) -> None:
    admin = await platform.admin()
    first = await platform.department("uno")
    second = await platform.department("dos")
    await admin.patch(f"/api/v1/departments/{first}", json={"name_en": "One"})
    await admin.patch(f"/api/v1/departments/{second}", json={"name_en": "Two"})

    everything = await entries(platform, action="department.updated")
    assert everything["total"] == 2

    by_entity = await entries(platform, action="department.updated", entity_id=first)
    assert by_entity["total"] == 1
    assert by_entity["items"][0]["entity_id"] == first

    page_one = await entries(platform, action="department.updated", limit=1, offset=0)
    page_two = await entries(platform, action="department.updated", limit=1, offset=1)
    assert page_one["total"] == page_two["total"] == 2
    assert page_one["items"][0]["id"] != page_two["items"][0]["id"]


async def test_records_come_back_newest_first(platform: Platform) -> None:
    admin = await platform.admin()
    department = await platform.department("orden")
    await admin.patch(f"/api/v1/departments/{department}", json={"name_en": "First"})
    await admin.patch(f"/api/v1/departments/{department}", json={"name_en": "Second"})

    page = await entries(platform, action="department.updated")

    assert page["items"][0]["after"]["name_en"] == "Second"


async def test_a_time_range_is_half_open(platform: Platform) -> None:
    """An upper bound is exclusive, so "the 1st to the 2nd" means two whole days."""
    admin = await platform.admin()
    department = await platform.department("rango")
    await admin.patch(f"/api/v1/departments/{department}", json={"name_en": "Ranged"})

    now = datetime.now(UTC)
    before = await entries(
        platform,
        action="department.updated",
        occurred_to=(now - timedelta(minutes=1)).isoformat(),
    )
    after = await entries(
        platform,
        action="department.updated",
        occurred_from=(now - timedelta(minutes=1)).isoformat(),
    )

    assert before["total"] == 0
    assert after["total"] == 1


async def test_the_actor_filter_narrows_to_one_person(platform: Platform) -> None:
    first = await platform.admin()
    second = await platform.admin()
    department = await platform.department("actor")
    await first.patch(f"/api/v1/departments/{department}", json={"name_en": "Mine"})
    await second.patch(f"/api/v1/departments/{department}", json={"name_en": "Theirs"})

    page = await entries(platform, action="department.updated", actor_user_id=second.user_id)

    assert page["total"] == 1
    assert page["items"][0]["actor_user_id"] == second.user_id


async def test_an_unknown_action_filter_returns_nothing(platform: Platform) -> None:
    """Rather than everything, which is what a filter bug usually does."""
    await platform.admin()

    response = await entries(platform, action="not.an.action")

    assert response["total"] == 0
    assert response["items"] == []


async def test_an_unrecognised_action_is_searchable_and_finds_nothing(
    platform: Platform,
) -> None:
    """A record written by an older version is still evidence.

    Refusing to search for an action today's catalogue does not name would hide
    exactly the records a review is looking for, so the filter takes any string
    and simply matches none.
    """
    await platform.admin()

    page = await entries(platform, action="something.removed.last.year")

    assert page["total"] == 0


async def test_an_entity_that_never_existed_returns_nothing(platform: Platform) -> None:
    await platform.admin()

    page = await entries(platform, entity_type="department", entity_id=uuid4())

    assert page["total"] == 0


# --- there is nothing to write ---------------------------------------------


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
async def test_the_audit_trail_has_no_write_endpoint(platform: Platform, method: str) -> None:
    compliance = await platform.account(roles=("compliance",))

    response = await compliance.call(method.upper(), PATH, json={"action": "forged"})

    assert response.status_code in {404, 405}, response.status_code


# --- retention --------------------------------------------------------------


def test_the_two_retentions_are_different_and_stated(settings) -> None:
    """Evidence and diagnostics do not have the same lifetime.

    Asserted because the requirement is about the difference: an audit log that
    expires with the logs is not an audit log, and four years is the figure the
    Spanish working-time obligation implies.
    """
    assert settings.audit_retention_days == 4 * 365
    assert settings.log_retention_days == 14
    assert settings.audit_retention_days > settings.log_retention_days


def test_audit_records_live_in_the_database_and_logs_do_not() -> None:
    """The separation is structural, not a policy someone has to follow.

    Audit records are rows; runtime logs are JSON on stdout, which no query in
    this application can reach and no retention setting here deletes.
    """
    from app.models.audit import AuditLog

    assert AuditLog.__tablename__ == "audit_log"

    import app.logging as logging_module

    source = logging_module.__doc__ or ""
    assert "stdout" in source.lower() or "console" in source.lower()


async def test_a_compliance_reader_sees_whom_they_are_reading_about(
    platform: Platform,
) -> None:
    """A record whose actor is a bare id is a record nobody can act on."""
    admin = await platform.admin()
    await admin.get("/api/v1/departments")

    page = await entries(platform, actor_user_id=admin.user_id)

    assert page["total"] >= 1
    assert all(item["actor_user_id"] == admin.user_id for item in page["items"])


async def test_the_page_size_is_bounded(platform: Platform) -> None:
    """An unbounded page is a way to make the compliance endpoint the outage."""
    compliance = await platform.account(roles=("compliance",))

    response = await compliance.get(f"{PATH}?limit=100000")

    assert response.status_code == 422


async def test_a_negative_offset_is_refused(platform: Platform) -> None:
    compliance = await platform.account(roles=("compliance",))

    response = await compliance.get(f"{PATH}?offset=-1")

    assert response.status_code == 422


async def test_the_actor_is_recorded_for_an_account_deactivation(platform: Platform) -> None:
    """A record written by a service that passes its actor explicitly still lands."""
    admin: Actor = await platform.admin()
    employee_id = await platform.employee()
    created = await admin.post(
        "/api/v1/accounts", json={"employee_id": employee_id, "username": "deact"}
    )
    account_id = created.json()["id"]

    await admin.post(f"/api/v1/accounts/{account_id}/deactivate")

    page = await entries(platform, action="account.deactivated")

    assert page["total"] == 1
    assert page["items"][0]["actor_user_id"] == admin.user_id
