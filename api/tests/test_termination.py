"""Ticket 18: what a termination takes effect *on*.

Ticket 17 stopped at the employee row — `termination_date` and `status` — and said
in so many words that the account half belonged to this ticket. This is that half,
driven the way the other half is: an approved change, applied by
`app.jobs.apply_personnel_changes` on its effective date, against real PostgreSQL
and real Redis. No mocks, because every claim here is about what a *row* says or
about what Redis does with a session a browser is still holding.

The checklist, and where each line is pinned:

* the account is disabled, the epoch moves and every Redis session is gone —
  `test_applying_a_termination_disables_the_account_and_ends_its_sessions`
* a disabled account with the right password is refused, in both languages, and
  the refusal says nothing beyond "this cannot sign in" —
  `test_a_disabled_account_is_refused_in_readable_words`
* nothing historical is deleted — `test_nothing_historical_is_deleted`, whose
  docstring says what each later ticket has to add to the same claim
* the leaver leaves the directory, the department headcount and the pool of
  approvers — `test_a_leaver_is_absent_from_the_directory_and_the_headcount`
* no account for a leaver, and a return is an explicit `join` —
  `test_a_rehire_is_a_join_and_does_not_reactivate_the_old_account`
* the leaver as somebody's approver, refused and reported —
  `test_a_route_that_ends_at_a_leaver_is_refused_and_named` and
  `test_the_gap_report_lists_exactly_who_hr_has_to_reassign`
* every action audited — `test_the_disable_and_the_revocation_are_audited`

The one checklist line this file does not claim is that attendance, timesheets,
leave, salary, payslips and approval history stay readable: none of those tables
exists yet. What survives today is asserted; what does not is named, with the
ticket that adds it.
"""

from contextlib import redirect_stdout
from dataclasses import dataclass
from datetime import date, timedelta
from io import StringIO
from uuid import UUID, uuid4

import pytest

from app.api.v1.auth import SESSION_COOKIE
from app.cache import SESSION_EPOCH_KEY, get_redis
from app.core.errors import ErrorCode
from app.core.messages import message_for
from app.domain.approval.models import DecisionKind
from app.domain.approval.service import ApprovalService
from app.domain.personnel.models import ChangeState
from app.domain.personnel.service import ENTITY_TYPE
from app.jobs.apply_personnel_changes import apply_due_changes, approver_gaps
from app.repositories.approval import PostgresApprovalRepository
from tests.support.platform import Actor, Platform


@dataclass(slots=True, frozen=True)
class Cast:
    """The people a termination moves through, and their places in the tree.

    `subject` is the leaver and approves for `report`; `manager` approves for both
    of them, so the subject's *own* route stays intact when they leave. That split
    is what lets the account half be tested without the approver guard firing, and
    the guard be tested on somebody else's route.
    """

    department: str
    position: str
    subject: str
    subject_actor: Actor
    manager: str
    hr: Actor
    other_hr: UUID
    report: str


@pytest.fixture
async def cast(platform: Platform) -> Cast:
    department = await platform.department("operaciones")
    position = await platform.position(department, "technician")

    # A real login and a real session: "their browser stops working" is a claim
    # about a session that exists.
    subject_actor = await platform.account(roles=("employee",))
    subject = subject_actor.employee_id
    manager = (await platform.account(roles=("employee",))).employee_id
    hr = await platform.account(roles=("hr",))
    # A distinct name, because the refusal's detail names people, and an assertion
    # that cannot tell two of them apart is not an assertion. Everybody else the
    # fixture creates is called "Ada Lovelace".
    report = await platform.employee(
        email=f"report{uuid4().hex[:8]}@empresa.es",
        first_name="Bea",
        last_name="Informe",
    )

    for employee_id in (subject, hr.employee_id):
        await platform.assign(
            employee_id, department, position, manager_employee_id=manager
        )
    # The subject approves for the report: the checklist line about a leaver who
    # was somebody's approver needs one in the fixture. One assignment, not two —
    # a second would leave the first active and make "the primary one" ambiguous.
    await platform.assign(report, department, position, manager_employee_id=subject)
    other_hr = await platform.grant_account(roles=("hr",), sign_in=False)
    return Cast(
        department=department,
        position=position,
        subject=subject,
        subject_actor=subject_actor,
        manager=manager,
        hr=hr,
        other_hr=UUID(other_hr.employee_id),
        report=report,
    )


