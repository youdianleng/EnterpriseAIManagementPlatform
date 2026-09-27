"""Weekly timesheets, driven over a real database and real sessions.

No mocks, for the reason the design records: what this ticket has to get right is a
statement about *rows* — a week that is one row per person, an entry that cannot be
written against a project nobody agreed to run, a warning computed from the schedule
rather than from the client's arithmetic — and a substitute would answer with the
test's own assumptions about all of them.

Every test names the checklist line it pins. The seven that matter most:

* `test_a_week_is_seven_days_from_monday_and_says_so` — the grid, and why the week is
  keyed by its Monday.
* `test_one_timesheet_per_person_per_week_is_a_database_fact` — the unique constraint,
  asserted against PostgreSQL rather than against the service.
* `test_a_day_over_its_expected_hours_warns_and_still_submits` — **the ticket's
  warning-not-refusal line**, and the test that a truncated hour would fail.
* `test_an_entry_against_an_archived_project_is_refused_by_the_database_too` —
  ticket 27's explicit third leg: a time entry against a non-`active` project, refused
  by the database and not only by the service.
* `test_submitting_files_the_week_and_freezes_it` — draft is editable, submitted is not.
* `test_a_rejected_week_can_be_corrected_and_refiled_with_the_history_kept` — the
  rejection path, with both rounds readable from the engine.
* `test_filling_in_somebody_elses_week_is_forbidden` — the 403 the ticket names.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from uuid import UUID, uuid4

import pytest

from app.core.errors import ErrorCode
from app.domain.approval.models import ApprovalStatus, DecisionKind
from app.domain.approval.service import ApprovalService
from app.domain.timesheet.models import MAX_ENTRY_MINUTES, TimesheetStatus, monday_of
from app.domain.timesheet.service import ENTITY_TYPE
from app.repositories.approval import PostgresApprovalRepository
from tests.support.platform import Actor, Platform

#: A Monday, in the past, so "today" can never change what one of these tests means.
#: 2026-03-09 is the Monday of ISO week 11.
WEEK = date(2026, 3, 9)


def day(offset: int) -> str:
    """A day of `WEEK`, as the API takes it. 0 is Monday."""
    return (WEEK + timedelta(days=offset)).isoformat()


def other_week(offset: int) -> date:
    """A week `offset` weeks away from `WEEK`, still a Monday."""
    return WEEK + timedelta(weeks=offset)


@dataclass(slots=True, frozen=True)
class Cast:
    """The people and places these tests move between."""

    #: The employee whose timesheet is being filled in.
    employee: Actor
    #: Holds a managerial position, so the engine resolves level one to them.
    manager_actor: Actor
    #: Decides level two. A different holder of `hr`, since nobody decides their own.
    other_hr: UUID
    department: str
    position: str
    #: A second employee, for the 403 cases.
    colleague: Actor
    #: Sets projects up. An ordinary employee may not create one — `project.manage`
    #: belongs to a manager or to administration — and the projects here are fixtures
    #: rather than the subject, so somebody entitled to make them makes them.
    admin: Actor
    #: A code unique to this run, so globally-unique project codes never collide.
    prefix: str


@pytest.fixture
async def cast(platform: Platform) -> Cast:
    """Two employees in one department, and the two people who will approve.

    Real accounts signed in for real: a decision made against an injected principal
    would prove nothing about the snapshot the endpoint builds, and the 403 the ticket
    asks for is a decision the kernel makes from that snapshot.
    """
    suffix = uuid4().hex[:8]
    department = await platform.department(f"tiempos{suffix}")
    position = await platform.position(department, f"tecnico{suffix}")

    # The manager's account exists before the employee is assigned, because the engine
    # resolves level one from the requester's primary position.
    manager_actor = await platform.account(roles=("employee",))
    manager_position = await platform.position(department, f"jefe{suffix}", is_managerial=True)
    await platform.assign(manager_actor.employee_id, department, manager_position)

    employee = await platform.account(roles=("employee",))
    await platform.assign(
        employee.employee_id,
        department,
        position,
        manager_employee_id=manager_actor.employee_id,
    )

    colleague = await platform.account(roles=("employee",))
    await platform.assign(colleague.employee_id, department, position)

    other_hr = await platform.grant_account(roles=("hr",), sign_in=False)
    return Cast(
        employee=employee,
        manager_actor=manager_actor,
        other_hr=UUID(other_hr.employee_id),
        department=department,
        position=position,
        colleague=colleague,
        admin=await platform.account(roles=("admin",)),
        prefix=suffix,
    )


# --- helpers ----------------------------------------------------------------


async def make_project(
    cast: Cast,
    *,
    status: str = "active",
    start: str = "2026-01-01",
    end: str | None = None,
    is_billable_default: bool = True,
) -> dict:
    response = await cast.admin.post(
        "/api/v1/projects",
        json={
            "code": f"ts{cast.prefix}{uuid4().hex[:4]}",
            "name_es": "Proyecto",
            "name_en": "Project",
            "department_id": cast.department,
            "start_date": start,
            "end_date": end,
            "status": status,
            "is_billable_default": is_billable_default,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def make_task(
    cast: Cast, project_id: str, *, is_active: bool = True, is_billable: bool | None = None
) -> dict:
    response = await cast.admin.post(
        f"/api/v1/projects/{project_id}/tasks",
        json={
            "code": f"t{uuid4().hex[:6]}",
            "name_es": "Tarea",
            "name_en": "Task",
            "is_billable": is_billable,
            "is_active": is_active,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def make_target(
    cast: Cast, *, is_billable: bool | None = None, **project: object
) -> tuple[dict, dict]:
    """A project and one of its tasks, both created through the endpoints."""
    created = await make_project(cast, **project)  # type: ignore[arg-type]
    return created, await make_task(cast, created["id"], is_billable=is_billable)


async def give_schedule(
    platform: Platform,
    employee_id: str,
    *,
    per_day: int = 480,
    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4),
) -> None:
    """A working week for one employee, written through the schedule endpoints.

    Committed rows rather than a stub: the expected-hours figure the grid shows has to
    come from the same `work_schedules` table production reads, or the warning would be
    tested against an answer nobody ever stores.
    """
    admin = await platform.account(roles=("admin",))
    schedule = await admin.post(
        "/api/v1/schedules",
        json={
            "code": f"wk{uuid4().hex[:8]}",
            "name_es": "Jornada",
            "name_en": "Working week",
            "department_id": None,
            "is_default": True,
            "days": [
                {
                    "weekday": weekday,
                    "expected_minutes": per_day,
                    "start_time": "08:00",
                    "end_time": "16:00",
                }
                for weekday in weekdays
            ],
        },
    )
    assert schedule.status_code == 201, schedule.text
    override = await admin.post(
        "/api/v1/schedules/overrides",
        json={
            "employee_id": employee_id,
            "schedule_id": schedule.json()["id"],
            "effective_from": "2020-01-01",
            "reason": "jornada de pruebas",
        },
    )
    assert override.status_code == 201, override.text


async def add_entry(
    actor: Actor,
    *,
    week: date = WEEK,
    entry_date: str | None = None,
    project_id: str,
    task_id: str,
    minutes: int = 480,
    note: str | None = None,
):
    body: dict = {
        "entry_date": entry_date or week.isoformat(),
        "project_id": project_id,
        "task_id": task_id,
        "minutes": minutes,
    }
    if note is not None:
        body["note"] = note
    return await actor.post(
        "/api/v1/timesheets/entries", params={"week": week.isoformat()}, json=body
    )


async def make_recorded_week(
    platform: Platform, actor: Actor, cast: Cast, *, week: date = WEEK
) -> None:
    """An empty-but-existing week for `actor`, written legitimately.

    A week with no row has no `timesheets.id`, and an entry's composite key names one —
    so a test that wants to insert an entry by *direct SQL* has to have a week to point
    at, and the only honest way to make one is the endpoint. Its content is then
    deleted, leaving the row and nothing else.
    """
    project, task = await make_target(cast)
    written = await add_entry(
        actor, week=week, project_id=project["id"], task_id=task["id"], minutes=15
    )
    assert written.status_code == 201, written.text
    await platform.sql("DELETE FROM timesheet_entries WHERE week_start = :week", {"week": week})


async def read_week(actor: Actor, *, week: date = WEEK, employee_id: str | None = None):
    params = {"week": week.isoformat()}
    if employee_id is not None:
        params["employee_id"] = employee_id
    return await actor.get("/api/v1/timesheets/week", params=params)


async def read_status(actor: Actor, *, week: date = WEEK):
    return await actor.get("/api/v1/timesheets/week/status", params={"week": week.isoformat()})


async def submit(actor: Actor, *, week: date = WEEK):
    return await actor.post("/api/v1/timesheets/submit", params={"week": week.isoformat()})


async def decide(
    platform: Platform,
    cast: Cast,
    *,
    week: date = WEEK,
    decision: DecisionKind = DecisionKind.APPROVE,
    level_one: bool = True,
    level_two: bool = True,
    comment: str | None = None,
) -> None:
    """Drive the engine the way an approval inbox will, at the levels asked for."""
    async with platform.factory() as session:
        engine = ApprovalService(PostgresApprovalRepository(session), session)
        sheet_id = await platform.scalar(
            "SELECT id FROM timesheets WHERE week_start = :week", {"week": week}
        )
        assert sheet_id is not None, "the week was never written"
        state = await engine.state_of(ENTITY_TYPE, sheet_id)
        assert state is not None, "the week was never filed"
        if level_one:
            await engine.decide(state.id, UUID(cast.manager_actor.employee_id), decision, comment)
        if level_two and decision is DecisionKind.APPROVE:
            await engine.decide(state.id, cast.other_hr, decision, comment)


# --- a week runs Monday to Sunday, and holds as many entries per day as there is work --


async def test_a_week_is_seven_days_from_monday_and_says_so(
    platform: Platform, cast: Cast
) -> None:
    """The grid: seven days, Monday to Sunday, each with a total of its own.

    A week that started anywhere else would make "which week is this" a question with
    two answers for two clients, and the Monday is what the unique constraint is keyed
    on.
    """
    project, task = await make_target(cast)

    response = await read_week(cast.employee)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["week_start"] == WEEK.isoformat()
    assert body["week_end"] == (WEEK + timedelta(days=6)).isoformat()
    assert len(body["days"]) == 7, "a grid is seven columns, and gaps are columns too"
    assert [row["entry_date"] for row in body["days"]] == [day(offset) for offset in range(7)]
    assert [row["weekday"] for row in body["days"]] == list(range(7))
    assert body["days"][0]["entry_date"] == "2026-03-09"  # a Monday
    assert body["status"] == "draft"
    assert body["has_timesheet"] is False, "reading a week must not create it"
    assert await platform.scalar("SELECT count(*) FROM timesheets") == 0

    # Several entries on one day, on different tasks, each a row of its own.
    other_project, other_task = await make_target(cast)
    for project_id, task_id, minutes in (
        (project["id"], task["id"], 120),
        (other_project["id"], other_task["id"], 240),
    ):
        written = await add_entry(
            cast.employee,
            entry_date=day(2),
            project_id=project_id,
            task_id=task_id,
            minutes=minutes,
        )
        assert written.status_code == 201, written.text

    reread = (await read_week(cast.employee)).json()
    wednesday = reread["days"][2]
    assert len(wednesday["entries"]) == 2, "a day holds as many entries as there is work"
    assert wednesday["total_minutes"] == 360
    assert reread["entries_total_minutes"] == 360
    assert all(
        row["total_minutes"] == 0 for index, row in enumerate(reread["days"]) if index != 2
    )


async def test_a_week_key_that_is_not_a_monday_is_refused(platform: Platform, cast: Cast) -> None:
    """The one defect that would make every total in the product wrong at once."""
    response = await read_week(cast.employee, week=date(2026, 3, 10))

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == ErrorCode.TIMESHEET_WEEK_NOT_MONDAY.value
    assert await platform.scalar("SELECT count(*) FROM timesheets") == 0


async def test_an_entry_outside_the_week_it_is_written_into_is_refused(
    platform: Platform, cast: Cast
) -> None:
    """A Sunday belongs to one week and not the next, which is what `week_start` means."""
    project, task = await make_target(cast)

    response = await add_entry(
        cast.employee,
        entry_date=other_week(1).isoformat(),
        project_id=project["id"],
        task_id=task["id"],
    )

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == ErrorCode.INVALID_REQUEST.value


async def test_a_task_of_another_project_cannot_be_recorded(
    platform: Platform, cast: Cast
) -> None:
    """The pair is checked against the project the request names, not merely looked up.

    The database refuses the pair as well, through the composite foreign key; the test
    below asserts that side of it.
    """
    mine, _mine_task = await make_target(cast)
    _theirs, their_task = await make_target(cast)

    response = await add_entry(
        cast.employee, project_id=mine["id"], task_id=their_task["id"], minutes=60
    )

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == ErrorCode.PROJECT_TASK_NOT_FOUND.value


# --- one timesheet per person per week, keyed by the Monday -------------------


async def test_one_timesheet_per_person_per_week_is_a_database_fact(
    platform: Platform, cast: Cast
) -> None:
    """**Asserted against PostgreSQL, not against the service.**

    The service never tries to write a second row — it reads the week first — so a test
    that went only through the API would pass with no constraint at all. This inserts
    the second row directly, the way a console or a retry would, and the unique
    constraint is what refuses it.
    """
    project, task = await make_target(cast)
    assert (
        await add_entry(cast.employee, project_id=project["id"], task_id=task["id"])
    ).status_code == 201

    refusal = await platform.refused_by_database(
        """
        INSERT INTO timesheets (id, employee_id, week_start, status)
        VALUES (:id, :employee_id, :week, 'draft')
        """,
        {"id": uuid4(), "employee_id": cast.employee.employee_id, "week": WEEK},
    )

    assert "uq_timesheets_employee_week" in refusal, refusal
    assert await platform.scalar(
        "SELECT count(*) FROM timesheets WHERE employee_id = :id",
        {"id": cast.employee.employee_id},
    ) == 1

    # And the API answers with the week that exists rather than with a second one.
    again = await add_entry(
        cast.employee, entry_date=day(1), project_id=project["id"], task_id=task["id"], minutes=60
    )
    assert again.status_code == 201, again.text
    assert await platform.scalar(
        "SELECT count(*) FROM timesheets WHERE employee_id = :id",
        {"id": cast.employee.employee_id},
    ) == 1


async def test_two_people_have_a_week_of_their_own_for_the_same_dates(
    platform: Platform, cast: Cast
) -> None:
    """The uniqueness is per person, not per week: a company-wide key would be one row."""
    project, task = await make_target(cast)
    await add_entry(cast.employee, project_id=project["id"], task_id=task["id"], minutes=60)
    other_project, other_task = await make_target(cast)
    await add_entry(
        cast.colleague, project_id=other_project["id"], task_id=other_task["id"], minutes=60
    )

    assert await platform.scalar("SELECT count(*) FROM timesheets") == 2
    ours = (await read_week(cast.employee)).json()
    theirs = (await read_week(cast.colleague)).json()
    assert ours["entries_total_minutes"] == 60
    assert theirs["entries_total_minutes"] == 60


# --- the grid: fast entry, copy the previous week, live totals ----------------


async def test_the_grid_reports_live_totals_per_day_and_for_the_week(
    platform: Platform, cast: Cast
) -> None:
    """Every write answers with the whole grid, so a client never guesses at a total."""
    project, task = await make_target(cast)

    first = await add_entry(
        cast.employee, entry_date=day(0), project_id=project["id"], task_id=task["id"], minutes=60
    )
    assert first.json()["entries_total_minutes"] == 60
    assert first.json()["days"][0]["total_minutes"] == 60

    second = await add_entry(
        cast.employee, entry_date=day(4), project_id=project["id"], task_id=task["id"], minutes=90
    )
    assert second.json()["entries_total_minutes"] == 150
    assert second.json()["days"][4]["total_minutes"] == 90
    assert second.json()["days"][0]["total_minutes"] == 60

    # The entry carries what it names, so a cell can be drawn without a join per row.
    entry = second.json()["days"][4]["entries"][0]
    assert entry["project_code"] == project["code"]
    assert entry["task_code"] == task["code"]
    assert entry["task_name_es"] == "Tarea"
    assert entry["is_billable"] is True


async def test_an_entry_can_be_changed_and_removed_while_the_week_is_a_draft(
    platform: Platform, cast: Cast
) -> None:
    """Editing obeys the same rule as writing, including when the target moves."""
    project, task = await make_target(cast)
    other_project, other_task = await make_target(cast, is_billable=False)
    created = await add_entry(
        cast.employee,
        entry_date=day(0),
        project_id=project["id"],
        task_id=task["id"],
        minutes=60,
        note="primera",
    )
    entry_id = created.json()["days"][0]["entries"][0]["id"]

    moved = await cast.employee.patch(
        f"/api/v1/timesheets/entries/{entry_id}",
        params={"week": WEEK.isoformat()},
        json={
            "entry_date": day(3),
            "project_id": other_project["id"],
            "task_id": other_task["id"],
            "minutes": 90,
            "note": None,
        },
    )

    assert moved.status_code == 200, moved.text
    body = moved.json()
    assert body["days"][0]["total_minutes"] == 0
    assert body["days"][3]["total_minutes"] == 90
    row = body["days"][3]["entries"][0]
    assert row["note"] is None, "an explicit null clears the note"
    # Re-resolved on the move: the new task inherits an unbillable project.
    assert row["is_billable"] is False

    # An omitted key leaves the note alone — the distinction the sentinel exists for.
    set_again = await cast.employee.patch(
        f"/api/v1/timesheets/entries/{entry_id}",
        params={"week": WEEK.isoformat()},
        json={"minutes": 30, "note": "otra vez"},
    )
    assert set_again.json()["days"][3]["entries"][0]["note"] == "otra vez"
    renamed = await cast.employee.patch(
        f"/api/v1/timesheets/entries/{entry_id}",
        params={"week": WEEK.isoformat()},
        json={"minutes": 45},
    )
    assert renamed.json()["days"][3]["entries"][0]["note"] == "otra vez", (
        "an omitted note was cleared"
    )

    removed = await cast.employee.delete(
        f"/api/v1/timesheets/entries/{entry_id}", params={"week": WEEK.isoformat()}
    )
    assert removed.status_code == 200, removed.text
    assert removed.json()["entries_total_minutes"] == 0
    assert await platform.scalar("SELECT count(*) FROM timesheet_entries") == 0


async def test_copying_the_previous_week_copies_entries_and_never_the_status(
    platform: Platform, cast: Cast
) -> None:
    """The copy is the shape of the week, not the decision somebody took about it."""
    project, task = await make_target(cast)
    await add_entry(
        cast.employee,
        entry_date=day(0),
        project_id=project["id"],
        task_id=task["id"],
        minutes=120,
        note="lunes",
    )
    await add_entry(
        cast.employee, entry_date=day(2), project_id=project["id"], task_id=task["id"], minutes=240
    )
    # File the source week, so its status is something the copy must *not* inherit.
    assert (await submit(cast.employee)).status_code == 200
    assert await platform.scalar(
        "SELECT status FROM timesheets WHERE week_start = :week", {"week": WEEK}
    ) == "pending"

    response = await cast.employee.post(
        "/api/v1/timesheets/copy-previous", params={"week": other_week(1).isoformat()}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "draft", "the copy inherited a status nobody decided for it"
    assert body["entries_total_minutes"] == 360
    assert body["days"][0]["total_minutes"] == 120
    assert body["days"][2]["total_minutes"] == 240
    assert body["days"][0]["entries"][0]["note"] == "lunes"
    assert body["approval_request_id"] is None, "the copy points at somebody else's request"


async def test_copying_is_refused_when_the_target_is_not_empty_or_has_no_source(
    platform: Platform, cast: Cast
) -> None:
    """Both refusals, because "merge" and "nothing to copy" are the two quiet wrongs."""
    project, task = await make_target(cast)

    nothing = await cast.employee.post(
        "/api/v1/timesheets/copy-previous", params={"week": WEEK.isoformat()}
    )
    assert nothing.status_code == 422, nothing.text
    assert nothing.json()["error"]["code"] == ErrorCode.TIMESHEET_COPY_SOURCE_INVALID.value

    await add_entry(
        cast.employee, week=other_week(-1), project_id=project["id"], task_id=task["id"], minutes=60
    )
    await add_entry(cast.employee, project_id=project["id"], task_id=task["id"], minutes=30)

    occupied = await cast.employee.post(
        "/api/v1/timesheets/copy-previous", params={"week": WEEK.isoformat()}
    )
    assert occupied.status_code == 409, occupied.text
    assert occupied.json()["error"]["code"] == ErrorCode.TIMESHEET_COPY_TARGET_NOT_EMPTY.value
    assert await platform.scalar(
        "SELECT count(*) FROM timesheet_entries WHERE week_start = :week", {"week": WEEK}
    ) == 1, "the refused copy wrote rows anyway"


async def test_copying_into_a_filed_week_is_refused(platform: Platform, cast: Cast) -> None:
    """The target's editability is checked before anything is written."""
    project, task = await make_target(cast)
    await add_entry(
        cast.employee, week=other_week(-1), project_id=project["id"], task_id=task["id"], minutes=60
    )
    await add_entry(cast.employee, project_id=project["id"], task_id=task["id"], minutes=60)
    assert (await submit(cast.employee)).status_code == 200

    # A *different* week is still a draft, so the copy works and only the filed one is
    # refused — which is the control for the refusal below.
    other = await cast.employee.post(
        "/api/v1/timesheets/copy-previous", params={"week": other_week(1).isoformat()}
    )
    assert other.status_code == 200, other.text

    refused = await cast.employee.post(
        "/api/v1/timesheets/copy-previous", params={"week": WEEK.isoformat()}
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == ErrorCode.TIMESHEET_NOT_EDITABLE.value


# --- a day over its expected hours warns and still lets the week be submitted -


async def test_a_day_over_its_expected_hours_warns_and_still_submits(
    platform: Platform, cast: Cast
) -> None:
    """**The ticket's warning-not-refusal line, and the test truncation would fail.**

    Nine hours against eight expected: accepted, reported as over, and every minute of
    it kept. The warning is computed from `work_schedules`, so a client that believed
    otherwise could not change it, and the response to the *submission* carries it too
    — the moment the employee can still act on it.
    """
    await give_schedule(platform, cast.employee.employee_id, per_day=480)
    project, task = await make_target(cast)

    nine_hours = await add_entry(
        cast.employee,
        entry_date=day(0),
        project_id=project["id"],
        task_id=task["id"],
        minutes=540,
    )

    assert nine_hours.status_code == 201, nine_hours.text
    monday = nine_hours.json()["days"][0]
    assert monday["total_minutes"] == 540, "nothing was silently truncated"
    assert monday["expected_minutes"] == 480
    body = nine_hours.json()
    assert body["over_budget"] is True
    assert body["over_budget_days"] == [
        {
            "entry_date": day(0),
            "total_minutes": 540,
            "expected_minutes": 480,
            "over_minutes": 60,
        }
    ]

    # The control: a day *at* its expectation is not over. Without this, the assertion
    # above would pass for a function that reported every day as over.
    tuesday = await add_entry(
        cast.employee, entry_date=day(1), project_id=project["id"], task_id=task["id"], minutes=480
    )
    assert tuesday.json()["over_budget_days"] == [
        {
            "entry_date": day(0),
            "total_minutes": 540,
            "expected_minutes": 480,
            "over_minutes": 60,
        }
    ], "an eight-hour day against eight expected was reported as over"

    filed = await submit(cast.employee)

    assert filed.status_code == 200, filed.text
    assert filed.json()["status"] == "pending", "the warning blocked the submission"
    assert filed.json()["over_budget"] is True
    assert filed.json()["over_budget_days"][0]["over_minutes"] == 60
    assert filed.json()["entries_total_minutes"] == 1020
    # Every minute is still there, after the filing as well as before it.
    assert await platform.scalar(
        "SELECT sum(minutes) FROM timesheet_entries WHERE week_start = :week", {"week": WEEK}
    ) == 1020

    # The read carries the same warning, because it is a fact about the week.
    assert (await read_week(cast.employee)).json()["over_budget"] is True


async def test_the_expected_figure_comes_from_the_schedule_and_not_from_the_client(
    platform: Platform, cast: Cast
) -> None:
    """A week nobody has configured expects nothing, and the grid says so rather than zero."""
    project, task = await make_target(cast)

    unconfigured = await read_week(cast.employee)
    assert all(row["expected_minutes"] is None for row in unconfigured.json()["days"])
    assert unconfigured.json()["expected_total_minutes"] is None

    await give_schedule(platform, cast.employee.employee_id, per_day=480, weekdays=(0, 1, 2))
    configured = (await read_week(cast.employee)).json()
    assert [row["expected_minutes"] for row in configured["days"]] == [480, 480, 480, 0, 0, 0, 0]
    assert configured["expected_total_minutes"] == 1440

    # A day nobody works is expected to hold nothing, so an entry on one is over by
    # every minute of it: excluding those days would drop the case that matters most.
    saturday = await add_entry(
        cast.employee, entry_date=day(5), project_id=project["id"], task_id=task["id"], minutes=60
    )
    assert saturday.json()["days"][5]["expected_minutes"] == 0
    assert saturday.json()["over_budget_days"] == [
        {
            "entry_date": day(5),
            "total_minutes": 60,
            "expected_minutes": 0,
            "over_minutes": 60,
        }
    ]


# --- minutes are a positive integer with a per-entry ceiling -----------------


@pytest.mark.parametrize("minutes", [0, -60, MAX_ENTRY_MINUTES + 1, 4800])
async def test_minutes_must_be_a_positive_integer_within_a_day(
    platform: Platform, cast: Cast, minutes: int
) -> None:
    """Zero, negatives and anything longer than a day, refused with the catalogue's code.

    `4800` is the typo this ceiling exists for: ten eight-hour days typed into one cell.
    The ceiling is **not** the day's expected hours, which is a warning — see the test
    above it.
    """
    project, task = await make_target(cast)

    response = await add_entry(
        cast.employee, project_id=project["id"], task_id=task["id"], minutes=minutes
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == ErrorCode.VALIDATION_FAILED.value
    assert await platform.scalar("SELECT count(*) FROM timesheet_entries") == 0


async def test_a_full_day_is_allowed_and_the_patch_obeys_the_same_ceiling(
    platform: Platform, cast: Cast
) -> None:
    """The control for the refusals above: 24 h is inside the limit and 24 h + 1 is not."""
    project, task = await make_target(cast)
    created = await add_entry(
        cast.employee, project_id=project["id"], task_id=task["id"], minutes=MAX_ENTRY_MINUTES
    )
    assert created.status_code == 201, created.text
    assert created.json()["entries_total_minutes"] == MAX_ENTRY_MINUTES

    entry_id = created.json()["days"][0]["entries"][0]["id"]
    too_long = await cast.employee.patch(
        f"/api/v1/timesheets/entries/{entry_id}",
        params={"week": WEEK.isoformat()},
        json={"minutes": MAX_ENTRY_MINUTES + 1},
    )
    assert too_long.status_code == 422, too_long.text
    assert await platform.scalar("SELECT minutes FROM timesheet_entries") == MAX_ENTRY_MINUTES


async def test_the_database_refuses_minutes_outside_the_range_too(
    platform: Platform, cast: Cast
) -> None:
    """The service refuses first; the column is what a console cannot argue with."""
    project, task = await make_target(cast)
    await add_entry(cast.employee, project_id=project["id"], task_id=task["id"], minutes=60)

    too_long = await platform.refused_by_database("UPDATE timesheet_entries SET minutes = 1441")
    zero = await platform.refused_by_database("UPDATE timesheet_entries SET minutes = 0")

    assert "ck_timesheet_entries_minutes_range" in too_long, too_long
    assert "ck_timesheet_entries_minutes_range" in zero, zero
    assert await platform.scalar("SELECT minutes FROM timesheet_entries") == 60


async def test_the_database_refuses_a_task_of_another_project_too(
    platform: Platform, cast: Cast
) -> None:
    """The composite foreign key, which is what makes the pair a database fact."""
    mine, mine_task = await make_target(cast)
    theirs, their_task = await make_target(cast)
    assert theirs["id"] != mine["id"]
    await add_entry(cast.employee, project_id=mine["id"], task_id=mine_task["id"], minutes=60)

    refusal = await platform.refused_by_database(
        "UPDATE timesheet_entries SET task_id = :task_id, project_id = :project_id",
        {"task_id": their_task["id"], "project_id": mine["id"]},
    )

    assert "fk_timesheet_entries_task_project" in refusal, refusal
    assert str(await platform.scalar("SELECT task_id FROM timesheet_entries")) == mine_task["id"]


# --- the status is visible; a draft is editable and a filed week is not -------


async def test_submitting_files_the_week_and_freezes_it(platform: Platform, cast: Cast) -> None:
    """Draft is editable, pending is not, and the status says which."""
    project, task = await make_target(cast)
    created = await add_entry(
        cast.employee, project_id=project["id"], task_id=task["id"], minutes=240
    )
    entry_id = created.json()["days"][0]["entries"][0]["id"]

    assert (await read_week(cast.employee)).json()["is_editable"] is True

    filed = await submit(cast.employee)

    assert filed.status_code == 200, filed.text
    assert filed.json()["status"] == "pending"
    assert filed.json()["is_editable"] is False
    assert filed.json()["submitted_at"] is not None
    assert filed.json()["approval_request_id"] is not None

    status = await read_status(cast.employee)
    assert status.status_code == 200, status.text
    assert status.json()["status"] == "pending"
    assert status.json()["approval"]["status"] == ApprovalStatus.PENDING_FIRST.value
    assert status.json()["approval"]["pending_level"] == 1

    # The notification went out, because the engine is wrapped in the notifier.
    told = await platform.sql("SELECT recipient_employee_id FROM notifications ORDER BY created_at")
    assert [str(row[0]) for row in told] == [cast.manager_actor.employee_id], (
        "filing a week did not tell the person who has to approve it"
    )

    # Every write path refuses, with the module's own code rather than the engine's.
    refused = [
        await add_entry(
            cast.employee,
            entry_date=day(1),
            project_id=project["id"],
            task_id=task["id"],
            minutes=60,
        ),
        await cast.employee.patch(
            f"/api/v1/timesheets/entries/{entry_id}",
            params={"week": WEEK.isoformat()},
            json={"minutes": 30},
        ),
        await cast.employee.delete(
            f"/api/v1/timesheets/entries/{entry_id}", params={"week": WEEK.isoformat()}
        ),
        await submit(cast.employee),
    ]
    assert [response.status_code for response in refused] == [409, 409, 409, 409], [
        response.text for response in refused
    ]
    assert {response.json()["error"]["code"] for response in refused} == {
        ErrorCode.TIMESHEET_NOT_EDITABLE.value
    }


async def test_an_approved_week_reads_approved_and_is_locked_for_ever(
    platform: Platform, cast: Cast
) -> None:
    """Both levels, then the status the grid shows.

    The status is read from the engine on the very next request rather than after the
    next write, which is the whole reason the read reconciles it.
    """
    project, task = await make_target(cast)
    await add_entry(cast.employee, project_id=project["id"], task_id=task["id"], minutes=240)
    assert (await submit(cast.employee)).status_code == 200

    await decide(platform, cast, comment="aprobado")

    body = (await read_week(cast.employee)).json()
    assert body["status"] == TimesheetStatus.APPROVED.value
    assert body["is_editable"] is False

    status = (await read_status(cast.employee)).json()
    assert status["approval"]["status"] == ApprovalStatus.APPROVED.value
    assert status["approval"]["decided_at"] is not None
    assert [(row["level"], row["decision"]) for row in status["approval"]["decisions"]] == [
        (1, "approved"),
        (2, "approved"),
    ]
    assert status["approval"]["decisions"][0]["comment"] == "aprobado"

    # And an approved week refuses its writes like any other filed one.
    refused = await add_entry(
        cast.employee, entry_date=day(1), project_id=project["id"], task_id=task["id"], minutes=60
    )
    assert refused.status_code == 409, refused.text


async def test_a_week_with_nothing_in_it_cannot_be_filed(platform: Platform, cast: Cast) -> None:
    """There is no such thing as filing nothing, and the refusal says so."""
    response = await submit(cast.employee)

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == ErrorCode.TIMESHEET_NOT_EDITABLE.value
    assert await platform.scalar("SELECT count(*) FROM approval_requests") == 0


# --- a rejected week can be corrected and refiled, and the history is kept ----


async def test_a_rejected_week_can_be_corrected_and_refiled_with_the_history_kept(
    platform: Platform, cast: Cast
) -> None:
    """**The rejection path, and the history the ticket asks to keep.**

    A return-for-correction leaves the engine's request in `draft` and this module's
    week in `rejected` — the employee's again, which is what the grid has to show.
    Refiling opens a **new round**, and both rounds' decisions stay readable. Nothing
    here mirrors the engine's record, because two copies of "who rejected this and why"
    are two versions of the truth.
    """
    project, task = await make_target(cast)
    created = await add_entry(
        cast.employee, project_id=project["id"], task_id=task["id"], minutes=30
    )
    entry_id = created.json()["days"][0]["entries"][0]["id"]
    assert (await submit(cast.employee)).status_code == 200

    await decide(platform, cast, decision=DecisionKind.RETURN, comment="faltan horas del martes")

    returned = (await read_week(cast.employee)).json()
    assert returned["status"] == TimesheetStatus.REJECTED.value
    assert returned["is_editable"] is True, "a returned week is the employee's again"

    status = (await read_status(cast.employee)).json()
    assert status["approval"]["round"] == 1
    assert status["approval"]["decisions"][0]["comment"] == "faltan horas del martes"

    # Correct it: the two writes a returned week is for.
    corrected = await cast.employee.patch(
        f"/api/v1/timesheets/entries/{entry_id}",
        params={"week": WEEK.isoformat()},
        json={"minutes": 480},
    )
    assert corrected.status_code == 200, corrected.text
    extra = await add_entry(
        cast.employee, entry_date=day(1), project_id=project["id"], task_id=task["id"], minutes=480
    )
    assert extra.status_code == 201, extra.text

    refiled = await submit(cast.employee)

    assert refiled.status_code == 200, refiled.text
    assert refiled.json()["status"] == "pending"
    assert refiled.json()["entries_total_minutes"] == 960

    # Both rounds are the engine's record, and both are readable.
    status = (await read_status(cast.employee)).json()
    assert status["approval"]["round"] == 2
    rounds = [(row["round"], row["decision"]) for row in status["approval"]["decisions"]]
    assert (1, "returned") in rounds, "the first round's return was lost"
    assert await platform.scalar("SELECT count(*) FROM approval_requests") == 1, (
        "a resubmission is a new round of one request, not a second request"
    )

    await decide(platform, cast, comment="ahora si")
    assert (await read_week(cast.employee)).json()["status"] == TimesheetStatus.APPROVED.value
    final = (await read_status(cast.employee)).json()
    # Round 1 holds one decision — the return, at level 1, because that is where the
    # round ended. Round 2 holds both levels, because it ran to the end. Two rounds of
    # one request, which is the history the ticket asks to keep.
    assert [
        (row["round"], row["level"], row["decision"])
        for row in final["approval"]["decisions"]
    ] == [(1, 1, "returned"), (2, 1, "approved"), (2, 2, "approved")]
    assert final["approval"]["status"] == ApprovalStatus.APPROVED.value


async def test_a_final_rejection_leaves_the_week_editable_and_the_reason_readable(
    platform: Platform, cast: Cast
) -> None:
    """A rejection is not a dead end for the *week*, whatever it is for that request:
    the employee corrects it and files again."""
    project, task = await make_target(cast)
    await add_entry(cast.employee, project_id=project["id"], task_id=task["id"], minutes=30)
    assert (await submit(cast.employee)).status_code == 200

    await decide(platform, cast, decision=DecisionKind.REJECT, comment="no procede")

    body = (await read_week(cast.employee)).json()
    assert body["status"] == TimesheetStatus.REJECTED.value
    assert body["is_editable"] is True

    status = (await read_status(cast.employee)).json()
    assert status["approval"]["status"] == ApprovalStatus.REJECTED.value
    assert status["approval"]["decisions"][0]["comment"] == "no procede"

    # The employee can still correct it.
    again = await add_entry(
        cast.employee, entry_date=day(1), project_id=project["id"], task_id=task["id"], minutes=60
    )
    assert again.status_code == 201, again.text


# --- your own hours only: filling in somebody else's is a 403 -----------------


async def test_filling_in_somebody_elses_week_is_forbidden(platform: Platform, cast: Cast) -> None:
    """**The ticket's 只能为本人填报，代填返回 403**, on every read and every write.

    Naming a colleague is expressible on purpose: the surface could have left
    `employee_id` out and made the act unsayable, but then the refusal could not be
    tested and the audit trail would have no record of it.
    """
    project, task = await make_target(cast)
    await add_entry(cast.employee, project_id=project["id"], task_id=task["id"], minutes=60)
    entry_id = await platform.scalar(
        "SELECT id FROM timesheet_entries WHERE week_start = :week", {"week": WEEK}
    )
    mine = {"week": WEEK.isoformat(), "employee_id": cast.employee.employee_id}

    attempts = [
        await read_week(cast.colleague, employee_id=cast.employee.employee_id),
        await cast.colleague.get("/api/v1/timesheets/week/status", params=mine),
        await cast.colleague.post(
            "/api/v1/timesheets/entries",
            params=mine,
            json={
                "entry_date": day(1),
                "project_id": project["id"],
                "task_id": task["id"],
                "minutes": 60,
            },
        ),
        await cast.colleague.post("/api/v1/timesheets/submit", params=mine),
        await cast.colleague.post(
            "/api/v1/timesheets/copy-previous",
            params={"week": other_week(1).isoformat(), "employee_id": cast.employee.employee_id},
        ),
        await cast.colleague.patch(
            f"/api/v1/timesheets/entries/{entry_id}", params=mine, json={"minutes": 1}
        ),
        await cast.colleague.delete(f"/api/v1/timesheets/entries/{entry_id}", params=mine),
    ]

    assert [response.status_code for response in attempts] == [403] * 7, [
        (response.status_code, response.text) for response in attempts
    ]
    assert {response.json()["error"]["code"] for response in attempts} == {
        ErrorCode.FORBIDDEN.value
    }
    assert await platform.scalar("SELECT minutes FROM timesheet_entries") == 60, (
        "a refused write changed the row anyway"
    )

    refusals = await platform.sql(
        "SELECT after -> 'action' FROM audit_log WHERE action = 'access.refused'"
    )
    assert refusals, "a refusal was not recorded"
    assert {row[0] for row in refusals} == {
        "timesheet.read_own",
        "timesheet.write_own",
        "timesheet.submit_own",
    }


async def test_the_callers_own_week_is_reachable_by_naming_themselves(
    platform: Platform, cast: Cast
) -> None:
    """The control for the refusal above, and the reason `employee_id` exists at all.

    Without this, a surface that refused *everybody* would pass the test above.
    """
    project, task = await make_target(cast)
    assert (
        await add_entry(
            cast.employee,
            entry_date=day(2),
            project_id=project["id"],
            task_id=task["id"],
            minutes=120,
        )
    ).status_code == 201

    response = await read_week(cast.employee, employee_id=cast.employee.employee_id)

    assert response.status_code == 200, response.text
    assert response.json()["days"][2]["total_minutes"] == 120


async def test_a_manager_cannot_read_a_reports_week(platform: Platform, cast: Cast) -> None:
    """Not even the person who approves it, until ticket 29 adds that action.

    `timesheet.read_own` is self-only, so a manager's route to their report's hours is a
    *new* action with its own resource rule — which is what the kernel's
    `SELF_ONLY_ACTIONS` is for, and what stops "I approve it" becoming "I can read
    everything".
    """
    project, task = await make_target(cast)
    await add_entry(cast.employee, project_id=project["id"], task_id=task["id"], minutes=60)

    response = await read_week(cast.manager_actor, employee_id=cast.employee.employee_id)

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == ErrorCode.FORBIDDEN.value


async def test_listing_your_own_weeks_shows_only_yours(platform: Platform, cast: Cast) -> None:
    """The list form of the same rule: scoped by the query, with no parameter that could
    name somebody else."""
    project, task = await make_target(cast)
    await add_entry(cast.employee, project_id=project["id"], task_id=task["id"], minutes=60)
    other_project, other_task = await make_target(cast)
    await add_entry(
        cast.colleague,
        week=other_week(-1),
        project_id=other_project["id"],
        task_id=other_task["id"],
        minutes=60,
    )

    mine = await cast.employee.get("/api/v1/timesheets/mine")
    theirs = await cast.colleague.get("/api/v1/timesheets/mine")

    assert mine.status_code == 200, mine.text
    assert mine.json()["total"] == 1
    assert [row["week_start"] for row in mine.json()["items"]] == [WEEK.isoformat()]
    assert mine.json()["items"][0]["status"] == "draft"
    assert mine.json()["items"][0]["is_editable"] is True
    assert [row["week_start"] for row in theirs.json()["items"]] == [other_week(-1).isoformat()]


# --- entries outside a project's own dates are refused ------------------------


async def test_an_entry_outside_the_projects_own_dates_is_refused(
    platform: Platform, cast: Cast
) -> None:
    """Before the start and after the end, both refused with the project's own window."""
    early, early_task = await make_target(cast, start="2026-04-01")
    finished, finished_task = await make_target(cast, start="2025-01-01", end="2026-02-28")

    too_early = await add_entry(
        cast.employee,
        entry_date=day(0),
        project_id=early["id"],
        task_id=early_task["id"],
        minutes=60,
    )
    too_late = await add_entry(
        cast.employee,
        entry_date=day(0),
        project_id=finished["id"],
        task_id=finished_task["id"],
        minutes=60,
    )

    for response in (too_early, too_late):
        assert response.status_code == 422, response.text
        assert (
            response.json()["error"]["code"]
            == ErrorCode.TIMESHEET_ENTRY_OUTSIDE_PROJECT_DATES.value
        )
    assert await platform.scalar("SELECT count(*) FROM timesheet_entries") == 0

    # The control: a project whose window contains the week accepts it.
    inside = await make_target(cast, start="2026-03-01", end="2026-03-31")
    accepted = await add_entry(
        cast.employee,
        entry_date=day(0),
        project_id=inside[0]["id"],
        task_id=inside[1]["id"],
        minutes=60,
    )
    assert accepted.status_code == 201, accepted.text


async def test_the_database_refuses_an_entry_outside_the_projects_dates_too(
    platform: Platform, cast: Cast
) -> None:
    """The service refuses first; the trigger is what a console cannot argue with.

    An entry written on the Monday is moved, by direct SQL, onto the Saturday of the
    same week — past the project's end date. Nothing in the application is involved.
    """
    project, task = await make_target(cast, start="2026-03-01", end="2026-03-13")
    assert (
        await add_entry(cast.employee, project_id=project["id"], task_id=task["id"], minutes=60)
    ).status_code == 201

    refusal = await platform.refused_by_database(
        "UPDATE timesheet_entries SET entry_date = :outside",
        {"outside": (WEEK + timedelta(days=5)).isoformat()},
    )

    assert "outside project" in refusal, refusal
    assert await platform.scalar("SELECT entry_date FROM timesheet_entries") == WEEK


# --- ticket 27's third leg: a non-active project cannot receive an entry ------


async def test_an_entry_against_an_archived_project_is_refused_by_the_database_too(
    platform: Platform, cast: Cast
) -> None:
    """**Ticket 27's explicit third leg**, in the form its ticket file asks for.

    Two guarantees, and the second is the one only the database can make. The service
    refuses an archived project with a catalogued code; the *trigger* refuses the same
    row when it is inserted directly, with no service anywhere in the path — which is
    what a console, a script or a future endpoint would otherwise be able to do.
    """
    project, task = await make_target(cast)
    archived = await cast.admin.patch(
        f"/api/v1/projects/{project['id']}", json={"status": "archived"}
    )
    assert archived.status_code == 200, archived.text

    refused = await add_entry(
        cast.employee, project_id=project["id"], task_id=task["id"], minutes=60
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == ErrorCode.PROJECT_TASK_NOT_RECORDABLE.value

    # A week row for the entry's composite key to name, made legitimately and emptied.
    await make_recorded_week(platform, cast.employee, cast)

    refusal = await platform.refused_by_database(
        """
        INSERT INTO timesheet_entries
            (id, timesheet_id, employee_id, week_start, entry_date,
             project_id, task_id, minutes, is_billable)
        SELECT :id, t.id, t.employee_id, t.week_start, :day, :project_id, :task_id, 60, true
          FROM timesheets t
         WHERE t.week_start = :week
        """,
        {
            "id": uuid4(),
            "day": day(0),
            "project_id": project["id"],
            "task_id": task["id"],
            "week": WEEK,
        },
    )

    assert "only an active project accepts new time" in refusal, refusal
    assert "archived" in refusal, refusal
    assert await platform.scalar(
        "SELECT count(*) FROM timesheet_entries WHERE project_id = :id", {"id": project["id"]}
    ) == 0, "the refused insert wrote a row anyway"


async def test_a_project_that_is_not_active_refuses_an_entry_at_the_database(
    platform: Platform, cast: Cast
) -> None:
    """The same refusal for a project put back to `draft`.

    Written against `draft` rather than `archived` because it is the status a
    service-only check would most plausibly miss: a draft project is a normal state on
    the way somewhere, not an end state somebody deliberately withdrew.
    """
    project, task = await make_target(cast)
    await make_recorded_week(platform, cast.employee, cast)
    await cast.admin.patch(f"/api/v1/projects/{project['id']}", json={"status": "draft"})

    refusal = await platform.refused_by_database(
        """
        INSERT INTO timesheet_entries
            (id, timesheet_id, employee_id, week_start, entry_date,
             project_id, task_id, minutes, is_billable)
        SELECT :id, t.id, t.employee_id, t.week_start, :day, :project_id, :task_id, 60, true
          FROM timesheets t
         WHERE t.week_start = :week
        """,
        {
            "id": uuid4(),
            "day": day(0),
            "project_id": project["id"],
            "task_id": task["id"],
            "week": WEEK,
        },
    )

    assert "only an active project accepts new time" in refusal, refusal
    assert "draft" in refusal, refusal
    assert await platform.scalar("SELECT count(*) FROM timesheet_entries") == 0


async def test_a_closed_project_keeps_the_entries_it_already_has(
    platform: Platform, cast: Cast
) -> None:
    """The other half, and the reason the guard is a trigger rather than a constraint.

    A constraint that also governed writes to `projects` would make closing a project
    impossible — or would take its history with it. What matters is the direction: new
    time refused, recorded time untouched, and the week still readable.
    """
    project, task = await make_target(cast)
    assert (
        await add_entry(cast.employee, project_id=project["id"], task_id=task["id"], minutes=300)
    ).status_code == 201

    await cast.admin.patch(f"/api/v1/projects/{project['id']}", json={"status": "closed"})

    assert await platform.scalar(
        "SELECT count(*) FROM timesheet_entries WHERE project_id = :id", {"id": project["id"]}
    ) == 1, "closing the project took its history with it"
    assert await platform.scalar("SELECT sum(minutes) FROM timesheet_entries") == 300

    body = (await read_week(cast.employee)).json()
    assert body["days"][0]["total_minutes"] == 300, "the grid could not read the week any more"
    assert body["days"][0]["entries"][0]["is_billable"] is True

    # ... and no new time: the same week, a second day.
    response = await add_entry(
        cast.employee, entry_date=day(1), project_id=project["id"], task_id=task["id"], minutes=60
    )
    assert response.status_code == 422, response.text


async def test_a_week_naming_a_project_archived_since_is_refused_at_submission(
    platform: Platform, cast: Cast
) -> None:
    """Between typing an entry on Monday and filing it on Friday, the project can stop.

    Filing a week that names a task nobody may record against would hand the approver a
    document that could not have been written that day — and the refusal now is one the
    employee can still act on, which the same refusal after approval is not.
    """
    project, task = await make_target(cast)
    assert (
        await add_entry(cast.employee, project_id=project["id"], task_id=task["id"], minutes=60)
    ).status_code == 201
    await cast.admin.patch(f"/api/v1/projects/{project['id']}", json={"status": "archived"})

    response = await submit(cast.employee)

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == ErrorCode.PROJECT_TASK_NOT_RECORDABLE.value
    assert await platform.scalar("SELECT count(*) FROM approval_requests") == 0


# --- the billable flag is the server's answer --------------------------------


async def test_the_recorded_billable_flag_is_the_tasks_configuration(
    platform: Platform, cast: Cast
) -> None:
    """The client cannot state it, and the stored value follows the task.

    `EntryWrite` is a `StrictModel`, so `is_billable` in the body is refused rather than
    ignored — and the value on the row comes from `RecordTarget`, resolved from the
    task's override and its project's default.
    """
    unbillable_project, unbillable_task = await make_target(cast, is_billable_default=False)
    billable_project, billable_task = await make_target(cast, is_billable_default=True)

    inherited = await add_entry(
        cast.employee,
        entry_date=day(0),
        project_id=unbillable_project["id"],
        task_id=unbillable_task["id"],
        minutes=60,
    )
    assert inherited.status_code == 201, inherited.text
    assert inherited.json()["days"][0]["entries"][0]["is_billable"] is False

    # A task that overrides its project: unbillable under a billable project, so the
    # value on the row can only have come from the task's own configuration.
    override_task = await make_task(cast, billable_project["id"], is_billable=False)
    row = await add_entry(
        cast.employee,
        entry_date=day(1),
        project_id=billable_project["id"],
        task_id=override_task["id"],
        minutes=60,
    )
    assert row.status_code == 201, row.text
    assert row.json()["days"][1]["entries"][0]["is_billable"] is False, (
        "the task's own override was not honoured"
    )
    # The control: the same project's plain task inherits its billable default.
    plain = await add_entry(
        cast.employee,
        entry_date=day(2),
        project_id=billable_project["id"],
        task_id=billable_task["id"],
        minutes=60,
    )
    assert plain.json()["days"][2]["entries"][0]["is_billable"] is True

    claimed = await cast.employee.post(
        "/api/v1/timesheets/entries",
        params={"week": WEEK.isoformat()},
        json={
            "entry_date": day(3),
            "project_id": unbillable_project["id"],
            "task_id": unbillable_task["id"],
            "minutes": 60,
            "is_billable": True,
        },
    )
    assert claimed.status_code == 422, claimed.text
    assert claimed.json()["error"]["code"] == ErrorCode.VALIDATION_FAILED.value


async def test_a_deactivated_task_refuses_new_time_but_keeps_what_it_has(
    platform: Platform, cast: Cast
) -> None:
    """A task switched off is one no new entry may name, and an old one still can."""
    project, task = await make_target(cast)
    assert (
        await add_entry(cast.employee, project_id=project["id"], task_id=task["id"], minutes=60)
    ).status_code == 201

    await cast.admin.post(f"/api/v1/projects/{project['id']}/tasks/{task['id']}/deactivate")

    refused = await add_entry(
        cast.employee, entry_date=day(1), project_id=project["id"], task_id=task["id"], minutes=60
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == ErrorCode.PROJECT_TASK_NOT_RECORDABLE.value
    assert (await read_week(cast.employee)).json()["days"][0]["total_minutes"] == 60


# --- the module's own bookkeeping -------------------------------------------


async def test_every_write_leaves_a_trail(platform: Platform, cast: Cast) -> None:
    """A timesheet is evidence, so who wrote what and when is recorded."""
    project, task = await make_target(cast)
    created = await add_entry(
        cast.employee, project_id=project["id"], task_id=task["id"], minutes=120
    )
    entry_id = created.json()["days"][0]["entries"][0]["id"]
    await cast.employee.patch(
        f"/api/v1/timesheets/entries/{entry_id}",
        params={"week": WEEK.isoformat()},
        json={"minutes": 240},
    )
    await cast.employee.delete(
        f"/api/v1/timesheets/entries/{entry_id}", params={"week": WEEK.isoformat()}
    )
    await add_entry(cast.employee, project_id=project["id"], task_id=task["id"], minutes=300)
    await submit(cast.employee)
    await cast.employee.post(
        "/api/v1/timesheets/copy-previous", params={"week": other_week(1).isoformat()}
    )

    actions = [row[0] for row in await platform.sql("SELECT action FROM audit_log ORDER BY id")]

    assert "timesheet.created" in actions
    assert actions.count("timesheet.entry_written") == 3  # add, patch, add
    assert "timesheet.entry_removed" in actions
    assert "timesheet.submitted" in actions
    assert "timesheet.copied" in actions

    # The submission record carries the total that was filed, and every record names
    # the week's own row rather than only a date.
    submitted = await platform.sql(
        "SELECT entity_id, after -> 'total_minutes' FROM audit_log "
        "WHERE action = 'timesheet.submitted'"
    )
    assert submitted[0][1] == 300
    assert str(submitted[0][0]) == str(
        await platform.scalar("SELECT id FROM timesheets WHERE week_start = :week", {"week": WEEK})
    )


async def test_the_audit_trail_records_what_the_warning_was_at_submission(
    platform: Platform, cast: Cast
) -> None:
    """A week filed with a long day is the fact an approver is being asked about.

    The record of what the employee was told belongs beside it: "they filed a 9-hour
    day" and "they filed it having been warned" are different facts about the week.
    """
    await give_schedule(platform, cast.employee.employee_id, per_day=480)
    project, task = await make_target(cast)
    await add_entry(
        cast.employee, entry_date=day(0), project_id=project["id"], task_id=task["id"], minutes=540
    )

    assert (await submit(cast.employee)).status_code == 200

    recorded = await platform.sql(
        "SELECT after -> 'over_budget_days' FROM audit_log WHERE action = 'timesheet.submitted'"
    )
    assert recorded[0][0] == [day(0)]


async def test_the_week_status_follows_the_engine_on_the_very_next_read(
    platform: Platform, cast: Cast
) -> None:
    """Ticket 29 decides through the engine; every read has to show the outcome.

    The decision here goes in *behind* the endpoint, on its own session, which is what a
    future approval inbox does. The very next request has to report the outcome rather
    than a cached `pending` — a cache only ever written by this module's own `submit` is
    a cache that goes stale on the one event that matters.
    """
    project, task = await make_target(cast)
    await add_entry(cast.employee, project_id=project["id"], task_id=task["id"], minutes=60)
    assert (await submit(cast.employee)).status_code == 200

    await decide(platform, cast)

    # No endpoint of this module ran between the decision and these reads.
    assert (await read_week(cast.employee)).json()["status"] == TimesheetStatus.APPROVED.value
    assert (await read_status(cast.employee)).json()["status"] == TimesheetStatus.APPROVED.value

    # And a write path reconciles the stored column, so a later list agrees with the grid.
    refused = await add_entry(
        cast.employee, entry_date=day(1), project_id=project["id"], task_id=task["id"], minutes=60
    )
    assert refused.status_code == 409, refused.text
    assert await platform.scalar(
        "SELECT status FROM timesheets WHERE week_start = :week", {"week": WEEK}
    ) == "approved"


async def test_the_weeks_a_person_has_are_listed_newest_first(
    platform: Platform, cast: Cast
) -> None:
    project, task = await make_target(cast)
    for week in (other_week(-2), other_week(-1), WEEK):
        await add_entry(
            cast.employee, week=week, project_id=project["id"], task_id=task["id"], minutes=60
        )

    response = await cast.employee.get("/api/v1/timesheets/mine", params={"limit": 2})

    assert response.status_code == 200, response.text
    assert response.json()["total"] == 3
    assert len(response.json()["items"]) == 2
    assert [row["week_start"] for row in response.json()["items"]] == [
        WEEK.isoformat(),
        other_week(-1).isoformat(),
    ]
    assert monday_of(WEEK) == WEEK, "the module's own week helper agrees with the fixture"
