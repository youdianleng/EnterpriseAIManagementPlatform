"""Ticket 29: the permanent lock, the supplementary submission, and the week lock.

Real database, real sessions, real engine — no mocks, for the reason the timesheet
module records: what this ticket has to get right is a statement about *rows*. A locked
week that nothing may write, a reversal that is the exact negation of the row it
cancels, a net that is arithmetic over those rows, and a week that has fallen out of
the eight-week window and is closed to every write path — all four are properties of
what is stored, and a substitute would answer with the test's own assumptions.

Every test names the checklist line it pins. The ones worth reading first:

* `test_an_approved_week_is_locked_and_every_write_path_refuses` — 审批通过后状态变为
  已锁定，无法直接编辑任何条目, over **every** write route rather than the one that
  seemed easiest to guard, and again at the database for a console that never asks.
* `test_every_write_route_is_enumerated_here` — the list above is compared with the
  routes the application actually declares, so a write route added later fails this
  file instead of quietly becoming a hole.
* `test_a_supplement_writes_a_reversal_and_a_replacement_and_the_net_is_the_difference`
  — 冲销 entry plus 新条目, asserted as `original + reversal + new = expected`, with the
  original's rows byte-for-byte where they were.
* `test_the_link_is_readable_from_both_ends` — 关联双向可查: the week lists its
  supplements, and each supplement names the sheet it corrects.
* `test_a_supplement_goes_through_both_levels_and_locks_when_approved` — 补充提交同样走
  两级审批，通过后净额正确.
* `test_a_week_outside_the_window_is_refused_with_the_weeks_that_remain` — the
  eight-week window, refused with a catalogued, bilingual key and the count in the
  detail.
* `test_outside_the_window_no_write_path_may_touch_the_week` — 全局周锁机制存在, with
  the database refusing the same write once the week is closed.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.core.errors import ErrorCode
from app.domain.approval.models import ApprovalStatus, DecisionKind
from app.domain.approval.service import ApprovalService
from app.domain.attendance.business_day import madrid_today
from app.domain.timesheet.models import (
    MAX_ENTRY_MINUTES,
    SUPPLEMENT_WINDOW_WEEKS,
    EntryType,
    TimesheetStatus,
    monday_of,
    supplement_weeks_left,
)
from app.domain.timesheet.service import ENTITY_TYPE
from app.main import app
from app.repositories.approval import PostgresApprovalRepository
from tests.support.platform import Actor, Platform

#: The Monday this run happens in. The window is measured from it, on both sides.
CURRENT_WEEK = monday_of(madrid_today(datetime.now(UTC)))

#: The week the correction tests work in: in the past, inside the window, and far
#: enough from its edge that a slow run cannot walk out of it.
WEEK = CURRENT_WEEK - timedelta(weeks=2)

#: A Monday outside the window, where an approved week is a *stored* fact rather than
#: something the product could produce: a week this old cannot be written at all, so
#: nothing could ever have filed and approved it. Inserted directly for exactly that
#: reason — the test is about what happens to history, not about how it was made.
CLOSED_WEEK = CURRENT_WEEK - timedelta(weeks=SUPPLEMENT_WINDOW_WEEKS + 6)

#: Every write route under `/api/v1/timesheets`, as (method, path). `test_every_write_
#: route_is_enumerated_here` compares this with the router, which is what turns "a new
#: route must think about the lock" from a convention into a failing test.
WRITE_ROUTES = frozenset(
    {
        ("POST", "/api/v1/timesheets/entries"),
        ("PATCH", "/api/v1/timesheets/entries/{entry_id}"),
        ("DELETE", "/api/v1/timesheets/entries/{entry_id}"),
        ("POST", "/api/v1/timesheets/copy-previous"),
        ("POST", "/api/v1/timesheets/submit"),
        ("POST", "/api/v1/timesheets/supplements"),
    }
)


@dataclass(slots=True, frozen=True)
class Cast:
    """The people and places these tests move between."""

    employee: Actor
    manager_actor: Actor
    other_hr: UUID
    department: str
    position: str
    admin: Actor
    prefix: str


@pytest.fixture
async def cast(platform: Platform) -> Cast:
    """One employee, their manager, and a second holder of `hr` to decide level two."""
    suffix = uuid4().hex[:8]
    department = await platform.department(f"bloqueo{suffix}")
    position = await platform.position(department, f"tecnico{suffix}")

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

    other_hr = await platform.grant_account(roles=("hr",), sign_in=False)
    return Cast(
        employee=employee,
        manager_actor=manager_actor,
        other_hr=UUID(other_hr.employee_id),
        department=department,
        position=position,
        admin=await platform.account(roles=("admin",)),
        prefix=suffix,
    )


# --- helpers ----------------------------------------------------------------


async def make_target(cast: Cast, **project: object) -> tuple[dict, dict]:
    """A project and one of its tasks, both created through the endpoints."""
    created = await cast.admin.post(
        "/api/v1/projects",
        json={
            "code": f"lock{cast.prefix}{uuid4().hex[:4]}",
            "name_es": "Proyecto",
            "name_en": "Project",
            "department_id": cast.department,
            "start_date": "2020-01-01",
            "end_date": None,
            "status": "active",
            "is_billable_default": True,
            **project,
        },
    )
    assert created.status_code == 201, created.text
    task = await cast.admin.post(
        f"/api/v1/projects/{created.json()['id']}/tasks",
        json={"code": f"t{uuid4().hex[:6]}", "name_es": "Tarea", "name_en": "Task"},
    )
    assert task.status_code == 201, task.text
    return created.json(), task.json()


async def add_entry(
    actor: Actor,
    *,
    week: date,
    project_id: str,
    task_id: str,
    minutes: int,
    entry_date: str | None = None,
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


async def read_week(actor: Actor, *, week: date):
    return await actor.get("/api/v1/timesheets/week", params={"week": week.isoformat()})


async def submit(actor: Actor, *, week: date):
    return await actor.post("/api/v1/timesheets/submit", params={"week": week.isoformat()})


async def supplement(actor: Actor, *, week: date, corrections: list[dict]):
    return await actor.post(
        "/api/v1/timesheets/supplements",
        params={"week": week.isoformat()},
        json={"corrections": corrections},
    )


async def sheet_id(platform: Platform, *, week: date, supplementary: bool | None = None) -> UUID:
    clause = {
        None: "",
        False: " AND supersedes_timesheet_id IS NULL",
        True: " AND supersedes_timesheet_id IS NOT NULL",
    }[supplementary]
    found = await platform.scalar(
        f"SELECT id FROM timesheets WHERE week_start = :week{clause} ORDER BY created_at LIMIT 1",
        {"week": week},
    )
    assert found is not None, f"no sheet for the week of {week} (supplementary={supplementary})"
    return UUID(str(found))


async def decide(
    platform: Platform,
    cast: Cast,
    *,
    week: date,
    supplementary: bool = False,
    decision: DecisionKind = DecisionKind.APPROVE,
    comment: str | None = None,
) -> None:
    """Drive the engine the way an approval inbox will, on the sheet asked for.

    The sheet is named rather than looked up by week: a corrected week has two, each
    with a request of its own, and deciding "the week" would after a supplement decide
    whichever row the query happened to return first.
    """
    async with platform.factory() as session:
        engine = ApprovalService(PostgresApprovalRepository(session), session)
        target = await sheet_id(platform, week=week, supplementary=supplementary)
        state = await engine.state_of(ENTITY_TYPE, target)
        assert state is not None, "that sheet was never filed"
        await engine.decide(state.id, UUID(cast.manager_actor.employee_id), decision, comment)
        if decision is DecisionKind.APPROVE:
            await engine.decide(state.id, cast.other_hr, decision, comment)


async def approved_week(platform: Platform, cast: Cast, *, minutes: int = 480) -> dict:
    """A week of this employee's that two levels have approved — a locked week.

    Returns the entry the tests correct: the whole point of a locked week is that its
    rows are the ones a supplement has to work around. The week is *read* before it is
    returned, which is what applies the engine's answer to the stored status — a
    decision taken in the engine is only the week's until somebody reads it.
    """
    project, task = await make_target(cast)
    created = await add_entry(
        cast.employee, week=WEEK, project_id=project["id"], task_id=task["id"], minutes=minutes
    )
    assert created.status_code == 201, created.text
    assert (await submit(cast.employee, week=WEEK)).status_code == 200
    await decide(platform, cast, week=WEEK)
    read = await read_week(cast.employee, week=WEEK)
    assert read.json()["is_locked"] is True, read.text
    entry = created.json()["days"][0]["entries"][0]
    entry.update(project=project, task=task)
    return entry


# --- the lock: approved means nobody writes again ----------------------------


def test_every_write_route_is_enumerated_here() -> None:
    """The list of write routes is the router's, not this file's opinion of it.

    The global week lock and the permanent lock are only as good as the number of
    paths that check them, and a route added later is the way a rule like that quietly
    stops being true. This test fails the moment the surface grows, which is the point:
    the new route has to be added to `WRITE_ROUTES` and to the tests below it.
    """
    declared = {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        if path.startswith("/api/v1/timesheets")
        for method in operations
        if method.upper() not in {"GET", "HEAD", "OPTIONS"}
    }

    assert declared == set(WRITE_ROUTES), (
        "the timesheet write surface changed: add the route to WRITE_ROUTES and cover "
        "it in the lock tests"
    )


async def test_an_approved_week_is_locked_and_every_write_path_refuses(
    platform: Platform, cast: Cast
) -> None:
    """**审批通过后状态变为已锁定，无法直接编辑任何条目.**

    Every write route, not the one that was easiest to guard: the ticket's word is
    "no write path", and a week that could still be edited through the copy endpoint or
    by filing it again would not be locked. Each refusal carries the lock's own code —
    not `TIMESHEET_NOT_EDITABLE` — because the remedy differs: this week is corrected,
    not returned.
    """
    entry = await approved_week(platform, cast)
    body = (await read_week(cast.employee, week=WEEK)).json()

    assert body["status"] == TimesheetStatus.APPROVED.value
    assert body["is_locked"] is True
    assert body["is_editable"] is False, "a locked week offers nothing to write in"

    attempts = [
        await add_entry(
            cast.employee,
            week=WEEK,
            project_id=entry["project"]["id"],
            task_id=entry["task"]["id"],
            minutes=60,
            entry_date=(WEEK + timedelta(days=1)).isoformat(),
        ),
        await cast.employee.patch(
            f"/api/v1/timesheets/entries/{entry['id']}",
            params={"week": WEEK.isoformat()},
            json={"minutes": 30},
        ),
        await cast.employee.delete(
            f"/api/v1/timesheets/entries/{entry['id']}", params={"week": WEEK.isoformat()}
        ),
        await cast.employee.post(
            "/api/v1/timesheets/copy-previous", params={"week": WEEK.isoformat()}
        ),
        await submit(cast.employee, week=WEEK),
    ]

    assert [response.status_code for response in attempts] == [409] * len(attempts), [
        response.text for response in attempts
    ]
    assert {response.json()["error"]["code"] for response in attempts} == {
        ErrorCode.TIMESHEET_WEEK_LOCKED.value
    }
    assert (await read_week(cast.employee, week=WEEK)).json()["entries_total_minutes"] == 480, (
        "a refused write changed the week anyway"
    )


async def test_the_lock_holds_at_the_database_for_a_writer_that_never_asks(
    platform: Platform, cast: Cast
) -> None:
    """The service refuses first; `time_entries_guard_week_lock` is the backstop.

    A console, a script or a future endpoint that writes the table directly is the
    case a service check cannot cover, and it is the case that matters: an approved
    week is the record of what the company billed for. Insert, update and delete are
    all refused, and each names the reason.
    """
    entry = await approved_week(platform, cast)

    inserted = await platform.refused_by_database(
        """
        INSERT INTO timesheet_entries
            (id, timesheet_id, employee_id, week_start, entry_date,
             project_id, task_id, minutes, is_billable)
        SELECT :id, t.id, t.employee_id, t.week_start, :day, :project_id, :task_id, 60, true
          FROM timesheets t
         WHERE t.week_start = :week AND t.supersedes_timesheet_id IS NULL
        """,
        {
            "id": uuid4(),
            "day": WEEK.isoformat(),
            "project_id": entry["project"]["id"],
            "task_id": entry["task"]["id"],
            "week": WEEK,
        },
    )
    updated = await platform.refused_by_database(
        "UPDATE timesheet_entries SET minutes = 60 WHERE id = :id", {"id": entry["id"]}
    )
    deleted = await platform.refused_by_database(
        "DELETE FROM timesheet_entries WHERE id = :id", {"id": entry["id"]}
    )

    for refusal in (inserted, updated, deleted):
        assert "is approved" in refusal, refusal
    assert await platform.scalar(
        "SELECT minutes FROM timesheet_entries WHERE id = :id", {"id": entry["id"]}
    ) == 480


async def test_apply_decision_writes_the_engines_answer_onto_the_week(
    platform: Platform, cast: Cast
) -> None:
    """`apply_decision`, ticket 28's open step: the week catches up on demand.

    Driven through the service rather than through a page, because that is what a
    future approval inbox does: the manager decides in the engine, and this is the call
    that moves the document on the strength of it. Twice, to show it is idempotent and
    that a second call writes no second record.
    """
    from app.api.v1.timesheets import _service
    from app.domain.access.snapshot import resolve_principal

    project, task = await make_target(cast)
    assert (
        await add_entry(
            cast.employee, week=WEEK, project_id=project["id"], task_id=task["id"], minutes=240
        )
    ).status_code == 201
    assert (await submit(cast.employee, week=WEEK)).status_code == 200

    # Behind the module's back: the engine decides, nothing reads the week yet.
    await decide(platform, cast, week=WEEK)
    assert await platform.scalar(
        "SELECT status FROM timesheets WHERE week_start = :week", {"week": WEEK}
    ) == "pending", "the stored status moved without apply_decision"

    async with platform.factory() as session:
        principal = await resolve_principal(session, UUID(cast.employee.user_id))
        assert principal is not None
        service = _service(session, principal)
        view = await service.apply_decision(WEEK)
        assert view.status is TimesheetStatus.APPROVED
        assert view.is_locked is True
        # Idempotent: nothing left to move, so nothing is written.
        again = await service.apply_decision(WEEK)
        assert again.status is TimesheetStatus.APPROVED

    recorded = await platform.sql(
        "SELECT before -> 'status', after -> 'status', initiated_by FROM audit_log "
        "WHERE action = 'timesheet.decision_applied'"
    )
    assert recorded == [("pending", "approved", "system")], recorded


# --- the supplement: a new sheet, a reversal, and a replacement ---------------


async def test_a_supplement_writes_a_reversal_and_a_replacement_and_the_net_is_the_difference(
    platform: Platform, cast: Cast
) -> None:
    """**被修改的原条目生成冲销的负值条目，同时写入新的正值条目.**

    The ticket's arithmetic, asserted as the ticket words it:
    `original + reversal + new = expected`. Three rows, none of them an edit of
    another, and the day's net is what a reader takes a day's total to mean.
    """
    entry = await approved_week(platform, cast, minutes=480)

    response = await supplement(
        cast.employee,
        week=WEEK,
        corrections=[{"entry_id": entry["id"], "minutes": 300, "note": "media jornada"}],
    )

    assert response.status_code == 201, response.text
    body = response.json()
    rows = await platform.sql(
        "SELECT entry_type, minutes, reverses_entry_id, note FROM timesheet_entries "
        "WHERE week_start = :week ORDER BY created_at",
        {"week": WEEK},
    )
    assert [(row[0], row[1]) for row in rows] == [
        ("normal", 480),
        ("reversal", -480),
        ("normal", 300),
    ], rows
    assert str(rows[1][2]) == entry["id"], "the reversal does not name the entry it cancels"
    assert rows[2][3] == "media jornada"

    # 原 + 冲销 + 新增 = 期望值, literally.
    original, reversal, replacement = (row[1] for row in rows)
    assert original + reversal + replacement == 300
    assert body["entries_total_minutes"] == 300
    assert body["days"][0]["total_minutes"] == 300
    assert body["days"][0]["gross_minutes"] == 780
    assert body["days"][0]["reversal_minutes"] == 480
    assert body["gross_total_minutes"] == 780
    assert body["reversal_total_minutes"] == 480

    # ... and the same net per task, which is the other half of the net view.
    assert [
        (task["gross_minutes"], task["reversal_minutes"], task["net_minutes"])
        for task in body["tasks"]
    ] == [(780, 480, 300)]

    # The reversal is not editable and neither is what it reversed: the pair cancels
    # because both halves are each other's negation, and an edit breaks that.
    reversal_entry = next(
        row for row in body["days"][0]["entries"] if row["entry_type"] == EntryType.REVERSAL
    )
    edited = await cast.employee.patch(
        f"/api/v1/timesheets/entries/{reversal_entry['id']}",
        params={"week": WEEK.isoformat()},
        json={"minutes": 480},
    )
    removed = await cast.employee.delete(
        f"/api/v1/timesheets/entries/{reversal_entry['id']}",
        params={"week": WEEK.isoformat()},
    )
    assert [edited.status_code, removed.status_code] == [409, 409], (edited.text, removed.text)
    assert {edited.json()["error"]["code"], removed.json()["error"]["code"]} == {
        ErrorCode.TIMESHEET_ENTRY_IS_REVERSAL.value
    }
    # Every one of those refusals is a route the caller reaches through the *open*
    # sheet, so the locked original is not the reason they were refused.
    assert await platform.scalar("SELECT count(*) FROM timesheet_entries") == 3


async def test_the_database_refuses_a_reversal_that_does_not_negate_its_original(
    platform: Platform, cast: Cast
) -> None:
    """The pair is a database fact, not a convention the service remembers.

    Three ways to break it, all refused by `time_entries_guard_reversal`: a reversal
    that is not the exact negation, a reversal of a reversal, and a change to an entry
    that has already been reversed.
    """
    entry = await approved_week(platform, cast, minutes=480)
    assert (
        await supplement(
            cast.employee, week=WEEK, corrections=[{"entry_id": entry["id"], "minutes": 300}]
        )
    ).status_code == 201
    reversal_id = await platform.scalar(
        "SELECT id FROM timesheet_entries WHERE entry_type = 'reversal'"
    )
    supplement_sheet = await sheet_id(platform, week=WEEK, supplementary=True)

    wrong_amount = await platform.refused_by_database(
        """
        INSERT INTO timesheet_entries
            (id, timesheet_id, employee_id, week_start, entry_date, project_id, task_id,
             minutes, is_billable, entry_type, reverses_entry_id)
        SELECT :id, :sheet, t.employee_id, t.week_start, t.entry_date, t.project_id,
               t.task_id, -60, t.is_billable, 'reversal', t.id
          FROM timesheet_entries t WHERE t.id = :original
        """,
        {"id": uuid4(), "sheet": supplement_sheet, "original": entry["id"]},
    )
    reversed_reversal = await platform.refused_by_database(
        """
        INSERT INTO timesheet_entries
            (id, timesheet_id, employee_id, week_start, entry_date, project_id, task_id,
             minutes, is_billable, entry_type, reverses_entry_id)
        SELECT :id, :sheet, t.employee_id, t.week_start, t.entry_date, t.project_id,
               t.task_id, -480, t.is_billable, 'reversal', t.id
          FROM timesheet_entries t WHERE t.id = :reversal
        """,
        {"id": uuid4(), "sheet": supplement_sheet, "reversal": reversal_id},
    )
    changed_original = await platform.refused_by_database(
        "UPDATE timesheet_entries SET minutes = 60 WHERE id = :id", {"id": entry["id"]}
    )

    assert "must be" in wrong_amount, wrong_amount
    assert "is itself a reversal" in reversed_reversal, reversed_reversal
    assert "was reversed and may not change" in changed_original, changed_original
    assert await platform.scalar(
        "SELECT minutes FROM timesheet_entries WHERE id = :id", {"id": entry["id"]}
    ) == 480


async def test_an_unwritten_week_is_editable_and_names_no_sheet_yet(
    platform: Platform, cast: Cast
) -> None:
    """The control that keeps the lock from closing weeks nobody has written.

    A week with no sheet at all is writable — the first write is what creates it — so
    `is_editable` cannot be "there is an open sheet". `editable_timesheet_id` is null
    there and says *which* sheet a write would land in, which is a different question
    from whether one is allowed; without this test, a grid with no "add hours" cell on
    an empty week would look like a working lock.
    """
    empty = (await read_week(cast.employee, week=CURRENT_WEEK + timedelta(weeks=2))).json()

    assert empty["is_editable"] is True
    assert empty["has_timesheet"] is False
    assert empty["editable_timesheet_id"] is None
    assert empty["sheets"] == []
    assert empty["is_locked"] is False
    assert empty["week_closed"] is False


async def test_the_link_is_readable_from_both_ends(platform: Platform, cast: Cast) -> None:
    """**原工时表与新工时表之间的关联双向可查；原始记录永久保留且可追溯.**

    Both directions, and the original frozen while it happens: the week lists the
    supplements filed against it, each supplement names the sheet it corrects, and the
    original's own rows are the ones the approver signed — same ids, same minutes, same
    update timestamp, because nothing about a correction touches them.
    """
    entry = await approved_week(platform, cast, minutes=240)
    before = await platform.sql(
        "SELECT minutes, updated_at FROM timesheet_entries WHERE id = :id", {"id": entry["id"]}
    )
    original_sheet = await sheet_id(platform, week=WEEK, supplementary=False)

    created = await supplement(
        cast.employee, week=WEEK, corrections=[{"entry_id": entry["id"], "minutes": 120}]
    )
    assert created.status_code == 201, created.text
    supplement_sheet = await sheet_id(platform, week=WEEK, supplementary=True)

    # Week -> its supplements, and supplement -> the sheet it corrects.
    body = (await read_week(cast.employee, week=WEEK)).json()
    assert [row["timesheet_id"] for row in body["supplements"]] == [str(supplement_sheet)]
    assert body["supplements"][0]["corrects_timesheet_id"] == str(original_sheet)
    assert [row["timesheet_id"] for row in body["sheets"]] == [
        str(original_sheet),
        str(supplement_sheet),
    ]
    # The status read carries the same link, per sheet, with each one's own history.
    status = (
        await cast.employee.get("/api/v1/timesheets/week/status", params={"week": WEEK.isoformat()})
    ).json()
    assert [
        (row["timesheet_id"], row["is_supplementary"], row["corrects_timesheet_id"])
        for row in status["sheets"]
    ] == [
        (str(original_sheet), False, None),
        (str(supplement_sheet), True, str(original_sheet)),
    ]
    # The supplements share the week they correct, which is what makes one week one
    # grid: the Monday is the key, and the link is the document.
    assert {row["week_start"] for row in body["supplements"]} == {WEEK.isoformat()}

    # 原始记录永久保留: not one row of the original moved, and it is still readable.
    after = await platform.sql(
        "SELECT minutes, updated_at FROM timesheet_entries WHERE id = :id", {"id": entry["id"]}
    )
    assert after == before, "the supplement edited the original"
    assert body["has_timesheet"] is True
    assert body["days"][0]["entries"][0]["minutes"] == 240
    assert body["days"][0]["entries"][0]["id"] == entry["id"]


async def test_a_supplement_goes_through_both_levels_and_locks_when_approved(
    platform: Platform, cast: Cast
) -> None:
    """**补充提交同样走两级审批，通过后净额正确.**

    The correction is a document: it is filed by the same route, the manager decides
    level one, HR decides level two, and the week is not finished until both have. A
    net that is only right while a supplement is a draft is not a net the payroll
    report ticket 30 can sum, so the assertion is made at the end, on the approved
    week.
    """
    entry = await approved_week(platform, cast, minutes=480)
    assert (
        await supplement(
            cast.employee, week=WEEK, corrections=[{"entry_id": entry["id"], "minutes": 360}]
        )
    ).status_code == 201
    supplement_sheet = await sheet_id(platform, week=WEEK, supplementary=True)

    filed = await submit(cast.employee, week=WEEK)

    assert filed.status_code == 200, filed.text
    assert filed.json()["status"] == "approved", "the original's status was overwritten"
    assert filed.json()["entries_total_minutes"] == 360
    # The original's lock is untouched and the correction is what is pending.
    assert filed.json()["supplements"][0]["status"] == TimesheetStatus.PENDING.value
    assert await platform.scalar(
        "SELECT status FROM timesheets WHERE id = :id", {"id": supplement_sheet}
    ) == "pending"

    # Filing the correction told the manager, through the wrapped engine: nothing in
    # the timesheet module sends a notification of its own.
    told = await platform.sql(
        "SELECT recipient_employee_id, entity_id FROM notifications ORDER BY created_at"
    )
    assert [str(row[0]) for row in told][-1] == cast.manager_actor.employee_id
    assert str(told[-1][1]) == str(supplement_sheet)

    await decide(platform, cast, week=WEEK, supplementary=True, comment="corregido")

    final = (await read_week(cast.employee, week=WEEK)).json()
    assert final["status"] == TimesheetStatus.APPROVED.value
    assert final["supplements"][0]["status"] == TimesheetStatus.APPROVED.value
    assert final["entries_total_minutes"] == 360
    assert final["days"][0]["total_minutes"] == 360
    assert final["gross_total_minutes"] == 840
    assert final["reversal_total_minutes"] == 480
    # And the closed net is what the database sums, not only what the response says.
    assert await platform.scalar(
        "SELECT sum(minutes) FROM timesheet_entries WHERE week_start = :week", {"week": WEEK}
    ) == 360
    # A second correction of the same week is allowed once the first is decided — the
    # week's net simply moves again.
    assert (await read_week(cast.employee, week=WEEK)).json()["can_supplement"] is True


async def test_a_correction_that_changes_nothing_is_refused(platform: Platform, cast: Cast) -> None:
    """Three refusals around the document itself, each with its own code.

    An empty correction list, an entry that is not this week's, and the same entry
    named twice: all three are documents with nothing to approve, and the codes differ
    because the remedies do — "state a correction" and "that is not your entry".
    """
    entry = await approved_week(platform, cast)

    empty = await cast.employee.post(
        "/api/v1/timesheets/supplements",
        params={"week": WEEK.isoformat()},
        json={"corrections": []},
    )
    unknown = await supplement(
        cast.employee, week=WEEK, corrections=[{"entry_id": str(uuid4()), "minutes": 60}]
    )
    twice = await supplement(
        cast.employee,
        week=WEEK,
        corrections=[
            {"entry_id": entry["id"], "minutes": 60},
            {"entry_id": entry["id"], "minutes": 120},
        ],
    )

    assert empty.status_code == 422, empty.text
    assert empty.json()["error"]["code"] == ErrorCode.VALIDATION_FAILED.value
    for response in (unknown, twice):
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == ErrorCode.TIMESHEET_SUPPLEMENT_INVALID.value
    assert await platform.scalar("SELECT count(*) FROM timesheets") == 1, (
        "a refused supplement left a sheet behind"
    )


async def test_a_draft_week_is_not_supplemented_and_an_open_one_is_not_supplemented_twice(
    platform: Platform, cast: Cast
) -> None:
    """The two state refusals: nothing to correct, and nothing to correct it with.

    A draft week is the employee's to edit, which is a shorter path than a correction
    document with an approval round of its own; and a week with a correction already in
    flight has no stable figure for a second one to be written against.
    """
    entry = await approved_week(platform, cast)

    second = await supplement(
        cast.employee, week=WEEK, corrections=[{"entry_id": entry["id"], "minutes": 60}]
    )
    assert second.status_code == 201, second.text
    in_flight = await supplement(
        cast.employee, week=WEEK, corrections=[{"entry_id": entry["id"], "minutes": 120}]
    )
    assert in_flight.status_code == 409, in_flight.text
    assert in_flight.json()["error"]["code"] == ErrorCode.TIMESHEET_SUPPLEMENT_OPEN.value

    # The control: an ordinary draft week refuses a supplement for the other reason.
    project, task = await make_target(cast)
    draft_week = CURRENT_WEEK - timedelta(weeks=1)
    created = await add_entry(
        cast.employee, week=draft_week, project_id=project["id"], task_id=task["id"], minutes=60
    )
    assert created.status_code == 201, created.text
    draft_entry = created.json()["days"][0]["entries"][0]
    refused = await supplement(
        cast.employee, week=draft_week, corrections=[{"entry_id": draft_entry["id"], "minutes": 30}]
    )
    assert refused.status_code == 409, refused.text
    assert (
        refused.json()["error"]["code"] == ErrorCode.TIMESHEET_SUPPLEMENT_NOT_LOCKED.value
    )


async def test_a_supplement_obeys_the_same_project_guards_as_an_entry(
    platform: Platform, cast: Cast
) -> None:
    """A correction writes entries, so it satisfies the guards rather than bypassing them.

    The replacement row names a target like any other entry, and `_recordable` is the
    same call `add_entry` makes: an archived project is refused, so a correction cannot
    be used to book time somewhere nothing may be booked. The reversal is not the
    subject here — it points at a locked row and is refused by the same rule one step
    earlier.
    """
    entry = await approved_week(platform, cast)
    # A project built while it was still allowed to have tasks, then withdrawn: the
    # correction is written after it stopped accepting time, which is the case the
    # guard exists for.
    archived, archived_task = await make_target(cast)
    assert (
        await cast.admin.patch(
            f"/api/v1/projects/{archived['id']}", json={"status": "archived"}
        )
    ).status_code == 200

    refused = await supplement(
        cast.employee,
        week=WEEK,
        corrections=[
            {
                "entry_id": entry["id"],
                "minutes": 60,
                "project_id": archived["id"],
                "task_id": archived_task["id"],
            }
        ],
    )

    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == ErrorCode.PROJECT_TASK_NOT_RECORDABLE.value
    assert await platform.scalar("SELECT count(*) FROM timesheet_entries") == 1, (
        "the refused correction wrote its pair anyway"
    )


# --- the window and the global week lock -------------------------------------


async def test_a_week_outside_the_window_is_refused_with_the_weeks_that_remain(
    platform: Platform, cast: Cast
) -> None:
    """**补填窗口为最近 8 周，超出窗口的周被拒绝并给出可读提示与剩余可补周数.**

    The count is in the detail and the weekday is the key the client renders from, so
    the sentence a person reads comes from the catalogue in their own language while
    the number stays the server's. The read reports the same count for a week inside
    the window, which is what the screen uses to offer the correction at all.
    """
    # A locked week that has since fallen out of the window. Inserted rather than
    # produced: a week this old cannot be written, so nothing could have filed it.
    old_sheet = uuid4()
    await platform.sql(
        """
        INSERT INTO timesheets (id, employee_id, week_start, status, is_supplementary)
        VALUES (:id, :employee_id, :week, 'approved', false)
        """,
        {"id": old_sheet, "employee_id": cast.employee.employee_id, "week": CLOSED_WEEK},
    )

    response = await supplement(
        cast.employee, week=CLOSED_WEEK, corrections=[{"entry_id": str(uuid4()), "minutes": 60}]
    )

    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == ErrorCode.TIMESHEET_WEEK_CLOSED.value
    assert error["message_key"] == "errors.timesheet_week_closed"
    assert f"0 of {SUPPLEMENT_WINDOW_WEEKS} weeks remain" in error["detail"], error["detail"]
    assert str(CLOSED_WEEK) in error["detail"]

    # The control: the same request against a week inside the window is answered with
    # weeks to spare rather than refused, and the read says how many.
    inside = await read_week(cast.employee, week=WEEK)
    assert inside.json()["supplement_weeks_left"] > 0
    assert inside.json()["week_closed"] is False
    outside = (await read_week(cast.employee, week=CLOSED_WEEK)).json()
    assert outside["week_closed"] is True
    # The closed week is reported as unwritable too, so a screen cannot offer a cell
    # whose write the API would refuse.
    assert outside["is_editable"] is False
    assert outside["can_supplement"] is False
    assert supplement_weeks_left(CLOSED_WEEK, CURRENT_WEEK) == 0
    assert supplement_weeks_left(WEEK, CURRENT_WEEK) == SUPPLEMENT_WINDOW_WEEKS - 2
    assert supplement_weeks_left(CURRENT_WEEK + timedelta(weeks=4), CURRENT_WEEK) == (
        SUPPLEMENT_WINDOW_WEEKS
    ), "a week in the future is never closed by the window"


async def test_outside_the_window_no_write_path_may_touch_the_week(
    platform: Platform, cast: Cast
) -> None:
    """**全局周锁机制存在：超出补填窗口的周任何写入路径都无法触及.**

    Every write route in `WRITE_ROUTES`, refused with the window's own code — not only
    the supplement endpoint, which is the half a rule like this usually covers. The
    attempt also *closes* the week: the row this refusal writes is what
    `time_entries_guard_week_lock` reads afterwards, so a console writing the table
    directly is refused by the same fact rather than by a second opinion about the
    clock.
    """
    project, task = await make_target(cast)
    mine = {"week": CLOSED_WEEK.isoformat()}
    stranger = str(uuid4())

    attempts = {
        ("POST", "/api/v1/timesheets/entries"): await cast.employee.post(
            "/api/v1/timesheets/entries",
            params=mine,
            json={
                "entry_date": CLOSED_WEEK.isoformat(),
                "project_id": project["id"],
                "task_id": task["id"],
                "minutes": 60,
            },
        ),
        ("PATCH", "/api/v1/timesheets/entries/{entry_id}"): await cast.employee.patch(
            f"/api/v1/timesheets/entries/{stranger}", params=mine, json={"minutes": 60}
        ),
        ("DELETE", "/api/v1/timesheets/entries/{entry_id}"): await cast.employee.delete(
            f"/api/v1/timesheets/entries/{stranger}", params=mine
        ),
        ("POST", "/api/v1/timesheets/copy-previous"): await cast.employee.post(
            "/api/v1/timesheets/copy-previous", params=mine
        ),
        ("POST", "/api/v1/timesheets/submit"): await submit(cast.employee, week=CLOSED_WEEK),
        ("POST", "/api/v1/timesheets/supplements"): await supplement(
            cast.employee, week=CLOSED_WEEK, corrections=[{"entry_id": stranger, "minutes": 60}]
        ),
    }

    assert set(attempts) == set(WRITE_ROUTES), "a write route is missing from this test"
    for route, response in attempts.items():
        assert response.status_code == 409, (route, response.text)
        assert response.json()["error"]["code"] == ErrorCode.TIMESHEET_WEEK_CLOSED.value, route

    # The refusal recorded the closing, with a timestamp and the reason.
    lock = await platform.sql(
        "SELECT week_start, locked_by_employee_id, reason FROM timesheet_weeks_lock "
        "WHERE week_start = :week",
        {"week": CLOSED_WEEK},
    )
    assert len(lock) == 1, "the refused write did not close the week"
    assert lock[0][1] is None, "a week the system closed was attributed to the caller"
    assert "window" in lock[0][2]
    # One row, not one per attempt: closing a week is idempotent.
    refusals = await platform.scalar(
        "SELECT count(*) FROM audit_log WHERE action = 'timesheet.week_locked'"
    )
    assert refusals == len(attempts)

    # And now the database refuses it too, for a writer that asks nothing.
    waiting = uuid4()
    await platform.sql(
        """
        INSERT INTO timesheets (id, employee_id, week_start, status, is_supplementary)
        VALUES (:id, :employee_id, :week, 'draft', false)
        """,
        {"id": waiting, "employee_id": cast.employee.employee_id, "week": CLOSED_WEEK},
    )
    inserted = await platform.refused_by_database(
        """
        INSERT INTO timesheet_entries
            (id, timesheet_id, employee_id, week_start, entry_date, project_id, task_id,
             minutes, is_billable)
        VALUES (:id, :sheet, :employee_id, :week, :week, :project_id, :task_id, 60, true)
        """,
        {
            "id": uuid4(),
            "sheet": waiting,
            "employee_id": cast.employee.employee_id,
            "week": CLOSED_WEEK,
            "project_id": project["id"],
            "task_id": task["id"],
        },
    )
    assert "closed to every write" in inserted, inserted


async def test_the_lock_is_a_lower_bound_and_next_week_is_still_writable(
    platform: Platform, cast: Cast
) -> None:
    """The window closes history; it does not stop anybody planning.

    The control for every refusal above: a future week is written exactly as a current
    one is, which is why the window is a lower bound rather than a range. Without this,
    a rule that refused every week except the current one would pass the tests above.
    """
    project, task = await make_target(cast)
    next_week = CURRENT_WEEK + timedelta(weeks=1)

    written = await add_entry(
        cast.employee, week=next_week, project_id=project["id"], task_id=task["id"], minutes=120
    )

    assert written.status_code == 201, written.text
    assert written.json()["status"] == "draft"
    assert written.json()["is_editable"] is True
    assert written.json()["week_closed"] is False
    assert await platform.scalar(
        "SELECT count(*) FROM timesheet_weeks_lock WHERE week_start = :week", {"week": next_week}
    ) == 0


async def test_opening_a_correction_closes_the_weeks_that_have_fallen_out_of_the_window(
    platform: Platform, cast: Cast
) -> None:
    """The window's own bookkeeping, kept where the feature that needs it runs.

    A week nobody wrote does not need a row saying it is closed — it is unwritable by
    construction. A week the employee *has* written and let fall out of the window is
    closed when they next open a correction, so the row exists before somebody reaches
    for a console, and it is closed once rather than on every attempt.
    """
    entry = await approved_week(platform, cast)
    stale = CURRENT_WEEK - timedelta(weeks=SUPPLEMENT_WINDOW_WEEKS + 2)
    await platform.sql(
        """
        INSERT INTO timesheets (id, employee_id, week_start, status, is_supplementary)
        VALUES (:id, :employee_id, :week, 'draft', false)
        """,
        {"id": uuid4(), "employee_id": cast.employee.employee_id, "week": stale},
    )

    opened = await supplement(
        cast.employee, week=WEEK, corrections=[{"entry_id": entry["id"], "minutes": 420}]
    )

    assert opened.status_code == 201, opened.text
    assert await platform.scalar(
        "SELECT count(*) FROM timesheet_weeks_lock WHERE week_start = :week", {"week": stale}
    ) == 1
    recorded = await platform.sql(
        "SELECT after -> 'weeks_closed' FROM audit_log WHERE action = 'timesheet.supplement_opened'"
    )
    assert recorded[0][0] == [stale.isoformat()]


async def test_a_supplement_states_which_week_it_corrects_in_the_audit_trail(
    platform: Platform, cast: Cast
) -> None:
    """Every state change here is traceable, including the pair that was written.

    One record for the document — which sheet, against which original, and what each
    correction asked for — and one for each row, because a reader filtering
    `timesheet.entry_written` has to see the reversal that cancelled an approved week's
    hours. The decision records are the engine's: the same entity id, its own action.
    """
    entry = await approved_week(platform, cast, minutes=480)
    assert (
        await supplement(
            cast.employee, week=WEEK, corrections=[{"entry_id": entry["id"], "minutes": 300}]
        )
    ).status_code == 201
    assert (await submit(cast.employee, week=WEEK)).status_code == 200
    await decide(platform, cast, week=WEEK, supplementary=True)

    original_sheet = await sheet_id(platform, week=WEEK, supplementary=False)
    supplement_sheet = await sheet_id(platform, week=WEEK, supplementary=True)
    # The read is what writes the engine's answer onto the correction's own sheet, so
    # the transition below is this module's record and not the engine's.
    applied_view = await read_week(cast.employee, week=WEEK)
    assert applied_view.json()["supplements"][0]["status"] == "approved"

    opened = await platform.sql(
        "SELECT entity_id, after -> 'supersedes_timesheet_id', after -> 'corrections' "
        "FROM audit_log WHERE action = 'timesheet.supplement_opened'"
    )
    assert str(opened[0][0]) == str(supplement_sheet)
    assert str(opened[0][1]) == str(original_sheet)
    assert opened[0][2] == [
        {
            "reverses_entry_id": entry["id"],
            "before_minutes": 480,
            "after_minutes": 300,
            "replacement_entry_id": opened[0][2][0]["replacement_entry_id"],
        }
    ]

    reversals = await platform.sql(
        "SELECT after -> 'entry_type', after -> 'minutes' FROM audit_log "
        "WHERE action = 'timesheet.entry_written' AND after ->> 'entry_type' = 'reversal'"
    )
    assert reversals == [("reversal", -480)]

    # The engine's own record is keyed on a sheet, not on the week: the original's two
    # decisions and the correction's two, distinguishable by entity id.
    decided = await platform.sql(
        "SELECT entity_id, after -> 'decision' FROM audit_log WHERE action = 'approval.decided' "
        "ORDER BY id"
    )
    by_sheet: dict[str, list[str]] = {}
    for entity_id, decision in decided:
        by_sheet.setdefault(str(entity_id), []).append(decision)
    assert by_sheet == {
        str(original_sheet): ["approved", "approved"],
        str(supplement_sheet): ["approved", "approved"],
    }, by_sheet
    # ... and this module recorded the correction's transition when it read it back.
    applied = await platform.sql(
        "SELECT entity_id, before -> 'status', after -> 'status' FROM audit_log "
        "WHERE action = 'timesheet.decision_applied' ORDER BY id"
    )
    assert [str(row[0]) for row in applied] == [str(original_sheet), str(supplement_sheet)]
    assert applied[-1][1:] == ("pending", "approved")


async def test_copying_a_corrected_week_copies_what_stands_and_not_the_adjustments(
    platform: Platform, cast: Cast
) -> None:
    """A copy of a week that was corrected is a copy of the week as it now reads.

    Two corrections of the two kinds: one entry restated, one cancelled outright. The
    reversal is deliberately not copied — it is a statement about *that* week's history,
    and carrying it into another week would cancel an entry that week never had — and
    neither is the cancelled entry, which no longer stands.
    """
    project, task = await make_target(cast)
    monday = 480
    tuesday = 240
    first = await add_entry(
        cast.employee, week=WEEK, project_id=project["id"], task_id=task["id"], minutes=monday
    )
    assert first.status_code == 201, first.text
    second = await add_entry(
        cast.employee,
        week=WEEK,
        project_id=project["id"],
        task_id=task["id"],
        minutes=tuesday,
        entry_date=(WEEK + timedelta(days=1)).isoformat(),
    )
    assert second.status_code == 201, second.text
    assert (await submit(cast.employee, week=WEEK)).status_code == 200
    await decide(platform, cast, week=WEEK)
    restated = first.json()["days"][0]["entries"][0]["id"]
    cancelled = second.json()["days"][1]["entries"][0]["id"]

    assert (
        await supplement(
            cast.employee,
            week=WEEK,
            corrections=[
                {"entry_id": restated, "minutes": 300},
                {"entry_id": cancelled, "minutes": None},
            ],
        )
    ).status_code == 201
    assert (await submit(cast.employee, week=WEEK)).status_code == 200
    await decide(platform, cast, week=WEEK, supplementary=True)

    target = CURRENT_WEEK - timedelta(weeks=1)
    copied = await cast.employee.post(
        "/api/v1/timesheets/copy-previous", params={"week": target.isoformat()}
    )

    assert copied.status_code == 200, copied.text
    body = copied.json()
    assert body["entries_total_minutes"] == 300, body
    assert body["days"][0]["total_minutes"] == 300, "the cancelled entry was copied back"
    assert body["days"][1]["total_minutes"] == 0, "a reversal was copied as a line"
    assert [
        row["entry_type"] for day in body["days"] for row in day["entries"]
    ] == [EntryType.NORMAL.value]


async def test_the_minutes_check_keeps_its_ceiling_and_learns_the_sign(
    platform: Platform, cast: Cast
) -> None:
    """The widened `ck_timesheet_entries_minutes_range`, asserted where it is written.

    Ticket 28's rules survive — zero is not an amount, and nothing is longer than a
    day — and the new rule is that the sign follows the entry type: a *normal* row may
    not be negative. Without that half, a correction could be posted as a positive row
    that claims to cancel something, which is the one shape the whole net rests on not
    existing. (A reversal's own minutes are guarded one layer up, by the trigger that
    makes it the exact negation of its original.)
    """
    project, task = await make_target(cast)
    draft_week = CURRENT_WEEK - timedelta(weeks=1)
    created = await add_entry(
        cast.employee, week=draft_week, project_id=project["id"], task_id=task["id"], minutes=480
    )
    assert created.status_code == 201, created.text
    row = created.json()["days"][0]["entries"][0]["id"]

    negative = await platform.refused_by_database(
        "UPDATE timesheet_entries SET minutes = -60 WHERE id = :id", {"id": row}
    )
    zero = await platform.refused_by_database(
        "UPDATE timesheet_entries SET minutes = 0 WHERE id = :id", {"id": row}
    )
    too_long = await platform.refused_by_database(
        "UPDATE timesheet_entries SET minutes = :most WHERE id = :id",
        {"id": row, "most": MAX_ENTRY_MINUTES + 1},
    )

    for refusal in (negative, zero, too_long):
        assert "ck_timesheet_entries_minutes_range" in refusal, refusal
    assert await platform.scalar(
        "SELECT minutes FROM timesheet_entries WHERE id = :id", {"id": row}
    ) == 480


async def test_the_weeks_a_person_has_are_listed_without_their_supplements(
    platform: Platform, cast: Cast
) -> None:
    """A correction is not a week of its own in the list.

    It shares the Monday of the week it corrects, so listing it separately would show
    one week twice with two statuses and nothing to say which was the record. The
    status the list shows is `locked`, which is what the screen marks.
    """
    entry = await approved_week(platform, cast)
    assert (
        await supplement(
            cast.employee, week=WEEK, corrections=[{"entry_id": entry["id"], "minutes": 60}]
        )
    ).status_code == 201

    listed = await cast.employee.get("/api/v1/timesheets/mine")

    assert listed.status_code == 200, listed.text
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["week_start"] == WEEK.isoformat()
    assert listed.json()["items"][0]["status"] == TimesheetStatus.APPROVED.value
    assert listed.json()["items"][0]["is_locked"] is True
    assert await platform.scalar("SELECT count(*) FROM timesheets") == 2


async def test_a_week_that_is_not_the_callers_own_cannot_be_corrected(
    platform: Platform, cast: Cast
) -> None:
    """The 403 the timesheet surface has always answered, on the new route too.

    Correcting somebody else's locked week is the same act as filling in their draft,
    and the kernel decides it from the same three self-only actions: the refusal is a
    recorded 403, not a 404 that hides whether the week exists.
    """
    entry = await approved_week(platform, cast)
    colleague = await platform.account(roles=("employee",))
    await platform.assign(colleague.employee_id, cast.department, cast.position)

    refused = await colleague.post(
        "/api/v1/timesheets/supplements",
        params={"week": WEEK.isoformat(), "employee_id": cast.employee.employee_id},
        json={"corrections": [{"entry_id": entry["id"], "minutes": 60}]},
    )

    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == ErrorCode.FORBIDDEN.value
    assert await platform.scalar("SELECT count(*) FROM timesheets") == 1
    refusals = await platform.sql(
        "SELECT after -> 'action' FROM audit_log WHERE action = 'access.refused'"
    )
    assert [row[0] for row in refusals] == ["timesheet.write_own"]


async def test_an_approved_week_still_reads_and_still_reports_its_approval(
    platform: Platform, cast: Cast
) -> None:
    """Reading is not writing: the lock closes the write paths and nothing else.

    The week an approver signed has to stay readable for ever — that is what makes it
    evidence rather than a row — and the read still answers which week it is, what it
    came to, and how long is left to correct it.
    """
    entry = await approved_week(platform, cast, minutes=540)

    body = (await read_week(cast.employee, week=WEEK)).json()

    assert body["status"] == TimesheetStatus.APPROVED.value
    assert body["days"][0]["entries"][0]["id"] == entry["id"]
    assert body["days"][0]["total_minutes"] == 540
    assert body["supplement_weeks_left"] > 0
    assert body["can_supplement"] is True
    assert body["approval_request_id"] is not None
    assert (
        await cast.employee.get("/api/v1/timesheets/week/status", params={"week": WEEK.isoformat()})
    ).json()["approval"]["status"] == ApprovalStatus.APPROVED.value