# --- helpers ---------------------------------------------------------------


async def terminate(
    platform: Platform, cast: Cast, *, employee_id: str, effective: date
) -> str:
    """File and fully approve a termination, and hand back the change's id."""
    response = await cast.hr.post(
        "/api/v1/personnel-changes",
        json={
            "change_type": "termination",
            "employee_id": employee_id,
            "effective_date": effective.isoformat(),
            "changes": [
                {"field": "termination_date", "before": None, "after": effective.isoformat()},
                {"field": "status", "before": "active", "after": "terminated"},
            ],
        },
    )
    assert response.status_code == 201, response.text
    change_id = response.json()["id"]

    filed = await cast.hr.post(f"/api/v1/personnel-changes/{change_id}/submit")
    assert filed.status_code == 200, filed.text
    async with platform.factory() as session:
        engine = ApprovalService(PostgresApprovalRepository(session), session)
        state = await engine.state_of(ENTITY_TYPE, UUID(change_id))
        assert state is not None, "the termination was never filed"
        await engine.decide(state.id, UUID(cast.manager), DecisionKind.APPROVE, "baja")
        await engine.decide(state.id, cast.other_hr, DecisionKind.APPROVE, "registrada")
    return change_id


async def account_of(platform: Platform, employee_id: str) -> tuple:
    rows = await platform.sql(
        "SELECT id, username, is_active, session_epoch FROM users WHERE employee_id = :id",
        {"id": employee_id},
    )
    assert rows, f"employee {employee_id} has no account"
    return rows[0]


async def login(platform: Platform, username: str, password: str):
    return await platform.client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )


async def read_change(actor: Actor, change_id: str) -> dict:
    response = await actor.get(f"/api/v1/personnel-changes/{change_id}")
    assert response.status_code == 200, response.text
    return response.json()


async def department_headcount(platform: Platform, code: str) -> int:
    """The number `python -m app.seed --verify` prints beside a department.

    Read out of the command's own output rather than by copying its SQL, because
    the claim is about the report a human runs. `verify()` opens its own engine on
    the test database, which the application fixture has already pointed
    `DATABASE_URL` at.
    """
    from app.seed import verify

    with redirect_stdout(StringIO()) as printed:
        await verify()
    for line in printed.getvalue().splitlines():
        # "  <code>  <name>  <people>" with an optional "<- no manager" flag, and the
        # name may hold spaces: the code is first and the count is the first token
        # after it that is a number.
        fields = line.split()
        if not fields or fields[0] != code:
            continue
        counted = next((field for field in fields[1:] if field.isdigit()), None)
        if counted is not None:
            return int(counted)
    raise AssertionError(f"the report printed no line for {code}:\n{printed.getvalue()}")


# --- the effective date: the account goes with the employee -----------------


async def test_applying_a_termination_disables_the_account_and_ends_its_sessions(
    platform: Platform, cast: Cast
) -> None:
    """Every consequence of the word "disabled", on one effective date.

    The Redis assertion is the one a database test cannot make: the revocation is
    a value in Redis, written by the applier inside the change's transaction.
    """
    before = await account_of(platform, cast.subject)
    effective = date.today()
    change_id = await terminate(platform, cast, employee_id=cast.subject, effective=effective)
    assert await get_redis().smembers(f"session:user:{before[0]}"), (
        "the fixture's sign-in left no session index to revoke"
    )

    report = await apply_due_changes(on_date=effective)

    assert report.failed == (), report.failed
    assert report.applied == (UUID(change_id),)
    after = await account_of(platform, cast.subject)
    assert after[1] == before[1], "the account identity survives; only its access does not"
    assert after[2] is False
    assert after[3] == before[3] + 1, "the epoch has to move, or old sessions stay valid"
    assert await get_redis().get(SESSION_EPOCH_KEY.format(user_id=before[0])) == str(after[3])
    assert await platform.scalar(
        "SELECT status FROM employees WHERE id = :id", {"id": cast.subject}
    ) == "terminated"

    # The session the leaver is holding right now, on the cookie jar they signed in
    # with: the epoch moved, so the next request is refused even though the cookie
    # is still in the browser.
    held = await cast.subject_actor.get("/api/v1/auth/session")
    assert held.status_code == 401, "the leaver is still signed in until the cookie expires"
    assert held.json()["error"]["code"] == ErrorCode.SESSION_INVALID.value

    applied = await read_change(cast.hr, change_id)
    assert applied["state"] == ChangeState.APPLIED.value
    assert applied["applied_values"]["account_disabled"] is True
    assert applied["applied_values"]["account_id"] == str(before[0])


