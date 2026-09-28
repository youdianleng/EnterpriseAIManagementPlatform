"""Personnel changes, driven over a real database.

No mocks and no in-memory repository: the two things this module has to get right
are things a substitute would answer with the test's own assumptions. "An approval
did not touch the employee" is a statement about rows, and "the job applies a
document exactly once" is a statement about rows *and* about who holds the lock
while it does.

**Every job run goes through `app.jobs.apply_due_changes`**, the function the
command calls, on the application's own connection to the test database — as the
restricted role, with the row-level policies live. A test that called the service
directly would be testing something the scheduled task does not do.

The ticket's own vocabulary is used throughout: `state` is the one field the UI
reads, and it is the ticket's four states plus the two ways a document leaves them.
"""

import asyncio
from dataclasses import dataclass
from datetime import date, timedelta
from uuid import UUID, uuid4

import pytest

from app.core.errors import ErrorCode
from app.core.messages import message_for
from app.domain.approval.models import ApprovalStatus, DecisionKind
from app.domain.approval.service import ApprovalService
from app.domain.errors import DomainError
from app.domain.personnel.errors import PersonnelErrorCode
from app.domain.personnel.models import (
    ChangeState,
    ChangeType,
    parse_changes,
    state_of_change,
)
from app.domain.personnel.service import ENTITY_TYPE
from app.jobs.apply_personnel_changes import apply_due_changes, main
from app.repositories.approval import PostgresApprovalRepository
from app.repositories.personnel import PostgresPersonnelChangeRepository
from tests.support.platform import Actor, Platform


@dataclass(slots=True, frozen=True)
class Cast:
    """The people and places the changes in these tests move between.

    Codes as well as ids: assertions read the stored rows back joined to the
    department and position catalogues, so a failure says *where* somebody ended
    up rather than which UUID it was.
    """

    department: str
    department_code: str
    position: str
    position_code: str
    other_department: str
    other_department_code: str
    other_position: str
    other_position_code: str
    subject: str
    manager_actor: Actor
    manager: str
    #: Files the changes, and is therefore the requests' requester.
    hr: Actor
    #: Decides level two. A different holder of `hr`, since nobody decides their
    #: own request.
    other_hr: UUID


@pytest.fixture
async def cast(platform: Platform) -> Cast:
    department = await platform.department("operaciones")
    position = await platform.position(department, "technician")
    other_department = await platform.department("finanzas")
    other_position = await platform.position(other_department, "analyst")

    subject = await platform.employee()
    await platform.assign(subject, department, position)

    # The first approver is a signed-in person: the decision endpoint is reached
    # by a session, and "who approves" is answered by the engine, so the fixture
    # has to give the manager an account rather than only an employee record.
    manager_actor = await platform.account(roles=("employee",))
    manager = manager_actor.employee_id
    hr = await platform.account(roles=("hr",))
    # The engine resolves level one from the requester's primary position, so the
    # person filing these changes has to be somebody, somewhere.
    await platform.assign(hr.employee_id, department, position, manager_employee_id=manager)
    other_hr = await platform.grant_account(roles=("hr",), sign_in=False)
    return Cast(
        department=department,
        department_code="operaciones",
        position=position,
        position_code="technician",
        other_department=other_department,
        other_department_code="finanzas",
        other_position=other_position,
        other_position_code="analyst",
        subject=subject,
        manager=manager,
        manager_actor=manager_actor,
        hr=hr,
        other_hr=UUID(other_hr.employee_id),
    )


# --- helpers ---------------------------------------------------------------

#: "For the employee these tests are about", as distinct from `None`, which a join
#: needs and means "for nobody yet". A sentinel because the two are different
#: requests and defaulting one to the other is how this helper first went wrong.
SUBJECT = object()


def transfer_changes(cast: Cast, *, to_department: str | None = None) -> list[dict]:
    """The two field changes a transfer is, before values included."""
    department = to_department or cast.other_department
    position = cast.other_position if department == cast.other_department else cast.position
    return [
        {"field": "department_id", "before": cast.department, "after": department},
        {"field": "job_position_id", "before": cast.position, "after": position},
    ]


async def draft(
    actor: Actor,
    *,
    change_type: str,
    effective_date: date,
    changes: list[dict],
    employee_id: str | None = None,
):
    body: dict = {
        "change_type": change_type,
        "effective_date": effective_date.isoformat(),
        "changes": changes,
    }
    if employee_id is not None:
        body["employee_id"] = employee_id
    return await actor.post("/api/v1/personnel-changes", json=body)