async def test_a_second_apply_of_the_same_termination_does_not_double_the_epoch(
    platform: Platform, cast: Cast
) -> None:
    """Idempotence, extended to the half ticket 18 added.

    A second pass must not bump the epoch again: an applied change is not a
    candidate, so nothing runs at all — and the `is_active` guard means that even
    a change applied twice by hand moves the epoch once.
    """
    effective = date.today()
    await terminate(platform, cast, employee_id=cast.subject, effective=effective)
    await apply_due_changes(on_date=effective)
    epoch = (await account_of(platform, cast.subject))[3]

    second = await apply_due_changes(on_date=effective)

    assert second.applied == ()
    assert second.examined == 0
    assert (await account_of(platform, cast.subject))[3] == epoch


# --- the refusal at the door -------------------------------------------------


async def test_a_disabled_account_is_refused_in_readable_words(
    platform: Platform, cast: Cast
) -> None:
    """The refusal's *shape*: catalogued, bilingual, and saying nothing extra.

    A 403 is not enough of an assertion. `ERR_ACC_008` is the code a client routes
    on, both catalogues have to carry the sentence, and `detail` has to be empty —
    "account is disabled" in a body is one fact more than "these credentials cannot
    sign in", and the reason belongs in the audit trail, which the last test here
    reads.
    """
    await terminate(platform, cast, employee_id=cast.subject, effective=date.today())
    await apply_due_changes()

    response = await login(
        platform, cast.subject_actor.username, cast.subject_actor.password
    )

    assert response.status_code == 403, response.text
    error = response.json()["error"]
    assert error["code"] == ErrorCode.ACCOUNT_DISABLED.value
    assert error["message_key"] == "errors.account_disabled"
    assert error["detail"] is None, f"the refusal explained itself: {error['detail']!r}"
    assert error["message"] == message_for("errors.account_disabled", "es")
    assert "disabled" in (message_for("errors.account_disabled", "en") or "")
    # Nothing about the account travels with the refusal: not the employee, not the
    # username, and no cookie, so a client cannot tell a disabled account from a
    # rejected one by what comes back with it.
    assert cast.subject not in response.text
    assert cast.subject_actor.username not in response.text
    assert "user_id" not in response.text
    assert SESSION_COOKIE not in response.cookies


async def test_a_wrong_password_and_a_disabled_account_read_the_same_way(
    platform: Platform, cast: Cast
) -> None:
    """The other half of "reveals nothing": the two refusals are one shape.

    A refusal carrying a distinguishing *field* — one body key the other does not
    have — answers "does this account exist" for anybody who can type. The codes
    and the statuses differ, because "this account cannot sign in" and "these
    credentials are wrong" are different answers to somebody who already holds the
    password; the bodies are what must not say which is which.
    """
    await terminate(platform, cast, employee_id=cast.subject, effective=date.today())
    await apply_due_changes()

    disabled = await login(
        platform, cast.subject_actor.username, cast.subject_actor.password
    )
    # Different usernames for the two failures, so the login throttle — which
    # counts per username — cannot turn the second into a lockout and hide what is
    # being asserted.
    wrong = await login(platform, cast.subject_actor.username, "not-the-password")
    unknown = await login(platform, f"nadie{uuid4().hex[:8]}", "not-the-password")

    assert disabled.status_code == 403
    assert wrong.status_code == 401
    assert unknown.status_code == 401
    # The envelope's keys are the same for all three, and `detail` is empty in
    # every one: the reasons live in the audit trail, not in the body.
    assert sorted(disabled.json()["error"]) == sorted(wrong.json()["error"])
    assert sorted(unknown.json()["error"]) == sorted(wrong.json()["error"])
    for response in (disabled, wrong, unknown):
        assert response.json()["error"]["detail"] is None, response.text
        assert "user_id" not in response.text
    assert wrong.json()["error"]["code"] == unknown.json()["error"]["code"]
    assert wrong.json()["error"]["message"] == unknown.json()["error"]["message"]


# --- what survives ----------------------------------------------------------


async def test_nothing_historical_is_deleted(platform: Platform, cast: Cast) -> None:
    """Everything that exists today survives the leaver, row for row.

    **Asserted now**: the employee row, their assignments *including the ones that
    have ended*, the personnel change that terminated them and what it applied, the
    notifications the approval raised, the audit records, and the login itself —
    which survives as a disabled row rather than being deleted, because the trail
    points at it.

    **What each later ticket must add to this same claim**, because a table that
    does not exist cannot be asserted about:

    * attendance events and daily snapshots — ticket 21 (retention: `docs/DESIGN.md`
      §8.1, four years)
    * leave requests — ticket 22
    * timesheets — tickets 23–25
    * salary records — ticket 43
    * payslip batches and payslips — tickets 44–46
    * the approvals of those documents — 21–47, on the same engine read here

    Each adds rows to this test rather than a rule: "the leaver's records are still
    there" is one assertion every time, written against what existed *before* the
    termination.
    """
    admin = await platform.admin()
    # An assignment that already ended: the retention rules are about history, and
    # an ended row is the shape history has here.
    ended = await admin.post(
        f"/api/v1/employees/{cast.subject}/assignments",
        json={
            "department_id": cast.department,
            "job_position_id": cast.position,
            "start_date": "2023-01-01",
            "end_date": "2023-12-31",
        },
    )
    assert ended.status_code == 201, ended.text
    change_id = await terminate(platform, cast, employee_id=cast.subject, effective=date.today())
    account = await account_of(platform, cast.subject)
    before = {
        "assignments": await platform.scalar(
            "SELECT count(*) FROM employee_assignments WHERE employee_id = :id",
            {"id": cast.subject},
        ),
        "notifications": await platform.scalar(
            "SELECT count(*) FROM notifications WHERE entity_id = :id", {"id": change_id}
        ),
        "trail": await platform.scalar(
            "SELECT count(*) FROM audit_log WHERE entity_id = :id", {"id": cast.subject}
        ),
    }

    await apply_due_changes()

    assert await platform.scalar(
        "SELECT count(*) FROM employees WHERE id = :id", {"id": cast.subject}
    ) == 1
    assert await platform.scalar(
        "SELECT count(*) FROM users WHERE id = :id", {"id": account[0]}
    ) == 1, "the login identity was deleted instead of disabled"
    assert await platform.scalar(
        "SELECT count(*) FROM users WHERE id = :id AND is_active", {"id": account[0]}
    ) == 0
    assert await platform.scalar(
        "SELECT count(*) FROM employee_assignments WHERE employee_id = :id",
        {"id": cast.subject},
    ) == before["assignments"], "an assignment was tidied away"
    assert await platform.scalar(
        """
        SELECT count(*) FROM employee_assignments
        WHERE employee_id = :id AND end_date IS NOT NULL
        """,
        {"id": cast.subject},
    ) == 1, "the ended assignment is exactly what retention is about"
    assert await platform.scalar(
        "SELECT count(*) FROM personnel_changes WHERE id = :id", {"id": change_id}
    ) == 1
    assert await platform.scalar(
        "SELECT count(*) FROM notifications WHERE entity_id = :id", {"id": change_id}
    ) == before["notifications"] > 0, "the approval's notifications went with the leaver"
    assert await platform.scalar(
        "SELECT count(*) FROM audit_log WHERE entity_id = :id", {"id": cast.subject}
    ) > before["trail"], "the termination wrote nothing to the trail"
    assert await platform.scalar(
        "SELECT status FROM employees WHERE id = :id", {"id": cast.subject}
    ) == "terminated"


# --- out of the lists -------------------------------------------------------