async def filed(
    platform: Platform,
    cast: Cast,
    *,
    change_type: str = "transfer",
    effective_date: date,
    changes: list[dict] | None = None,
    employee_id: str | None | object = SUBJECT,
) -> str:
    """A change that exists and is ready to be filed, as the HR caller."""
    response = await draft(
        cast.hr,
        change_type=change_type,
        effective_date=effective_date,
        changes=changes if changes is not None else transfer_changes(cast),
        employee_id=cast.subject if employee_id is SUBJECT else employee_id,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def approve(platform: Platform, change_id: str, cast: Cast) -> None:
    """Both levels, through the engine, the way an approval inbox will."""
    async with platform.factory() as session:
        engine = ApprovalService(PostgresApprovalRepository(session), session)
        state = await engine.state_of(ENTITY_TYPE, UUID(change_id))
        assert state is not None, "the change was never filed"
        await engine.decide(state.id, UUID(cast.manager), DecisionKind.APPROVE, "adelante")
        await engine.decide(state.id, cast.other_hr, DecisionKind.APPROVE, "registrado")


async def filed_and_approved(
    platform: Platform,
    cast: Cast,
    *,
    effective_date: date,
    change_type: str = "transfer",
    changes: list[dict] | None = None,
    employee_id: str | None | object = SUBJECT,
) -> str:
    change_id = await filed(
        platform,
        cast,
        change_type=change_type,
        effective_date=effective_date,
        changes=changes,
        employee_id=employee_id,
    )
    response = await cast.hr.post(f"/api/v1/personnel-changes/{change_id}/submit")
    assert response.status_code == 200, response.text
    await approve(platform, change_id, cast)
    return change_id


async def read(actor: Actor, change_id: str) -> dict:
    response = await actor.get(f"/api/v1/personnel-changes/{change_id}")
    assert response.status_code == 200, response.text
    return response.json()


async def positions(platform: Platform, employee_id: str) -> list[tuple]:
    """The employee's assignments as stored, oldest first.

    Read with the department and position codes rather than ids so a failure says
    *where* somebody ended up, not which UUID it was.
    """
    return await platform.sql(
        """
        SELECT d.code, p.code, a.is_primary, a.start_date, a.end_date
        FROM employee_assignments a
        JOIN departments d ON d.id = a.department_id
        JOIN job_positions p ON p.id = a.job_position_id
        WHERE a.employee_id = :id
        ORDER BY a.start_date, d.code
        """,
        {"id": employee_id},
    )


async def employee_row(platform: Platform, employee_id: str) -> tuple:
    rows = await platform.sql(
        "SELECT hire_date, termination_date, status FROM employees WHERE id = :id",
        {"id": employee_id},
    )
    return rows[0]


# --- the payload is structured, never free text -----------------------------


async def test_a_change_with_no_structured_detail_is_refused(
    platform: Platform, cast: Cast
) -> None:
    """The requirement's own line: field names and values, not a paragraph."""
    response = await draft(
        cast.hr,
        change_type="transfer",
        effective_date=date.today(),
        changes=[],
        employee_id=cast.subject,
    )

    assert response.status_code == 422, response.text
    assert await platform.scalar("SELECT count(*) FROM personnel_changes") == 0


@pytest.mark.parametrize(
    "payload",
    [
        "traslado a finanzas",
        [],
        {},
        [{"field": "department_id"}],
        [{"department_id": "a6f1a0f0-0000-0000-0000-000000000000"}],
        [{"field": "note", "before": None, "after": "please move them"}],
        [{"field": "department_id", "before": None}],
    ],
    ids=[
        "free-text",
        "empty",
        "no-changes-key",
        "no-values",
        "no-field",
        "unknown-field",
        "no-after",
    ],
)
def test_only_a_list_of_named_field_changes_is_a_payload(payload: object) -> None:
    """`parse_changes` is the one door, and it is pure, so this needs no database."""
    with pytest.raises(DomainError) as refusal:
        parse_changes(ChangeType.TRANSFER, payload, effective_date=date.today())

    assert refusal.value.code is PersonnelErrorCode.PERSONNEL_CHANGE_INVALID_PAYLOAD


def test_a_change_type_carries_its_own_fields_only() -> None:
    """A promotion may not name a department: that is a transfer.

    Letting one type carry another's field is how a "promotion" quietly moves
    somebody between departments without anybody filing a transfer.
    """
    with pytest.raises(DomainError) as refusal:
        parse_changes(
            ChangeType.PROMOTION,
            [
                {"field": "department_id", "before": None, "after": str(uuid4())},
                {"field": "job_position_id", "before": str(uuid4()), "after": str(uuid4())},
            ],
            effective_date=date.today(),
        )

    assert refusal.value.code is PersonnelErrorCode.PERSONNEL_CHANGE_INVALID_PAYLOAD
    assert "does not carry" in (refusal.value.detail or "")


def test_a_join_states_no_previous_values(cast: Cast) -> None:
    """There was nobody there, so a `before` is a mistake rather than a detail."""
    with pytest.raises(DomainError) as refusal:
        parse_changes(
            ChangeType.JOIN,
            [
                {"field": "first_name", "before": "Ana", "after": "Ana"},
                {"field": "last_name", "before": None, "after": "Martín"},
                {"field": "email", "before": None, "after": "ana@empresa.es"},
                {"field": "hire_date", "before": None, "after": date.today().isoformat()},
                {"field": "department_id", "before": None, "after": cast.department},
                {"field": "job_position_id", "before": None, "after": cast.position},
            ],
            effective_date=date.today(),
        )

    assert "a join has nothing before it" in (refusal.value.detail or "")


# --- filing is not approving -------------------------------------------------


async def test_filing_hands_the_change_to_the_engine_and_changes_nothing_else(
    platform: Platform, cast: Cast
) -> None:
    change_id = await filed(
        platform, cast, effective_date=date.today() + timedelta(days=3)
    )

    created = await read(cast.hr, change_id)
    assert created["state"] == ChangeState.DRAFT.value
    assert created["status"] == "draft"
    assert created["approval"] is None
    assert created["applied_at"] is None

    response = await cast.hr.post(f"/api/v1/personnel-changes/{change_id}/submit")

    assert response.status_code == 200, response.text
    submitted = response.json()
    assert submitted["state"] == ChangeState.IN_APPROVAL.value
    assert submitted["status"] == "pending"
    assert submitted["approval"]["status"] == ApprovalStatus.PENDING_FIRST.value
    assert submitted["approval"]["round"] == 1
    assert submitted["approval"]["request_id"] is not None
    # Filed as the change's author, so the engine's route is theirs.
    row = await platform.sql(
        "SELECT entity_type, entity_id, requester_employee_id FROM approval_requests"
    )
    assert row[0][0] == ENTITY_TYPE
    assert str(row[0][1]) == change_id
    assert str(row[0][2]) == cast.hr.employee_id


async def test_a_filed_change_cannot_be_filed_again(platform: Platform, cast: Cast) -> None:
    change_id = await filed(platform, cast, effective_date=date.today() + timedelta(days=1))
    await cast.hr.post(f"/api/v1/personnel-changes/{change_id}/submit")

    response = await cast.hr.post(f"/api/v1/personnel-changes/{change_id}/submit")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == ErrorCode.PERSONNEL_CHANGE_NOT_DRAFT.value


async def test_a_rejection_leaves_the_change_rejected_and_the_employee_alone(
    platform: Platform, cast: Cast
) -> None:
    change_id = await filed(platform, cast, effective_date=date.today())
    before = await positions(platform, cast.subject)
    await cast.hr.post(f"/api/v1/personnel-changes/{change_id}/submit")

    async with platform.factory() as session:
        engine = ApprovalService(PostgresApprovalRepository(session), session)
        state = await engine.state_of(ENTITY_TYPE, UUID(change_id))
        await engine.decide(state.id, UUID(cast.manager), DecisionKind.REJECT, "no hay presupuesto")

    rejected = await read(cast.hr, change_id)
    assert rejected["state"] == ChangeState.REJECTED.value
    assert rejected["approval"]["decisions"][0]["comment"] == "no hay presupuesto"
    assert await positions(platform, cast.subject) == before


# --- approval is not application ---------------------------------------------


async def test_approval_does_not_change_the_employee(platform: Platform, cast: Cast) -> None:
    """Read the rows before and after the approval: byte for byte identical.

    This is the ticket's central rule. A transfer that took effect when it was
    approved would leak a move that HR agreed should start in three weeks.
    """
    effective = date.today() + timedelta(days=21)
    change_id = await filed(platform, cast, effective_date=effective)
    await cast.hr.post(f"/api/v1/personnel-changes/{change_id}/submit")
    before_positions = await positions(platform, cast.subject)
    before_employee = await employee_row(platform, cast.subject)

    await approve(platform, change_id, cast)

    approved = await read(cast.hr, change_id)
    assert approved["state"] == ChangeState.APPROVED_PENDING.value
    assert approved["status"] == "pending", (
        "the row records what this module did, not the engine's verdict"
    )
    assert approved["applied_at"] is None
    assert approved["approval"]["status"] == ApprovalStatus.APPROVED.value
    assert await positions(platform, cast.subject) == before_positions
    assert await employee_row(platform, cast.subject) == before_employee
    assert await platform.scalar(
        "SELECT count(*) FROM employee_assignments WHERE employee_id = :id", {"id": cast.subject}
    ) == len(before_positions)


# --- the job: on the effective date, in order, once --------------------------


async def test_the_job_waits_for_the_effective_date(platform: Platform, cast: Cast) -> None:
    effective = date.today() + timedelta(days=4)
    change_id = await filed_and_approved(platform, cast, effective_date=effective)
    before = await positions(platform, cast.subject)

    early = await apply_due_changes(on_date=date.today())

    assert early.applied == ()
    assert await positions(platform, cast.subject) == before
    assert (await read(cast.hr, change_id))["state"] == ChangeState.APPROVED_PENDING.value

    due = await apply_due_changes(on_date=effective)

    assert due.applied == (UUID(change_id),)
    assert due.failed == ()
    applied = await read(cast.hr, change_id)
    assert applied["state"] == ChangeState.APPLIED.value
    assert applied["status"] == "applied"
    assert applied["applied_at"] is not None
    assert applied["applied_values"]["assignment_id"] is not None

    # The old position ends the day before the move, so no day has two of them,
    # and it keeps `is_primary`: rows that have ended keep the flag so history
    # stays readable, and the partial index only enforces one *active* primary.
    assert await positions(platform, cast.subject) == [
        (
            cast.department_code,
            cast.position_code,
            True,
            date(2024, 1, 15),
            effective - timedelta(days=1),
        ),
        (cast.other_department_code, cast.other_position_code, True, effective, None),
    ]


async def test_the_job_catches_up_after_downtime_in_effective_date_order(
    platform: Platform, cast: Cast
) -> None:
    """Two transfers, both overdue, applied oldest first.

    The order is the assertion: the second move ends the position the first one
    moved the person into. Applied the other way round — newest first — the chain
    would be broken and the employee would end up somewhere HR never asked for.
    """
    third_department = await platform.department("legal")
    third_position = await platform.position(third_department, "counsel")
    first_move = date.today() - timedelta(days=3)
    second_move = date.today() - timedelta(days=1)

    earlier = await filed_and_approved(platform, cast, effective_date=first_move)
    later = await filed_and_approved(
        platform,
        cast,
        effective_date=second_move,
        changes=[
            {"field": "department_id", "before": cast.other_department, "after": third_department},
            {"field": "job_position_id", "before": cast.other_position, "after": third_position},
        ],
    )

    report = await apply_due_changes(on_date=date.today())

    assert set(report.applied) == {UUID(earlier), UUID(later)}
    assert report.failed == ()
    assert await positions(platform, cast.subject) == [
        (
            cast.department_code,
            cast.position_code,
            True,
            date(2024, 1, 15),
            first_move - timedelta(days=1),
        ),
        (
            cast.other_department_code,
            cast.other_position_code,
            True,
            first_move,
            second_move - timedelta(days=1),
        ),
        ("legal", "counsel", True, second_move, None),
    ]


async def test_running_the_job_twice_applies_nothing_the_second_time(
    platform: Platform, cast: Cast
) -> None:
    """Idempotence, asserted on the row and on the trail rather than on a counter."""
    change_id = await filed_and_approved(platform, cast, effective_date=date.today())

    first = await apply_due_changes()
    assert first.applied == (UUID(change_id),)
    applied_at = await platform.scalar(
        "SELECT applied_at FROM personnel_changes WHERE id = :id", {"id": change_id}
    )
    trail = await platform.scalar(
        "SELECT count(*) FROM audit_log WHERE action = 'personnel_change.applied'"
    )
    positions_after_first = await positions(platform, cast.subject)

    second = await apply_due_changes()

    assert second.applied == ()
    assert second.examined == 0, "an applied change is not even looked at again"
    assert await platform.scalar(
        "SELECT applied_at FROM personnel_changes WHERE id = :id", {"id": change_id}
    ) == applied_at
    assert await platform.scalar(
        "SELECT count(*) FROM audit_log WHERE action = 'personnel_change.applied'"
    ) == trail
    assert await positions(platform, cast.subject) == positions_after_first


async def test_the_command_runs_one_pass_and_reports_success(
    platform: Platform, cast: Cast
) -> None:
    """`python -m app.jobs.apply_personnel_changes` is `main`, and it exits 0.

    Exit 0 even when a change could not be applied: a document whose data moved
    under it is not a reason for a scheduler to report failure every fifteen
    minutes for ever. The failures are logged and the change stays unapplied, so
    the next pass retries it — which the test after this one pins.
    """
    change_id = await filed_and_approved(platform, cast, effective_date=date.today())

    assert await main() == 0

    assert (await read(cast.hr, change_id))["state"] == ChangeState.APPLIED.value


# --- all or nothing ----------------------------------------------------------


async def test_a_join_that_cannot_be_completed_creates_nothing(
    platform: Platform, cast: Cast
) -> None:
    """The strongest half of the atomicity claim: the *first* write is undone.

    The employee row is written and flushed before the assignment is attempted, so
    a job that committed per operation would leave a person in the directory with
    no position — employed, and nowhere to be. The rollback takes both.
    """
    email = f"nueva{uuid4().hex[:8]}@empresa.es"
    doomed_department = await platform.department("temporal")
    doomed_position = await platform.position(doomed_department, "intern")
    effective = date.today()
    change_id = await filed_and_approved(
        platform,
        cast,
        change_type="join",
        effective_date=effective,
        employee_id=None,
        changes=[
            {"field": "first_name", "before": None, "after": "Nueva"},
            {"field": "last_name", "before": None, "after": "Incorporación"},
            {"field": "email", "before": None, "after": email},
            {"field": "hire_date", "before": None, "after": effective.isoformat()},
            {"field": "department_id", "before": None, "after": doomed_department},
            {"field": "job_position_id", "before": None, "after": doomed_position},
        ],
    )
    admin = await platform.admin()
    assert (await admin.delete(f"/api/v1/positions/{doomed_position}")).status_code == 204
    assert (await admin.delete(f"/api/v1/departments/{doomed_department}")).status_code == 204

    report = await apply_due_changes()

    assert len(report.failed) == 1
    assert report.failed[0].change_id == UUID(change_id)
    assert await platform.scalar(
        "SELECT count(*) FROM employees WHERE email = :email", {"email": email}
    ) == 0, "the rollback left the employee the change had already created"
    assert await platform.scalar(
        "SELECT count(*) FROM personnel_changes WHERE id = :id AND applied_at IS NULL",
        {"id": change_id},
    ) == 1
    assert (await read(cast.hr, change_id))["state"] == ChangeState.APPROVED_PENDING.value


async def test_a_transfer_into_a_deleted_department_leaves_the_employee_where_they_were(
    platform: Platform, cast: Cast
) -> None:
    change_id = await filed_and_approved(platform, cast, effective_date=date.today())
    victim = await platform.department("efimera")
    victim_position = await platform.position(victim, "temporal")
    doomed = await filed_and_approved(
        platform,
        cast,
        effective_date=date.today() + timedelta(days=1),
        changes=[
            {"field": "department_id", "before": cast.department, "after": victim},
            {"field": "job_position_id", "before": cast.position, "after": victim_position},
        ],
    )
    admin = await platform.admin()
    assert (await admin.delete(f"/api/v1/positions/{victim_position}")).status_code == 204
    assert (await admin.delete(f"/api/v1/departments/{victim}")).status_code == 204
    before = await positions(platform, cast.subject)

    report = await apply_due_changes(on_date=date.today() + timedelta(days=1))

    assert set(report.applied) == {UUID(change_id)}
    assert [failure.change_id for failure in report.failed] == [UUID(doomed)]
    assert report.failed[0].code == ErrorCode.ORG_DEPARTMENT_NOT_FOUND.value
    # The good change landed; the impossible one wrote nothing at all.
    assert await positions(platform, cast.subject) == [
        (
            cast.department_code,
            cast.position_code,
            True,
            date(2024, 1, 15),
            date.today() - timedelta(days=1),
        ),
        (cast.other_department_code, cast.other_position_code, True, date.today(), None),
    ] != before
    assert (await read(cast.hr, doomed))["state"] == ChangeState.APPROVED_PENDING.value


# --- the applied record ------------------------------------------------------


async def test_applying_a_change_writes_the_before_and_after_pair_to_the_audit_log(
    platform: Platform, cast: Cast
) -> None:
    effective = date.today()
    await filed_and_approved(platform, cast, effective_date=effective)

    await apply_due_changes()

    rows = await platform.sql(
        "SELECT before, after, initiated_by, reason FROM audit_log "
        "WHERE action = 'personnel_change.applied'"
    )
    assert len(rows) == 1
    before, after, initiated_by, reason = rows[0]
    assert before == {
        "department_id": cast.department,
        "job_position_id": cast.position,
    }
    assert after["department_id"] == cast.other_department
    assert after["job_position_id"] == cast.other_position
    assert after["assignment_id"] is not None
    # Nobody pressed anything: the change says who filed it, the engine who approved.
    assert initiated_by == "system"
    assert "transfer" in reason


async def test_a_join_creates_the_employee_and_their_first_position(
    platform: Platform, cast: Cast
) -> None:
    """The leak check is the first assertion: nothing exists before the day."""
    email = f"alta{uuid4().hex[:8]}@empresa.es"
    effective = date.today()
    change_id = await filed_and_approved(
        platform,
        cast,
        change_type="join",
        effective_date=effective,
        employee_id=None,
        changes=[
            {"field": "first_name", "before": None, "after": "Alta"},
            {"field": "last_name", "before": None, "after": "Nueva"},
            {"field": "email", "before": None, "after": email},
            {"field": "hire_date", "before": None, "after": effective.isoformat()},
            {"field": "department_id", "before": None, "after": cast.department},
            {"field": "job_position_id", "before": None, "after": cast.position},
        ],
    )
    assert await platform.scalar(
        "SELECT count(*) FROM employees WHERE email = :email", {"email": email}
    ) == 0, "an approved hire reached the directory before its effective date"

    report = await apply_due_changes()

    assert report.failed == (), report.failed
    employee_id = await platform.scalar(
        "SELECT id FROM employees WHERE email = :email", {"email": email}
    )
    assert employee_id is not None
    assert await employee_row(platform, str(employee_id)) == (effective, None, "active")
    assert await positions(platform, str(employee_id)) == [
        (cast.department_code, cast.position_code, True, effective, None)
    ]
    applied = await read(cast.hr, change_id)
    assert applied["state"] == ChangeState.APPLIED.value
    assert applied["employee_id"] == str(employee_id), "the change is about them once they exist"
    assert applied["applied_values"]["employee_id"] == str(employee_id)


async def test_a_promotion_changes_the_position_and_keeps_the_department(
    platform: Platform, cast: Cast
) -> None:
    """The fifth type, applied: a new position, the same department, still primary.

    Same code path as a transfer — one assignment ends, another begins — which is
    the point of expressing both as assignments rather than as two mechanisms.
    """
    senior = await platform.position(cast.department, "senior_technician")
    change_id = await filed_and_approved(
        platform,
        cast,
        change_type="promotion",
        effective_date=date.today(),
        changes=[
            {"field": "job_position_id", "before": cast.position, "after": senior},
        ],
    )

    report = await apply_due_changes()

    assert report.failed == (), report.failed
    assert await positions(platform, cast.subject) == [
        (
            cast.department_code,
            cast.position_code,
            True,
            date(2024, 1, 15),
            date.today() - timedelta(days=1),
        ),
        (cast.department_code, "senior_technician", True, date.today(), None),
    ]
    applied = await read(cast.hr, change_id)
    assert applied["state"] == ChangeState.APPLIED.value
    assert applied["applied_values"]["job_position_id"] == senior


async def test_a_salary_change_is_applied_to_the_change_itself(
    platform: Platform, cast: Cast
) -> None:
    """`salary` has no table yet, and this is the documented answer to that.

    Ticket 43 adds `salary_records`. Until it does, the agreed figures are stored
    in — and applied to — this change's own record, and the audit carries the
    before/after pair. Inventing a salary table here would put a second, unofficial
    payroll record next to the official one that is coming, so the test also pins
    that no such table exists.
    """
    change_id = await filed_and_approved(
        platform,
        cast,
        change_type="salary",
        effective_date=date.today(),
        changes=[
            {"field": "base_salary", "before": "30000.00", "after": "33000.00"},
            {"field": "currency", "before": "EUR", "after": "EUR"},
        ],
    )

    await apply_due_changes()

    applied = await read(cast.hr, change_id)
    assert applied["state"] == ChangeState.APPLIED.value
    assert applied["applied_values"] == {"base_salary": "33000.00", "currency": "EUR"}
    assert {item["field"]: item["after"] for item in applied["changes"]} == {
        "base_salary": "33000.00",
        "currency": "EUR",
    }, "money is stored as a two-decimal string, never a float"
    row = await platform.sql(
        "SELECT before, after FROM audit_log WHERE action = 'personnel_change.applied'"
    )
    assert row[0][0]["base_salary"] == "30000.00"
    assert row[0][1]["base_salary"] == "33000.00"
    assert await platform.scalar("SELECT to_regclass('salary_records')") is None


async def test_a_termination_sets_the_date_and_finishes_the_account(
    platform: Platform, cast: Cast
) -> None:
    """The employee record and the login, in one transaction (ticket 18).

    This used to stop at the employee row and assert that the account was left
    alone, because the account half was ticket 18's work. It is not any more: the
    full checklist — the epoch, the Redis revocation, the directory and the
    approver refusal — is `tests/test_termination.py`, and what this asserts is
    that the termination itself still lands and now carries the account with it.
    """
    admin = await platform.admin()
    account = await admin.post(
        "/api/v1/accounts",
        json={"employee_id": cast.subject, "username": f"baja{uuid4().hex[:8]}"},
    )
    assert account.status_code == 201, account.text
    effective = date.today()
    change_id = await filed_and_approved(
        platform,
        cast,
        change_type="termination",
        effective_date=effective,
        changes=[
            {
                "field": "termination_date",
                "before": None,
                "after": effective.isoformat(),
            },
            {"field": "status", "before": "active", "after": "terminated"},
        ],
    )

    await apply_due_changes()

    assert await employee_row(platform, cast.subject) == (
        date(2024, 1, 15),
        effective,
        "terminated",
    )
    assert await platform.scalar(
        "SELECT is_active FROM users WHERE employee_id = :id", {"id": cast.subject}
    ) is False, "the leaver's login is disabled by the change that terminates them"
    applied = await read(cast.hr, change_id)
    assert applied["state"] == ChangeState.APPLIED.value
    assert applied["applied_values"]["account_disabled"] is True


# --- cancellation ------------------------------------------------------------


async def test_an_approved_change_can_be_cancelled_before_it_takes_effect(
    platform: Platform, cast: Cast
) -> None:
    """And the engine's record of the approval stays readable."""
    effective = date.today() + timedelta(days=10)
    change_id = await filed_and_approved(platform, cast, effective_date=effective)
    before = await positions(platform, cast.subject)

    response = await cast.hr.post(
        f"/api/v1/personnel-changes/{change_id}/cancel",
        json={"reason": "se traslada al mes que viene"},
    )

    assert response.status_code == 200, response.text
    cancelled = response.json()
    assert cancelled["state"] == ChangeState.CANCELLED.value
    assert cancelled["status"] == "cancelled"
    assert cancelled["cancelled_by_employee_id"] == cast.hr.employee_id
    assert cancelled["cancel_reason"] == "se traslada al mes que viene"
    assert cancelled["cancelled_at"] is not None
    # The approval itself is untouched: it happened, and the record says so.
    assert cancelled["approval"]["status"] == ApprovalStatus.APPROVED.value
    assert await platform.scalar(
        "SELECT status FROM approval_requests WHERE entity_id = :id", {"id": change_id}
    ) == "approved"

    report = await apply_due_changes(on_date=effective)

    assert report.applied == ()
    assert await positions(platform, cast.subject) == before
    assert await platform.scalar(
        "SELECT count(*) FROM audit_log WHERE action = 'personnel_change.cancelled'"
    ) == 1


async def test_a_draft_can_be_cancelled_too(platform: Platform, cast: Cast) -> None:
    change_id = await filed(platform, cast, effective_date=date.today() + timedelta(days=1))

    response = await cast.hr.post(
        f"/api/v1/personnel-changes/{change_id}/cancel", json={"reason": "mal planteado"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["state"] == ChangeState.CANCELLED.value
    assert response.json()["approval"] is None


async def test_a_cancellation_has_to_say_why(platform: Platform, cast: Cast) -> None:
    change_id = await filed(platform, cast, effective_date=date.today() + timedelta(days=1))

    response = await cast.hr.post(
        f"/api/v1/personnel-changes/{change_id}/cancel", json={"reason": ""}
    )

    assert response.status_code == 422, response.text


async def test_a_cancelled_change_cannot_be_cancelled_again(
    platform: Platform, cast: Cast
) -> None:
    change_id = await filed(platform, cast, effective_date=date.today() + timedelta(days=1))
    await cast.hr.post(
        f"/api/v1/personnel-changes/{change_id}/cancel", json={"reason": "primera vez"}
    )

    response = await cast.hr.post(
        f"/api/v1/personnel-changes/{change_id}/cancel", json={"reason": "otra vez"}
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == ErrorCode.PERSONNEL_CHANGE_NOT_CANCELLABLE.value


async def test_an_applied_change_cannot_be_cancelled_and_the_alternative_is_named(
    platform: Platform, cast: Cast
) -> None:
    """Applied is final. The refusal has to say what to do instead."""
    change_id = await filed_and_approved(platform, cast, effective_date=date.today())
    await apply_due_changes()
    before = await positions(platform, cast.subject)

    response = await cast.hr.post(
        f"/api/v1/personnel-changes/{change_id}/cancel",
        json={"reason": "mejor no"},
        headers={"Accept-Language": "en"},
    )

    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == ErrorCode.PERSONNEL_CHANGE_ALREADY_APPLIED.value
    assert error["message_key"] == "errors.personnel_change_already_applied"
    # The envelope renders the default locale, and both catalogues have to name the
    # alternative — a counter-change — rather than only refusing.
    assert "inverso" in error["message"], error["message"]
    assert "counter-change" in message_for("errors.personnel_change_already_applied", "en")
    assert await positions(platform, cast.subject) == before


# --- the states the UI reads -------------------------------------------------


async def test_the_ui_can_tell_the_six_states_apart(platform: Platform, cast: Cast) -> None:
    """One change in each state, read back through the endpoint.

    A UI that cannot tell "approved and waiting" from "in force" is the failure
    this whole ticket exists to prevent, so the six are asserted together.
    """
    soon = date.today() + timedelta(days=7)
    later = date.today() + timedelta(days=14)

    draft_change = await filed(platform, cast, effective_date=later)
    in_approval = await filed(platform, cast, effective_date=later)
    await cast.hr.post(f"/api/v1/personnel-changes/{in_approval}/submit")
    approved_pending = await filed_and_approved(platform, cast, effective_date=later)
    rejected = await filed(platform, cast, effective_date=later)
    await cast.hr.post(f"/api/v1/personnel-changes/{rejected}/submit")
    async with platform.factory() as session:
        engine = ApprovalService(PostgresApprovalRepository(session), session)
        state = await engine.state_of(ENTITY_TYPE, UUID(rejected))
        await engine.decide(state.id, UUID(cast.manager), DecisionKind.REJECT, "no")
    cancelled = await filed(platform, cast, effective_date=later)
    await cast.hr.post(
        f"/api/v1/personnel-changes/{cancelled}/cancel", json={"reason": "desistido"}
    )
    applied = await filed_and_approved(platform, cast, effective_date=soon)
    await apply_due_changes(on_date=soon)

    expected = {
        draft_change: ChangeState.DRAFT,
        in_approval: ChangeState.IN_APPROVAL,
        approved_pending: ChangeState.APPROVED_PENDING,
        rejected: ChangeState.REJECTED,
        cancelled: ChangeState.CANCELLED,
        applied: ChangeState.APPLIED,
    }
    seen = {change_id: (await read(cast.hr, change_id))["state"] for change_id in expected}

    assert seen == {change_id: state.value for change_id, state in expected.items()}


async def test_the_list_filters_by_employee_state_and_change_type(
    platform: Platform, cast: Cast
) -> None:
    subject_change = await filed(platform, cast, effective_date=date.today() + timedelta(days=2))
    other = await platform.employee()
    await platform.assign(other, cast.department, cast.position)
    other_change = await filed(
        platform, cast, effective_date=date.today() + timedelta(days=2), employee_id=other
    )
    await cast.hr.post(f"/api/v1/personnel-changes/{other_change}/submit")
    salary = await filed_and_approved(
        platform,
        cast,
        change_type="salary",
        effective_date=date.today(),
        changes=[{"field": "base_salary", "before": "1.00", "after": "2.00"}],
    )
    await apply_due_changes()

    mine = await cast.hr.get(
        "/api/v1/personnel-changes", params={"employee_id": cast.subject, "limit": 50}
    )
    waiting = await cast.hr.get(
        "/api/v1/personnel-changes", params={"state": ChangeState.IN_APPROVAL.value}
    )
    salaries = await cast.hr.get(
        "/api/v1/personnel-changes", params={"change_type": "salary"}
    )
    page = await cast.hr.get("/api/v1/personnel-changes", params={"limit": 2, "offset": 1})

    assert mine.status_code == 200, mine.text
    # Both of the subject's changes, and only theirs: a salary change is a change
    # for the same person as the move they are being paid for.
    assert {item["id"] for item in mine.json()["items"]} == {subject_change, salary}
    assert mine.json()["total"] == 2
    assert {item["id"] for item in waiting.json()["items"]} == {other_change}
    assert waiting.json()["total"] == 1
    assert {item["id"] for item in salaries.json()["items"]} == {salary}
    assert salaries.json()["total"] == 1
    assert page.json()["limit"] == 2
    assert page.json()["offset"] == 1
    assert page.json()["total"] == 3
    assert len(page.json()["items"]) == 2


async def test_the_query_state_and_the_pure_state_agree(platform: Platform, cast: Cast) -> None:
    """Two expressions of one rule: the list's `CASE` and `state_of_change`.

    The list computes the state in SQL so a page is one round trip; the detail
    computes it in Python. A status added to one and not the other would make the
    same change read differently in the two views, and nothing else would notice.
    """
    soon = date.today() + timedelta(days=5)
    ids = [
        await filed(platform, cast, effective_date=soon),
        await filed(platform, cast, effective_date=soon),
        await filed_and_approved(platform, cast, effective_date=soon),
        await filed_and_approved(platform, cast, effective_date=date.today()),
    ]
    await cast.hr.post(f"/api/v1/personnel-changes/{ids[1]}/submit")
    await cast.hr.post(f"/api/v1/personnel-changes/{ids[0]}/cancel", json={"reason": "no"})
    await apply_due_changes()

    listed = await cast.hr.get("/api/v1/personnel-changes", params={"limit": 100})
    from_query = {item["id"]: item["state"] for item in listed.json()["items"]}

    async with platform.factory() as session:
        repository = PostgresPersonnelChangeRepository(session)
        engine = ApprovalService(PostgresApprovalRepository(session), session)
        from_python = {}
        for change_id in ids:
            change = await repository.get(UUID(change_id))
            approval = await engine.state_of(ENTITY_TYPE, UUID(change_id))
            from_python[change_id] = state_of_change(
                change, approval.status if approval else None
            ).value

    assert from_query == from_python
    assert set(from_query.values()) <= {state.value for state in ChangeState}
    assert len(set(from_query.values())) >= 4, "the corpus does not exercise enough states"


# --- who may do this ---------------------------------------------------------


@pytest.mark.parametrize("role", ["employee", "manager", "it", "finance"])
async def test_a_change_for_somebody_elses_employee_is_refused_without_employee_manage(
    platform: Platform, cast: Cast, role: str
) -> None:
    """A personnel change is a personnel file entry: HR and administration.

    A manager approves their report's transfer; they do not raise it, and an
    ordinary colleague cannot even read that it exists.
    """
    actor = await platform.account(roles=(role,))
    change_id = await filed(platform, cast, effective_date=date.today() + timedelta(days=3))

    created = await draft(
        actor,
        change_type="transfer",
        effective_date=date.today() + timedelta(days=3),
        changes=transfer_changes(cast),
        employee_id=cast.subject,
    )
    listed = await actor.get("/api/v1/personnel-changes")
    one = await actor.get(f"/api/v1/personnel-changes/{change_id}")

    for response in (created, listed, one):
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == ErrorCode.FORBIDDEN.value
    assert cast.subject not in created.text
    assert "department_id" not in listed.text and "department_id" not in one.text


async def test_hr_may_raise_a_change_for_anybody(platform: Platform, cast: Cast) -> None:
    """The control for the refusal above: the same call, by the right role."""
    response = await draft(
        cast.hr,
        change_type="promotion",
        effective_date=date.today() + timedelta(days=3),
        changes=[
            {"field": "job_position_id", "before": cast.position, "after": cast.position}
        ],
        employee_id=cast.subject,
    )

    assert response.status_code == 201, response.text
    assert response.json()["state"] == ChangeState.DRAFT.value


# --- what a change may ask for ----------------------------------------------


async def test_a_move_must_name_a_position_of_the_department_it_moves_to(
    platform: Platform, cast: Cast
) -> None:
    """A position belongs to a department, so the two halves have to agree."""
    response = await draft(
        cast.hr,
        change_type="transfer",
        effective_date=date.today() + timedelta(days=3),
        changes=[
            {"field": "department_id", "before": cast.department, "after": cast.other_department},
            {"field": "job_position_id", "before": cast.position, "after": cast.position},
        ],
        employee_id=cast.subject,
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == ErrorCode.PERSONNEL_CHANGE_INVALID_PAYLOAD.value


async def test_a_promotion_may_not_move_somebody_between_departments(
    platform: Platform, cast: Cast
) -> None:
    """Staying put is what makes it a promotion; the refusal names the alternative."""
    response = await draft(
        cast.hr,
        change_type="promotion",
        effective_date=date.today() + timedelta(days=3),
        changes=[
            {
                "field": "job_position_id",
                "before": cast.position,
                "after": cast.other_position,
            }
        ],
        employee_id=cast.subject,
    )

    assert response.status_code == 422, response.text
    assert "transfer" in response.json()["error"]["detail"]


async def test_a_change_for_an_unknown_employee_is_refused(
    platform: Platform, cast: Cast
) -> None:
    response = await draft(
        cast.hr,
        change_type="transfer",
        effective_date=date.today() + timedelta(days=3),
        changes=transfer_changes(cast),
        employee_id=str(uuid4()),
    )

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == ErrorCode.EMPLOYEE_NOT_FOUND.value


async def test_a_join_does_not_name_an_employee_and_does_not_take_a_taken_email(
    platform: Platform, cast: Cast
) -> None:
    """A join creates the person, so naming one is a different document type."""
    named = await draft(
        cast.hr,
        change_type="join",
        effective_date=date.today() + timedelta(days=3),
        changes=[
            {"field": "first_name", "before": None, "after": "Alta"},
            {"field": "last_name", "before": None, "after": "Nueva"},
            {"field": "email", "before": None, "after": f"libre{uuid4().hex[:8]}@empresa.es"},
            {
                "field": "hire_date",
                "before": None,
                "after": (date.today() + timedelta(days=3)).isoformat(),
            },
            {"field": "department_id", "before": None, "after": cast.department},
            {"field": "job_position_id", "before": None, "after": cast.position},
        ],
        employee_id=cast.subject,
    )
    taken = await draft(
        cast.hr,
        change_type="join",
        effective_date=date.today() + timedelta(days=3),
        changes=[
            {"field": "first_name", "before": None, "after": "Alta"},
            {"field": "last_name", "before": None, "after": "Nueva"},
            # The subject's own address, which the directory already holds.
            {"field": "email", "before": None, "after": await platform.scalar(
                "SELECT email FROM employees WHERE id = :id", {"id": cast.subject}
            )},
            {
                "field": "hire_date",
                "before": None,
                "after": (date.today() + timedelta(days=3)).isoformat(),
            },
            {"field": "department_id", "before": None, "after": cast.department},
            {"field": "job_position_id", "before": None, "after": cast.position},
        ],
    )

    assert named.status_code == 422, named.text
    assert "does not name one" in named.json()["error"]["detail"]
    assert taken.status_code == 422, taken.text
    assert "already in use" in taken.json()["error"]["detail"]


def test_a_document_takes_effect_on_the_date_it_states() -> None:
    """Two dates for one event is one date too many, so they have to agree."""
    with pytest.raises(DomainError) as joined:
        parse_changes(
            ChangeType.JOIN,
            [
                {"field": "first_name", "before": None, "after": "Alta"},
                {"field": "last_name", "before": None, "after": "Nueva"},
                {"field": "email", "before": None, "after": "alta@empresa.es"},
                {"field": "hire_date", "before": None, "after": "2026-01-05"},
                {"field": "department_id", "before": None, "after": str(uuid4())},
                {"field": "job_position_id", "before": None, "after": str(uuid4())},
            ],
            effective_date=date(2026, 1, 6),
        )
    with pytest.raises(DomainError) as terminated:
        parse_changes(
            ChangeType.TERMINATION,
            [
                {"field": "termination_date", "before": None, "after": "2026-01-05"},
                {"field": "status", "before": "active", "after": "terminated"},
            ],
            effective_date=date(2026, 1, 6),
        )

    assert "takes effect on the hire date" in (joined.value.detail or "")
    assert "takes effect on its termination date" in (terminated.value.detail or "")


async def test_the_database_refuses_a_payload_that_is_not_a_list_of_changes(
    platform: Platform, cast: Cast
) -> None:
    """The shape is a constraint, not a convention — for anything that writes.

    Written over the owner connection on purpose: the application's own checks are
    tested above, and this is the floor under a script, a migration or a person
    with psql.
    """
    insert = (
        "INSERT INTO personnel_changes "
        "(id, change_type, employee_id, effective_date, payload, created_by_employee_id) "
        "VALUES (:id, 'transfer', :employee, current_date, CAST(:payload AS jsonb), :by)"
    )
    values = {"id": uuid4(), "employee": cast.subject, "by": cast.hr.employee_id}

    with pytest.raises(Exception) as free_text:
        await platform.sql(insert, {**values, "payload": '{"note": "move them"}'})
    with pytest.raises(Exception) as no_employee:
        await platform.sql(
            insert.replace(":employee", "NULL"),
            {**values, "payload": '{"changes": [{"field": "department_id"}]}'},
        )

    assert "ck_personnel_changes_payload" in str(free_text.value)
    assert "ck_personnel_changes_employee" in str(no_employee.value)


async def test_two_workers_at_once_apply_each_change_exactly_once(
    platform: Platform, cast: Cast
) -> None:
    """`FOR UPDATE SKIP LOCKED`, and the reason the lock is taken before the read.

    Two appliers run concurrently against three due changes. Without the lock both
    would read the same row and both would apply it — ending the same assignment
    twice, or creating the same employee twice — and the row count would be the
    only thing that noticed.
    """
    ids = []
    for _ in range(3):
        employee = await platform.employee()
        await platform.assign(employee, cast.department, cast.position)
        ids.append(
            await filed_and_approved(
                platform, cast, effective_date=date.today(), employee_id=employee
            )
        )

    first, second = await asyncio.gather(apply_due_changes(), apply_due_changes())

    applied = list(first.applied) + list(second.applied)
    assert sorted(applied) == sorted(UUID(change_id) for change_id in ids)
    assert len(set(applied)) == 3, "one change was applied by both workers"
    assert await platform.scalar(
        "SELECT count(*) FROM audit_log WHERE action = 'personnel_change.applied'"
    ) == 3
    for change_id in ids:
        assert await platform.scalar(
            "SELECT count(*) FROM personnel_changes WHERE id = :id AND applied_at IS NOT NULL",
            {"id": change_id},
        ) == 1


async def test_the_in_process_runner_is_off_unless_the_setting_turns_it_on() -> None:
    """The command is the supported way; the loop is opt-in and says so.

    A scheduler nobody can turn off is worse than a command somebody runs, so the
    default is asserted here rather than left to whoever reads a settings file.

    `_start_runners` returns every loop this process decided to run, which is one list
    for two jobs since ticket 31 added the parsing pipeline beside the applier. The
    assertion is on *which* loop is present, not on the list's length, so the two
    settings stay independent: a change that made one depend on the other would fail
    here.
    """
    from contextlib import suppress

    from app.config import Settings, get_settings
    from app.main import _start_runners

    # The parsing loop is on in development — where this suite runs — and this test is
    # about the *other* one, so it is pinned off. Otherwise the assertion below would
    # be about a list whose length depends on the environment.
    assert get_settings().personnel_apply_runner_enabled is False
    assert _start_runners(Settings(document_parse_runner_enabled=False)) == []

    enabled = Settings(
        personnel_apply_runner_enabled=True,
        personnel_apply_interval_seconds=3600,
        document_parse_runner_enabled=False,
    )
    runners = _start_runners(enabled)

    assert len(runners) == 1
    for runner in runners:
        runner.cancel()
        with suppress(asyncio.CancelledError):
            await runner


# --- the production path raises the notifications ---------------------------


async def test_filing_a_change_through_the_api_notifies_the_next_approver(
    platform: Platform, cast: Cast
) -> None:
    """The wiring ticket 19 could not prove: until this endpoint used the
    notifier, no production path raised an approval notification at all."""
    change_id = await filed(platform, cast, effective_date=date.today())
    submit_response = await cast.hr.post(f"/api/v1/personnel-changes/{change_id}/submit")
    assert submit_response.status_code == 200, submit_response.text

    rows = await platform.sql(
        """
        SELECT recipient_employee_id, type, title_key, payload
        FROM notifications
        """
    )
    assert [str(row[0]) for row in rows] == [cast.manager]
    assert rows[0][1] == "approval.awaiting_decision"
    assert rows[0][2] == "notifications.approval.awaiting_decision", "a dictionary key"
    assert " " not in rows[0][2], "a title key, not a sentence"
    assert rows[0][3]["level"] == 1


async def test_approving_through_the_api_moves_the_change_and_tells_hr(
    platform: Platform, cast: Cast
) -> None:
    """An approver has to be able to approve, and the hand-off has to be visible.

    Without the decision endpoint the two-level flow existed only in tests and
    jobs: a manager could not approve anything through the product.
    """
    change_id = await filed(platform, cast, effective_date=date.today())
    await cast.hr.post(f"/api/v1/personnel-changes/{change_id}/submit")
    await platform.sql("DELETE FROM notifications")

    decided = await cast.manager_actor.post(
        f"/api/v1/personnel-changes/{change_id}/decide",
        json={"decision": "approve", "comment": "adelante"},
    )
    assert decided.status_code == 200, decided.text
    # The engine's own status: level one is done, HR has it now.
    assert decided.json()["approval"]["status"] == "pending_second"

    rows = await platform.sql("SELECT recipient_employee_id, title_key FROM notifications")
    assert rows, "the next approver was not told"
    # The dictionary key namespace, not an audit action: `notifications.<event>`.
    assert {row[1] for row in rows} == {"notifications.approval.awaiting_decision"}


async def test_somebody_who_is_not_the_approver_cannot_decide(
    platform: Platform, cast: Cast
) -> None:
    """The engine owns that question, and the endpoint does not second-guess it."""
    change_id = await filed(platform, cast, effective_date=date.today())
    await cast.hr.post(f"/api/v1/personnel-changes/{change_id}/submit")
    outsider = await platform.account(roles=("employee",))

    response = await outsider.post(
        f"/api/v1/personnel-changes/{change_id}/decide",
        json={"decision": "approve"},
    )

    assert response.status_code == 403