async def test_a_leaver_is_absent_from_the_directory_and_the_headcount(
    platform: Platform, cast: Cast
) -> None:
    """The lists of people, and the leaver's absence from each.

    The directory endpoint and the headcount `--verify` prints are asserted
    separately because they are two different queries: a contact list, and a report
    a human reads. Both exclude the leaver, and the count drops even though the
    leaver's assignment is still active — the state a termination leaves behind,
    and the reason the exclusion cannot be left to "their position will have
    ended".
    """
    before = await department_headcount(platform, "operaciones")
    await terminate(platform, cast, employee_id=cast.subject, effective=date.today())
    everyone = await cast.hr.get(
        "/api/v1/employees/directory",
        params={"include_terminated": True, "limit": 500},
    )
    assert cast.subject in {row["employee_id"] for row in everyone.json()}, (
        "the control: the administrative view is where a leaver is looked up"
    )

    await apply_due_changes()

    listed = await cast.hr.get("/api/v1/employees/directory", params={"limit": 500})
    assert listed.status_code == 200, listed.text
    assert cast.subject not in {row["employee_id"] for row in listed.json()}
    assert await department_headcount(platform, "operaciones") == before - 1, (
        "the department count still includes the leaver"
    )
    assert await platform.scalar(
        """
        SELECT count(*) FROM employee_assignments
        WHERE employee_id = :id AND end_date IS NULL
        """,
        {"id": cast.subject},
    ) == 1, "the position is still active, which is why the exclusion is explicit"


# --- the leaver as an approver ---------------------------------------------


async def test_a_route_that_ends_at_a_leaver_is_refused_and_named(
    platform: Platform, cast: Cast
) -> None:
    """Refused at submission, naming the people HR has to fix.

    The leaver approves for `report`; the leaver's own route is untouched by their
    termination, so what refuses this document is somebody else's dead route. That
    is the behaviour chosen: the refusal covers the company's routes, because the
    fix is one act by HR and needs the whole list.
    """
    await terminate(platform, cast, employee_id=cast.subject, effective=date.today())
    await apply_due_changes()
    draft = await cast.hr.post(
        "/api/v1/personnel-changes",
        json={
            "change_type": "promotion",
            "employee_id": cast.report,
            "effective_date": (date.today() + timedelta(days=7)).isoformat(),
            "changes": [
                {"field": "job_position_id", "before": cast.position, "after": cast.position}
            ],
        },
    )
    assert draft.status_code == 201, draft.text
    change_id = draft.json()["id"]

    refused = await cast.hr.post(f"/api/v1/personnel-changes/{change_id}/submit")

    assert refused.status_code == 409, refused.text
    error = refused.json()["error"]
    assert error["code"] == ErrorCode.PERSONNEL_APPROVER_TERMINATED.value
    assert error["message_key"] == "errors.personnel_approver_terminated"
    # Named: the refusal is a work item, not a status. Both people, and where the
    # approver was configured — changing the position and changing the department
    # are different acts.
    assert "Bea Informe" in error["detail"], error["detail"]
    assert "->" in error["detail"]
    assert "(position)" in error["detail"], error["detail"]
    assert "aprobarlo" in error["message"]
    # Nothing was filed: the engine was never asked, so no request is sitting in a
    # queue nobody can decide.
    assert (await read_change(cast.hr, change_id))["state"] == ChangeState.DRAFT.value
    assert await platform.scalar(
        "SELECT count(*) FROM approval_requests WHERE entity_id = :id", {"id": change_id}
    ) == 0


async def test_the_gap_report_lists_exactly_who_hr_has_to_reassign(
    platform: Platform, cast: Cast
) -> None:
    """"The system refused and nobody knows who to fix" is not an outcome.

    Both places an approver can be configured are exercised: `report` names the
    leaver on their position, `inheritor` inherits it from the department's manager.
    """
    inheritor = await platform.employee(email=f"hereda{uuid4().hex[:8]}@empresa.es")
    # Assigned with no manager of their own, so their route is the department's
    # manager — the second of the two places an approver can be configured.
    await platform.assign(inheritor, cast.department, cast.position)
    await platform.sql(
        "UPDATE departments SET manager_employee_id = :manager WHERE id = :id",
        {"manager": cast.subject, "id": cast.department},
    )
    assert await approver_gaps() == [], "a live approver is not a gap"
    await terminate(platform, cast, employee_id=cast.subject, effective=date.today())

    await apply_due_changes()

    gaps = {gap.employee_id: gap for gap in await approver_gaps()}
    assert set(gaps) == {UUID(cast.report), UUID(inheritor)}, (
        "the report is not exactly the people whose route now ends at the leaver: "
        f"{sorted(str(employee_id) for employee_id in gaps)}"
    )
    assert gaps[UUID(cast.report)].named_on_position is True
    assert gaps[UUID(inheritor)].named_on_position is False
    assert gaps[UUID(inheritor)].department_code == "operaciones"
    assert gaps[UUID(cast.report)].approver_employee_id == UUID(cast.subject)
    assert gaps[UUID(cast.report)].approver_name == gaps[UUID(inheritor)].approver_name
    # The controls: somebody with a live approver is not in the report, and the
    # leaver does not appear as a person who needs one.
    assert UUID(cast.manager) not in gaps
    assert UUID(cast.subject) not in gaps


async def test_reassigning_the_approver_clears_the_gap_and_unblocks_filing(
    platform: Platform, cast: Cast
) -> None:
    """The way out is the reassignment HR was told to make, and it works.

    Without this, "refused until HR reassigns" would be a claim nobody had ever
    completed — and that is the whole difference between a refusal and a dead end.
    """
    await terminate(platform, cast, employee_id=cast.subject, effective=date.today())
    await apply_due_changes()
    assignment = await platform.scalar(
        """
        SELECT id FROM employee_assignments
        WHERE employee_id = :id AND end_date IS NULL
        """,
        {"id": cast.report},
    )
    assert await approver_gaps() != []

    admin = await platform.admin()
    reassigned = await admin.post(
        f"/api/v1/employees/{cast.report}/assignments",
        json={
            "department_id": cast.department,
            "job_position_id": cast.position,
            "start_date": date.today().isoformat(),
            "manager_employee_id": cast.manager,
        },
    )
    assert reassigned.status_code == 201, reassigned.text
    ended = await admin.delete(
        f"/api/v1/employees/{cast.report}/assignments/{assignment}"
    )
    assert ended.status_code == 200, ended.text
    draft = await cast.hr.post(
        "/api/v1/personnel-changes",
        json={
            "change_type": "promotion",
            "employee_id": cast.report,
            "effective_date": (date.today() + timedelta(days=7)).isoformat(),
            "changes": [
                {"field": "job_position_id", "before": cast.position, "after": cast.position}
            ],
        },
    )
    change_id = draft.json()["id"]

    filed = await cast.hr.post(f"/api/v1/personnel-changes/{change_id}/submit")

    assert filed.status_code == 200, filed.text
    assert (await read_change(cast.hr, change_id))["state"] == ChangeState.IN_APPROVAL.value
    assert await approver_gaps() == []


# --- no second account, and no silent return --------------------------------


async def test_a_terminated_employee_cannot_be_given_an_account(
    platform: Platform, cast: Cast
) -> None:
    """`ERR_ACC_004` covers it, checked on the flow that creates one.

    Two leavers, because there are two ways to be one. The subject still has their
    disabled login on file, so this is that guard; `accountless` never had one, and
    is refused on the employment status alone. Both are `ERR_ACC_004` — the code
    means "this person may not hold an account", which is the ticket's claim.
    """
    admin = await platform.admin()
    accountless = await platform.employee(email=f"sin{uuid4().hex[:8]}@empresa.es")
    await platform.assign(accountless, cast.department, cast.position)
    await terminate(platform, cast, employee_id=cast.subject, effective=date.today())
    await apply_due_changes()

    reused = await admin.post(
        "/api/v1/accounts",
        json={"employee_id": cast.subject, "username": f"x{uuid4().hex[:8]}"},
    )
    assert reused.status_code == 422, reused.text
    assert reused.json()["error"]["code"] == ErrorCode.ACCOUNT_EMPLOYEE_NOT_ACTIVE.value

    await platform.sql(
        "UPDATE employees SET status = 'terminated' WHERE id = :id", {"id": accountless}
    )
    fresh = await admin.post(
        "/api/v1/accounts",
        json={"employee_id": accountless, "username": f"y{uuid4().hex[:8]}"},
    )
    assert fresh.status_code == 422, fresh.text
    assert fresh.json()["error"]["code"] == ErrorCode.ACCOUNT_EMPLOYEE_NOT_ACTIVE.value
    assert "no longer employed" in (message_for("errors.account_employee_not_active", "en") or "")


async def test_a_rehire_is_a_join_and_does_not_reactivate_the_old_account(
    platform: Platform, cast: Cast
) -> None:
    """A return is an explicit act, and it does not put the old login back.

    Three refusals and one positive, because "the leaver cannot be given an account
    again" has more than one door:

    * the *join* for a return under the address they left with is refused, so a
      re-hire cannot come in through the document that creates people;
    * the leaver's own login cannot be switched back on — re-enabling it would be
      the act `create` refuses, by another door;
    * a `join` that does create somebody new leaves every account alone: no login
      for the new person, and the leaver's still disabled.

    An administrator who wants a returning colleague to have access creates it
    deliberately, as for anybody else, once the person exists.
    """
    old_account = await account_of(platform, cast.subject)
    leaving_email = await platform.scalar(
        "SELECT email FROM employees WHERE id = :id", {"id": cast.subject}
    )
    await terminate(platform, cast, employee_id=cast.subject, effective=date.today())
    await apply_due_changes()
    assert (await account_of(platform, cast.subject))[2] is False
    admin = await platform.admin()

    # Door one: the address the leaver left with is still theirs, so a returning
    # colleague cannot be hired under it — the join names a taken address, and the
    # refusal says which.
    returning = await cast.hr.post(
        "/api/v1/personnel-changes",
        json={
            "change_type": "join",
            "effective_date": date.today().isoformat(),
            "changes": [
                {"field": "first_name", "before": None, "after": "Vuelve"},
                {"field": "last_name", "before": None, "after": "Incorporación"},
                {"field": "email", "before": None, "after": leaving_email},
                {"field": "hire_date", "before": None, "after": date.today().isoformat()},
                {"field": "department_id", "before": None, "after": cast.department},
                {"field": "job_position_id", "before": None, "after": cast.position},
            ],
        },
    )
    assert returning.status_code == 422, returning.text
    assert "already in use" in returning.json()["error"]["detail"]

    # Door two: switching the leaver's own login back on.
    reactivated = await admin.post(f"/api/v1/accounts/{old_account[0]}/reactivate")
    assert reactivated.status_code == 422, reactivated.text
    assert reactivated.json()["error"]["code"] == ErrorCode.ACCOUNT_EMPLOYEE_NOT_ACTIVE.value
    assert "a join change" in reactivated.json()["error"]["detail"]
    assert (await account_of(platform, cast.subject))[2] is False, (
        "a refused reactivation left the login enabled"
    )

    # The explicit act: a `join` change for somebody genuinely new, approved like
    # any other, which creates the person and touches nobody's account. The
    # leaver's own reports are reassigned first, because a company with a dead
    # route refuses *every* document — the guard from the earlier test — and this
    # test is about the account half, not about the refusal.
    report_assignment = await platform.scalar(
        """
        SELECT id FROM employee_assignments
        WHERE employee_id = :id AND end_date IS NULL
        """,
        {"id": cast.report},
    )
    await admin.post(
        f"/api/v1/employees/{cast.report}/assignments",
        json={
            "department_id": cast.department,
            "job_position_id": cast.position,
            "start_date": date.today().isoformat(),
            "manager_employee_id": cast.manager,
        },
    )
    assert (
        await admin.delete(
            f"/api/v1/employees/{cast.report}/assignments/{report_assignment}"
        )
    ).status_code == 200
    email = f"alta{uuid4().hex[:8]}@empresa.es"
    effective = date.today()
    joined = await cast.hr.post(
        "/api/v1/personnel-changes",
        json={
            "change_type": "join",
            "effective_date": effective.isoformat(),
            "changes": [
                {"field": "first_name", "before": None, "after": "Nueva"},
                {"field": "last_name", "before": None, "after": "Incorporación"},
                {"field": "email", "before": None, "after": email},
                {"field": "hire_date", "before": None, "after": effective.isoformat()},
                {"field": "department_id", "before": None, "after": cast.department},
                {"field": "job_position_id", "before": None, "after": cast.position},
            ],
        },
    )
    assert joined.status_code == 201, joined.text
    change_id = joined.json()["id"]
    filed = await cast.hr.post(f"/api/v1/personnel-changes/{change_id}/submit")
    assert filed.status_code == 200, filed.text
    async with platform.factory() as session:
        engine = ApprovalService(PostgresApprovalRepository(session), session)
        state = await engine.state_of(ENTITY_TYPE, UUID(change_id))
        await engine.decide(state.id, UUID(cast.manager), DecisionKind.APPROVE, "alta")
        await engine.decide(state.id, cast.other_hr, DecisionKind.APPROVE, "registrada")

    report = await apply_due_changes()

    assert report.failed == (), report.failed
    new_employee = await platform.scalar(
        "SELECT id FROM employees WHERE email = :email", {"email": email}
    )
    assert new_employee is not None and str(new_employee) != cast.subject
    rehired_account = await account_of(platform, cast.subject)
    assert (rehired_account[0], rehired_account[1], rehired_account[2]) == (
        old_account[0],
        old_account[1],
        False,
    ), "the re-hire reactivated the leaver's login"
    assert rehired_account[3] == old_account[3] + 1, (
        "the epoch moved once, when the termination was applied, and not again"
    )
    assert await platform.scalar(
        "SELECT count(*) FROM users WHERE employee_id = :id", {"id": new_employee}
    ) == 0, "a join issued a login nobody asked for"
    assert await platform.scalar(
        "SELECT status FROM employees WHERE id = :id", {"id": cast.subject}
    ) == "terminated", "the leaver's record was rewritten by the re-hire"


# --- the trail --------------------------------------------------------------


async def test_the_disable_and_the_revocation_are_audited(
    platform: Platform, cast: Cast
) -> None:
    """Both halves of the termination, with the reason on each.

    Two records, because two things happened to two entities: the personnel change
    was applied, and the account was disabled with its sessions ended. The second
    carries the epoch, so "when did the old sessions stop working" is answerable
    from the trail rather than only from Redis, which forgets.
    """
    effective = date.today()
    account = await account_of(platform, cast.subject)
    change_id = await terminate(platform, cast, employee_id=cast.subject, effective=effective)

    await apply_due_changes(on_date=effective)

    applied = await platform.sql(
        """
        SELECT before, after, reason, initiated_by, actor_user_id FROM audit_log
        WHERE action = 'personnel_change.applied' AND entity_id = :id
        """,
        {"id": change_id},
    )
    assert len(applied) == 1
    before, after, reason, initiated_by, actor_user_id = applied[0]
    assert before == {"termination_date": None, "status": "active"}
    assert after["status"] == "terminated"
    assert after["account_disabled"] is True
    assert after["account_id"] == str(account[0])
    assert reason == f"termination effective {effective}"
    assert initiated_by == "system", "nobody pressed anything: the job applied a document"
    assert actor_user_id is None

    trail = await platform.sql(
        """
        SELECT before, after, reason, initiated_by, actor_user_id, entity_id, entity_type
        FROM audit_log WHERE action = 'account.deactivated'
        """
    )
    assert len(trail) == 1, f"expected one deactivation record, found {len(trail)}"
    before, after, reason, initiated_by, actor_user_id, entity_id, entity_type = trail[0]
    assert (entity_type, entity_id) == ("user", account[0])
    assert before == {"is_active": True}
    assert after["is_active"] is False
    assert after["session_epoch"] == account[3] + 1
    assert after["sessions_revoked"] is True
    assert after["employee_id"] == cast.subject
    assert after["personnel_change_id"] == change_id
    assert reason == f"termination effective {effective}"
    assert initiated_by == "system"
    assert actor_user_id is None, "the applier is not a user, and says so rather than guessing"

    # A refused sign-in after the fact is recorded with its reason, which is what
    # makes the refusal above answerable from the trail rather than only from the
    # response body it deliberately says nothing in.
    await login(platform, cast.subject_actor.username, cast.subject_actor.password)
    assert await platform.scalar(
        """
        SELECT count(*) FROM audit_log
        WHERE action = 'auth.login_failed'
          AND after ->> 'reason' = 'account_disabled'
          AND after ->> 'username' = :username
        """,
        {"username": cast.subject_actor.username},
    ) == 1


async def test_a_termination_without_an_account_records_that_there_was_none(
    platform: Platform, cast: Cast
) -> None:
    """Not everybody has a login, and that is a normal outcome, not a failure.

    The applied record says `account_id: null`, which is what keeps "there was no
    login" distinguishable from "nobody wrote it down".
    """
    change_id = await terminate(
        platform, cast, employee_id=cast.report, effective=date.today()
    )

    report = await apply_due_changes()

    assert report.failed == (), report.failed
    assert (await read_change(cast.hr, change_id))["applied_values"] == {
        "termination_date": date.today().isoformat(),
        "status": "terminated",
        "account_id": None,
        "account_disabled": False,
    }
    assert await platform.scalar(
        "SELECT count(*) FROM audit_log WHERE action = 'account.deactivated'"
    ) == 0
