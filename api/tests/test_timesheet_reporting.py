"""Ticket 30: the hours report, its two subtotals, and who may read it.

Real database, real sessions, real approval engine — no mocks, for the reason the
timesheet module records: every claim here is a claim about *which rows a number was
made of*. "Only approved weeks", "reversals subtract", "a manager sees their reports"
and "the file states the same rows as the screen" are all statements about what the
query returned, and a substitute would answer with this file's own assumptions.

Every test names the checklist line it pins. The ones worth reading first:

* `test_the_report_counts_only_approved_sheets_net_of_reversals` — 汇总只计入已审批通过
  的净额，草稿与审批中的不计入: three weeks of one person's time, one approved, one a
  draft, one filed and waiting, and the report states the approved one and nothing
  else — absent rather than a row of zeros.
* `test_the_report_reconciles_with_the_detail_including_a_corrected_week` — 报表数值与
  逐条明细可对账: the report's figures compared against a `sum(minutes)` over the same
  rows, with a week that a supplement corrected in between.
* `test_a_reversal_carries_its_originals_billable_flag` — 汇总严格区分可计费与不可计费:
  a reversal carrying the other flag would move minutes between the two subtotals while
  every total stayed right, which is the one defect the split exists to make
  impossible, so the flag is asserted on the row.
* `test_a_manager_reads_their_reports_time_only` / `test_a_project_manager_reads_their_
  own_projects_only` / `test_hr_reads_everybody` — 经理只能看自己下属的工时；项目经理能看
  自己项目的工时；人力资源可看全员.
* `test_a_refusal_is_a_403_that_states_nothing` — 越权访问返回 403，且被拒绝的资源在响应
  中完全不存在.
* `test_the_export_states_the_same_rows_with_a_totals_line` — 支持导出为表格文件供财务或
  客户使用, and the export is audited as `data.exported`.
"""

import csv
import io
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.core.errors import ErrorCode
from app.domain.approval.models import ApprovalStatus, DecisionKind
from app.domain.approval.service import ApprovalService
from app.domain.attendance.business_day import madrid_today
from app.domain.timesheet.export import EXPORT_COLUMNS, EXPORT_EXCLUDES, TOTAL_LABEL
from app.domain.timesheet.models import monday_of
from app.domain.timesheet.report import MAX_REPORT_DAYS
from app.domain.timesheet.service import ENTITY_TYPE
from app.repositories.approval import PostgresApprovalRepository
from tests.support.platform import Actor, Platform

#: The Monday this run happens in. Every week below is measured from it, and all of
#: them are inside ticket 29's eight-week window so nothing has to be inserted raw.
CURRENT_WEEK = monday_of(madrid_today(datetime.now(UTC)))

WEEK_ONE = CURRENT_WEEK - timedelta(weeks=4)
WEEK_TWO = CURRENT_WEEK - timedelta(weeks=3)
WEEK_THREE = CURRENT_WEEK - timedelta(weeks=2)

FULL_DAY = 480


@dataclass(slots=True, frozen=True)
class Cast:
    """The people and places these tests move between.

    Two managerial positions, because a *project manager* is whoever created or was
    handed the project and a manager is a kind of caller: `manager` manages
    `project_a` and has one report, `other_manager` manages the control project, and
    `colleague` reports to nobody — which is what makes the two reaches tell apart.
    """

    manager: Actor
    other_manager: Actor
    report: Actor
    colleague: Actor
    #: Works in the second department: the other side of the department dimension,
    #: and somebody whose hours a manager in the first department must not reach.
    outsider: Actor
    hr: Actor
    department_a: str
    department_b: str


@pytest.fixture
async def cast(platform: Platform) -> Cast:
    suffix = uuid4().hex[:8]
    department_a = await platform.department(f"repA{suffix}")
    department_b = await platform.department(f"repB{suffix}")

    # Each manager is the other's approver, and every employee below names an approver:
    # the engine refuses a filing nobody could sign off, and a fixture with no route
    # would fail in the approval module rather than in the report.
    manager = await platform.account(roles=("employee",))
    other_manager = await platform.account(roles=("employee",))
    await platform.assign(
        manager.employee_id,
        department_a,
        await platform.position(department_a, f"jefeA{suffix}", is_managerial=True),
        manager_employee_id=other_manager.employee_id,
    )
    await platform.assign(
        other_manager.employee_id,
        department_b,
        await platform.position(department_b, f"jefeB{suffix}", is_managerial=True),
        manager_employee_id=manager.employee_id,
    )

    position = await platform.position(department_a, f"tecA{suffix}")
    # The colleague works in the manager's own department and reports to the *other*
    # manager: the department clause would hand them over, and the reporting
    # relationship refuses to — which is what the manager's test is about.
    colleague = await platform.account(roles=("employee",))
    await platform.assign(
        colleague.employee_id,
        department_a,
        position,
        manager_employee_id=other_manager.employee_id,
    )

    report = await platform.account(roles=("employee",))
    await platform.assign(
        report.employee_id,
        department_a,
        position,
        manager_employee_id=manager.employee_id,
    )

    hr = await platform.account(roles=("hr",))
    # The outsider's approver is HR rather than either manager, so the second
    # department's work is reachable *only* through the project its manager runs: with
    # a manager as their approver, both managers would read their hours through the
    # reporting relationship and the project clause would prove nothing.
    outsider = await platform.account(roles=("employee",))
    await platform.assign(
        outsider.employee_id,
        department_b,
        await platform.position(department_b, f"tecB{suffix}"),
        manager_employee_id=hr.employee_id,
    )

    return Cast(
        manager=manager,
        other_manager=other_manager,
        report=report,
        colleague=colleague,
        outsider=outsider,
        hr=hr,
        department_a=department_a,
        department_b=department_b,
    )


# --- helpers ----------------------------------------------------------------


async def make_project(actor: Actor, *, department_id: str, **overrides: object) -> dict:
    """A project created *by* `actor`, so they are named its manager."""
    created = await actor.post(
        "/api/v1/projects",
        json={
            "code": f"rp{uuid4().hex[:6]}",
            "name_es": "Proyecto",
            "name_en": "Project",
            "department_id": department_id,
            "start_date": "2020-01-01",
            "end_date": None,
            "status": "active",
            "is_billable_default": True,
            **overrides,
        },
    )
    assert created.status_code == 201, created.text
    return created.json()


async def make_task(actor: Actor, project_id: str, **overrides: object) -> dict:
    created = await actor.post(
        f"/api/v1/projects/{project_id}/tasks",
        json={
            "code": f"t{uuid4().hex[:6]}",
            "name_es": "Tarea",
            "name_en": "Task",
            **overrides,
        },
    )
    assert created.status_code == 201, created.text
    return created.json()


async def add_entry(
    actor: Actor,
    *,
    week: date,
    project_id: str,
    task_id: str,
    minutes: int,
    entry_date: date | None = None,
    expect: int = 201,
):
    response = await actor.post(
        "/api/v1/timesheets/entries",
        params={"week": week.isoformat()},
        json={
            "entry_date": (entry_date or week).isoformat(),
            "project_id": project_id,
            "task_id": task_id,
            "minutes": minutes,
        },
    )
    assert response.status_code == expect, response.text
    return response


async def sheet_id(
    platform: Platform, *, employee_id: str, week: date, supplementary: bool = False
) -> UUID:
    found = await platform.scalar(
        "SELECT id FROM timesheets WHERE employee_id = :employee_id AND week_start = :week "
        "AND is_supplementary = :supplementary",
        {"employee_id": employee_id, "week": week, "supplementary": supplementary},
    )
    assert found is not None, f"no sheet for {employee_id} in the week of {week}"
    return UUID(str(found))


async def approve(platform: Platform, sheet: UUID) -> None:
    """Approve a filed sheet at every level, as whoever the engine says is next.

    Level one names an individual — the requester's primary position's manager — and
    the step carries their id. Level two names *nobody* by construction: it is "anyone
    holding hr", so the decision is attributed to whichever hr account this database
    has, which is what an approval inbox does. Reading the route from the engine
    rather than assuming it keeps the helper honest for an employee who reports to
    nobody and one who does.
    """
    hr_employee_id = await platform.scalar(
        "SELECT employee_id FROM users WHERE roles @> '[\"hr\"]'::jsonb LIMIT 1"
    )
    async with platform.factory() as session:
        engine = ApprovalService(PostgresApprovalRepository(session), session)
        for _ in range(2):
            state = await engine.state_of(ENTITY_TYPE, sheet)
            assert state is not None, "that sheet was never filed"
            if state.status is ApprovalStatus.APPROVED:
                return
            step = state.pending_step
            assert step is not None, state
            approver = step.approver_employee_id or hr_employee_id
            assert approver is not None, "no hr account to decide level two"
            await engine.decide(state.id, UUID(str(approver)), DecisionKind.APPROVE)
        state = await engine.state_of(ENTITY_TYPE, sheet)
        assert state is not None and state.status is ApprovalStatus.APPROVED, state


async def file_and_approve(actor: Actor, platform: Platform, week: date) -> UUID:
    """File the week the employee already filled in, and have both levels approve.

    It ends by *reading* the week, which is the step that writes the engine's answer
    onto the sheet (`TimesheetService.apply_decision`). Ticket 29's lock and the
    report both read that column, so a decision nobody has read back is a decision the
    module has not recorded — the same boundary `test_timesheet_lock.py`'s helper
    works through, and the reason it is a read here rather than a second status write.
    """
    filed = await actor.post("/api/v1/timesheets/submit", params={"week": week.isoformat()})
    assert filed.status_code == 200, filed.text
    sheet = await sheet_id(platform, employee_id=actor.employee_id, week=week)
    await approve(platform, sheet)
    await apply_decisions(actor, week)
    return sheet


async def apply_decisions(actor: Actor, week: date) -> None:
    """Read the week, which is what moves its stored status to the engine's answer."""
    applied = await actor.get("/api/v1/timesheets/week", params={"week": week.isoformat()})
    assert applied.status_code == 200, applied.text
    assert applied.json()["status"] == "approved", applied.text


async def approved_week(
    actor: Actor,
    platform: Platform,
    *,
    week: date,
    project_id: str,
    task_id: str,
    minutes: int = FULL_DAY,
    entry_date: date | None = None,
) -> UUID:
    """One entry, filed and approved: a locked week to correct or to count."""
    await add_entry(
        actor,
        week=week,
        project_id=project_id,
        task_id=task_id,
        minutes=minutes,
        entry_date=entry_date,
    )
    return await file_and_approve(actor, platform, week)


async def supplement(
    actor: Actor, platform: Platform, *, week: date, entry_id: str, minutes: int | None
) -> UUID:
    """Correct one locked entry and have both levels approve the correction."""
    opened = await actor.post(
        "/api/v1/timesheets/supplements",
        params={"week": week.isoformat()},
        json={"corrections": [{"entry_id": entry_id, "minutes": minutes}]},
    )
    assert opened.status_code == 201, opened.text
    filed = await actor.post("/api/v1/timesheets/submit", params={"week": week.isoformat()})
    assert filed.status_code == 200, filed.text
    sheet = await sheet_id(
        platform, employee_id=actor.employee_id, week=week, supplementary=True
    )
    await approve(platform, sheet)
    await apply_decisions(actor, week)
    return sheet


async def report(actor: Actor, **params: object):
    """The report, as a client asks for it."""
    query = {key: value for key, value in params.items() if value is not None}
    if isinstance(query.get("group_by"), tuple):
        query["group_by"] = [str(value) for value in query["group_by"]]
    return await actor.get("/api/v1/timesheets/report", params=query)


def keys_of(row: dict) -> tuple:
    """A row's key, as the value that identifies it rather than as a label.

    The employee *id* and the department *id*, because every fixture account is the
    same person on paper and two departments can share a code shape: a name or a code
    would make two rows indistinguishable, and an assertion about who is missing would
    be vacuous.
    """
    values = []
    for dimension in row["dimensions"]:
        if dimension["week_start"] is not None:
            values.append(dimension["week_start"])
        elif dimension["kind"] == "project":
            values.append(dimension["code"])
        else:
            values.append(dimension["id"])
    return tuple(values)


def by_key(body: dict) -> dict:
    """The table as a mapping, which is the only way to assert it without depending on
    the order a `GROUP BY` happened to return uuids in."""
    return {keys_of(row): row["totals"] for row in body["rows"]}


async def detail_totals(
    platform: Platform, *, from_date: date, to_date: date, employee_id: str | None = None
) -> tuple[int, int, int, int, int, int]:
    """The same figures, summed from the rows the report is supposed to be made of.

    Written as SQL against the tables rather than by reading the API: a comparison that
    went through the code under test would agree with it about a mistake. The approval
    is the *sheet's*, which is what a corrected week turns on.
    """
    clause = "AND e.employee_id = :employee_id" if employee_id else ""
    row = (
        await platform.sql(
            f"""
            SELECT count(*),
                   coalesce(sum(e.minutes) FILTER (WHERE e.is_billable), 0),
                   coalesce(sum(e.minutes) FILTER (WHERE NOT e.is_billable), 0),
                   coalesce(sum(e.minutes) FILTER (WHERE e.minutes > 0), 0),
                   coalesce(sum(e.minutes) FILTER (WHERE e.minutes < 0), 0),
                   count(DISTINCT e.week_start)
              FROM timesheet_entries e
              JOIN timesheets t ON t.id = e.timesheet_id
             WHERE t.status = 'approved'
               AND e.entry_date BETWEEN :from_date AND :to_date
               {clause}
            """,
            {"from_date": from_date, "to_date": to_date, "employee_id": employee_id},
        )
    )[0]
    return (
        int(row[0]),
        int(row[1]),
        int(row[2]),
        int(row[3]),
        -int(row[4]),
        int(row[5]),
    )


# --- the four dimensions ----------------------------------------------------


async def test_the_report_aggregates_by_each_dimension_and_combines_them(
    platform: Platform, cast: Cast
) -> None:
    """**支持按项目、按部门、按员工、按期间四个维度汇总，并可组合筛选.**

    One table per dimension, over the same three approved weeks: the dimensions are the
    grouping and the facets at once, so the same filter answered four ways has to agree
    about the minutes and disagree only about how they are cut.
    """
    project_a = await make_project(cast.manager, department_id=cast.department_a)
    task_a = await make_task(cast.manager, project_a["id"])
    project_b = await make_project(cast.other_manager, department_id=cast.department_b)
    task_b = await make_task(cast.other_manager, project_b["id"])

    await approved_week(
        cast.colleague, platform, week=WEEK_ONE, project_id=project_a["id"],
        task_id=task_a["id"], minutes=480,
    )
    await approved_week(
        cast.colleague, platform, week=WEEK_TWO, project_id=project_a["id"],
        task_id=task_a["id"], minutes=240,
    )
    await approved_week(
        cast.outsider, platform, week=WEEK_ONE, project_id=project_b["id"],
        task_id=task_b["id"], minutes=300,
    )

    period = {"from_date": WEEK_ONE.isoformat(), "to_date": WEEK_THREE.isoformat()}
    by_project = (await report(cast.hr, **period, group_by=("project",))).json()
    by_department = (await report(cast.hr, **period, group_by=("department",))).json()
    by_employee = (await report(cast.hr, **period, group_by=("employee",))).json()
    by_period = (await report(cast.hr, **period, group_by=("period",))).json()
    combined = (await report(cast.hr, **period, group_by=("period", "project"))).json()

    assert by_project["group_by"] == ["project"]
    assert combined["group_by"] == ["period", "project"]
    assert {
        key: totals["total_minutes"] for key, totals in by_key(by_project).items()
    } == {(project_a["code"],): 720, (project_b["code"],): 300}
    assert {
        key: totals["total_minutes"] for key, totals in by_key(by_department).items()
    } == {(cast.department_a,): 720, (cast.department_b,): 300}
    assert {
        key: totals["total_minutes"] for key, totals in by_key(by_employee).items()
    } == {
        (cast.colleague.employee_id,): 720,
        (cast.outsider.employee_id,): 300,
    }
    # Only the period ordering is deterministic — ascending by the week it states —
    # and it is the one a reader expects.
    assert [keys_of(row) for row in by_period["rows"]] == [
        (WEEK_ONE.isoformat(),),
        (WEEK_TWO.isoformat(),),
    ]
    assert [row["totals"]["total_minutes"] for row in by_period["rows"]] == [780, 240]
    # Combined: a table of weeks by project, so the fortnight on A is two rows.
    assert len(combined["rows"]) == 3
    assert {
        key: totals["total_minutes"] for key, totals in by_key(combined).items()
    } == {
        (WEEK_ONE.isoformat(), project_a["code"]): 480,
        (WEEK_TWO.isoformat(), project_a["code"]): 240,
        (WEEK_ONE.isoformat(), project_b["code"]): 300,
    }

    # Every dimension is a facet as well, and the facets are conjunctive.
    only_a = (await report(cast.hr, **period, project_id=[project_a["id"]])).json()
    only_outsider = (
        await report(cast.hr, **period, employee_id=[cast.outsider.employee_id])
    ).json()
    both = (
        await report(cast.hr, **period, project_id=[project_a["id"]], group_by=("period",))
    ).json()

    assert len(only_a["rows"]) == 1 and only_a["rows"][0]["totals"]["total_minutes"] == 720
    assert only_outsider["total"]["total_minutes"] == 300
    # A dimension named twice is one dimension: the caller's order is kept and the
    # repeat is dropped, so a row cannot state the same key twice and mean it once.
    repeated = (
        await report(cast.hr, **period, group_by=("period", "period", "project"))
    ).json()
    assert repeated["group_by"] == ["period", "project"]
    assert repeated["rows"] == combined["rows"]
    assert [row["totals"]["total_minutes"] for row in both["rows"]] == [480, 240]
    # The total is over the whole filter, and `weeks` is a distinct count rather than
    # the rows added up: two people shared one of the two weeks, and the fortnight on
    # project A puts three week-lines in the table.
    assert by_project["total"]["total_minutes"] == 1020
    assert by_project["total"]["weeks"] == 2
    assert sum(row["totals"]["weeks"] for row in by_project["rows"]) == 3


# --- billable and non-billable, strictly separated --------------------------


async def test_billable_and_non_billable_are_separated_in_every_row_and_in_the_total(
    platform: Platform, cast: Cast
) -> None:
    """**汇总严格区分可计费与不可计费工时，分别给出小计.**

    Three configurations and all three are exercised: a billable project with a task
    that inherits, an unbillable task inside a billable project, and a non-billable
    project whose task overrides the other way. `is_billable` comes from the task and
    its project and never from the request, so the report's split is the configuration
    read back.
    """
    billable = await make_project(cast.manager, department_id=cast.department_a)
    inherits = await make_task(cast.manager, billable["id"])
    overridden_off = await make_task(cast.manager, billable["id"], is_billable=False)
    unbillable = await make_project(
        cast.manager, department_id=cast.department_a, is_billable_default=False
    )
    overridden_on = await make_task(cast.manager, unbillable["id"], is_billable=True)

    await add_entry(
        cast.colleague, week=WEEK_ONE, project_id=billable["id"],
        task_id=inherits["id"], minutes=480,
    )
    await add_entry(
        cast.colleague, week=WEEK_ONE, project_id=billable["id"],
        task_id=overridden_off["id"], minutes=120,
        entry_date=WEEK_ONE + timedelta(days=1),
    )
    await file_and_approve(cast.colleague, platform, WEEK_ONE)
    await approved_week(
        cast.colleague, platform, week=WEEK_TWO, project_id=unbillable["id"],
        task_id=overridden_on["id"], minutes=60,
    )

    body = (
        await report(
            cast.hr,
            from_date=WEEK_ONE.isoformat(),
            to_date=WEEK_THREE.isoformat(),
            group_by=("project",),
        )
    ).json()
    rows = by_key(body)

    assert rows[(billable["code"],)] == {
        "billable_minutes": 480,
        "non_billable_minutes": 120,
        "total_minutes": 600,
        "gross_minutes": 600,
        "reversal_minutes": 0,
        "entries": 2,
        "weeks": 1,
    }
    # The task's override is the answer in both directions: off inside a billable
    # project, and on inside an unbillable one.
    assert rows[(unbillable["code"],)]["billable_minutes"] == 60
    assert rows[(unbillable["code"],)]["non_billable_minutes"] == 0
    assert body["total"]["billable_minutes"] == 540
    assert body["total"]["non_billable_minutes"] == 120
    assert body["total"]["total_minutes"] == 660
    for totals in (*(row["totals"] for row in body["rows"]), body["total"]):
        assert totals["total_minutes"] == (
            totals["billable_minutes"] + totals["non_billable_minutes"]
        )


async def test_a_reversal_carries_its_originals_billable_flag(
    platform: Platform, cast: Cast
) -> None:
    """The flag on the negative row, asserted rather than assumed.

    A reversal carrying the *other* flag would still leave every total right — the
    minutes would simply move from one subtotal to the other, which is the one defect
    the split exists to make impossible. So the row is read, and so is the effect: the
    billable subtotal falls by exactly what the reversal cancels.
    """
    project = await make_project(cast.manager, department_id=cast.department_a)
    task = await make_task(cast.manager, project["id"])
    sheet = await approved_week(
        cast.colleague, platform, week=WEEK_ONE, project_id=project["id"],
        task_id=task["id"], minutes=480,
    )
    locked = await platform.sql(
        "SELECT id, is_billable FROM timesheet_entries WHERE timesheet_id = :sheet",
        {"sheet": sheet},
    )
    await supplement(
        cast.colleague, platform, week=WEEK_ONE,
        entry_id=str(locked[0][0]), minutes=300,
    )

    flags = await platform.sql(
        "SELECT entry_type, is_billable, minutes FROM timesheet_entries "
        "WHERE week_start = :week ORDER BY created_at",
        {"week": WEEK_ONE},
    )
    assert [(row[0], row[1]) for row in flags] == [
        ("normal", True),
        ("reversal", True),
        ("normal", True),
    ], flags

    totals = (
        await report(
            cast.hr, from_date=WEEK_ONE.isoformat(), to_date=WEEK_ONE.isoformat()
        )
    ).json()["total"]
    # 480 approved, corrected to 300: the net moved by the difference, and the reversal
    # sits in the same subtotal as the entry it cancels rather than beside it.
    assert totals == {
        "billable_minutes": 300,
        "non_billable_minutes": 0,
        "total_minutes": 300,
        "gross_minutes": 780,
        "reversal_minutes": 480,
        "entries": 3,
        "weeks": 1,
    }


# --- only approved, net of reversals, and reconcilable ---------------------


async def test_the_report_counts_only_approved_sheets_net_of_reversals(
    platform: Platform, cast: Cast
) -> None:
    """**汇总只计入已审批通过的净额，草稿与审批中的不计入.**

    Three weeks of one person's time on one project: the first approved, the second a
    draft nobody filed, the third filed and waiting for a decision. Only the first is
    in the report — and the other two are *absent* rather than present as rows of
    zeros, because "counted and came to nothing" and "not counted" are different
    answers and the ticket asks for the second.
    """
    project = await make_project(cast.manager, department_id=cast.department_a)
    task = await make_task(cast.manager, project["id"])

    await approved_week(
        cast.colleague, platform, week=WEEK_ONE, project_id=project["id"],
        task_id=task["id"], minutes=480,
    )
    await add_entry(
        cast.colleague, week=WEEK_TWO, project_id=project["id"], task_id=task["id"], minutes=240
    )
    await add_entry(
        cast.colleague, week=WEEK_THREE, project_id=project["id"], task_id=task["id"], minutes=120
    )
    assert (
        await cast.colleague.post(
            "/api/v1/timesheets/submit", params={"week": WEEK_THREE.isoformat()}
        )
    ).status_code == 200

    weeks = await platform.sql("SELECT week_start, status FROM timesheets ORDER BY week_start")
    assert [row[1] for row in weeks] == ["approved", "draft", "pending"], weeks

    body = (
        await report(
            cast.hr,
            from_date=WEEK_ONE.isoformat(),
            to_date=WEEK_THREE.isoformat(),
            group_by=("period",),
        )
    ).json()

    assert [keys_of(row) for row in body["rows"]] == [(WEEK_ONE.isoformat(),)]
    assert body["rows"][0]["totals"]["total_minutes"] == 480
    assert body["total"]["total_minutes"] == 480
    assert body["total"]["weeks"] == 1
    assert body["total"]["entries"] == 1
    # The unapproved days are not in the answer at all: no row states a week that was
    # never approved, so there is no line a reader could mistake for "counted, zero".
    assert {row["dimensions"][0]["week_start"] for row in body["rows"]} == {
        WEEK_ONE.isoformat()
    }
    # And the same filter through the *detail* the file is derived from sees three
    # weeks, so the difference is the status predicate rather than an empty database.
    assert await platform.scalar(
        "SELECT count(DISTINCT week_start) FROM timesheet_entries"
    ) == 3


async def test_the_report_reconciles_with_the_detail_including_a_corrected_week(
    platform: Platform, cast: Cast
) -> None:
    """**冲销记录在汇总中正确抵减，报表数值与逐条明细可对账.**

    The report's five figures compared against the same five summed from the rows, for
    one person and one period, with a week that a supplement corrected in between and a
    draft week beside it that nothing may count. The SQL is written against the tables
    rather than read back through the API, so the two sides are independent.
    """
    project = await make_project(cast.manager, department_id=cast.department_a)
    task = await make_task(cast.manager, project["id"])
    unbillable_task = await make_task(cast.manager, project["id"], is_billable=False)

    # A first week of which only part is billable, written in one go: an approved week
    # is locked, so both entries have to exist before it is filed.
    await add_entry(
        cast.colleague, week=WEEK_ONE, project_id=project["id"],
        task_id=task["id"], minutes=480,
    )
    await add_entry(
        cast.colleague, week=WEEK_ONE, project_id=project["id"],
        task_id=unbillable_task["id"], minutes=90,
        entry_date=WEEK_ONE + timedelta(days=1),
    )
    await file_and_approve(cast.colleague, platform, WEEK_ONE)

    # A second week, corrected by a supplement that two levels then approved.
    sheet = await approved_week(
        cast.colleague, platform, week=WEEK_TWO, project_id=project["id"],
        task_id=task["id"], minutes=480,
    )
    locked = await platform.sql(
        "SELECT id FROM timesheet_entries WHERE timesheet_id = :sheet", {"sheet": sheet}
    )
    await supplement(
        cast.colleague, platform, week=WEEK_TWO, entry_id=str(locked[0][0]), minutes=300
    )

    # ... and a draft week, which must not move any of the numbers.
    await add_entry(
        cast.colleague, week=WEEK_THREE, project_id=project["id"],
        task_id=task["id"], minutes=600,
    )

    period = {"from_date": WEEK_ONE.isoformat(), "to_date": WEEK_THREE.isoformat()}
    body = (
        await report(
            cast.hr, **period, employee_id=[cast.colleague.employee_id], group_by=("period",)
        )
    ).json()
    detail = await detail_totals(platform, employee_id=cast.colleague.employee_id, **period)

    assert (
        body["total"]["entries"],
        body["total"]["billable_minutes"],
        body["total"]["non_billable_minutes"],
        body["total"]["gross_minutes"],
        body["total"]["reversal_minutes"],
        body["total"]["weeks"],
    ) == detail
    assert body["total"]["total_minutes"] == 480 + 90 + 300
    # The table agrees with its own totals row, week by week.
    assert sum(row["totals"]["total_minutes"] for row in body["rows"]) == (
        body["total"]["total_minutes"]
    )
    assert [keys_of(row) for row in body["rows"]] == [
        (WEEK_ONE.isoformat(),),
        (WEEK_TWO.isoformat(),),
    ]
    assert body["rows"][1]["totals"]["gross_minutes"] == 780
    assert body["rows"][1]["totals"]["reversal_minutes"] == 480
    # And the database sums the corrected week too, so the net is arithmetic over rows
    # rather than a figure the report adjusted.
    assert await platform.scalar(
        "SELECT sum(minutes) FROM timesheet_entries e JOIN timesheets t "
        "ON t.id = e.timesheet_id WHERE t.status = 'approved' AND e.employee_id = :id",
        {"id": cast.colleague.employee_id},
    ) == 480 + 90 + 300


async def test_a_period_the_module_will_not_answer_is_refused(
    platform: Platform, cast: Cast
) -> None:
    """A report about a period nobody meant is refused before the aggregate runs.

    Two ways, one code: the dates are inverted, or the range is wider than the four
    years the record is kept for. Both are the caller's mistake, both name the bound,
    and neither reaches the database.
    """
    inverted = await report(
        cast.hr, from_date=WEEK_THREE.isoformat(), to_date=WEEK_ONE.isoformat()
    )
    too_wide = await report(
        cast.hr,
        from_date=WEEK_ONE.isoformat(),
        to_date=(WEEK_ONE + timedelta(days=MAX_REPORT_DAYS)).isoformat(),
    )

    for response in (inverted, too_wide):
        assert response.status_code == 422, response.text
        error = response.json()["error"]
        assert error["code"] == ErrorCode.TIMESHEET_REPORT_RANGE_INVALID.value
        assert error["message_key"] == "errors.timesheet_report_range_invalid"
        assert str(MAX_REPORT_DAYS) in error["detail"]


# --- who may read what ------------------------------------------------------


async def test_a_manager_reads_their_reports_time_only(platform: Platform, cast: Cast) -> None:
    """**经理只能看自己下属的工时.**

    The manager's reach is the reporting relationship and nothing else. The colleague
    works in the *same department* and reports to the other manager, and books their
    week against a project the manager here does not manage either — so both clauses
    are against them, and the department is the one the generic rule would have got
    wrong.
    """
    project = await make_project(cast.manager, department_id=cast.department_a)
    task = await make_task(cast.manager, project["id"])
    elsewhere = await make_project(cast.other_manager, department_id=cast.department_a)
    other_task = await make_task(cast.other_manager, elsewhere["id"])
    await approved_week(
        cast.report, platform, week=WEEK_ONE, project_id=project["id"],
        task_id=task["id"], minutes=300,
    )
    await approved_week(
        cast.colleague, platform, week=WEEK_TWO, project_id=elsewhere["id"],
        task_id=other_task["id"], minutes=480,
    )

    period = {"from_date": WEEK_ONE.isoformat(), "to_date": WEEK_THREE.isoformat()}
    response = await report(cast.manager, **period, group_by=("employee",))
    body = response.json()

    assert [keys_of(row) for row in body["rows"]] == [(cast.report.employee_id,)]
    assert body["total"]["total_minutes"] == 300
    assert body["total"]["entries"] == 1
    # The colleague is not in the response at all — not a row, not an id, not a minute
    # of theirs. The same period read by HR is the control: 780 minutes, not 300.
    assert cast.colleague.employee_id not in response.text
    assert elsewhere["code"] not in response.text
    as_hr = (await report(cast.hr, **period)).json()
    assert as_hr["total"]["total_minutes"] == 780
    assert len(as_hr["rows"]) == 2


async def test_a_project_manager_reads_their_own_projects_only(
    platform: Platform, cast: Cast
) -> None:
    """**项目经理能看自己项目的工时.**

    The second reach, and it is a fact about the *row*: the colleague reports to the
    other manager, and the manager here reads their time because it was booked against
    the manager's own project. The mirror is not symmetric on purpose — the other
    manager reaches the first project through *their* report, which is the other
    clause of the same union — so the control is that the two totals differ, that each
    manager states their own project, and that the outsider belongs to neither.
    """
    mine = await make_project(cast.manager, department_id=cast.department_a)
    my_task = await make_task(cast.manager, mine["id"])
    # In the other manager's own department, so the outsider may book against it and
    # neither manager reaches it through anything but the project's manager.
    theirs = await make_project(cast.other_manager, department_id=cast.department_b)
    their_task = await make_task(cast.other_manager, theirs["id"])

    await approved_week(
        cast.colleague, platform, week=WEEK_ONE, project_id=mine["id"],
        task_id=my_task["id"], minutes=300,
    )
    await approved_week(
        cast.outsider, platform, week=WEEK_TWO, project_id=theirs["id"],
        task_id=their_task["id"], minutes=480,
    )

    period = {"from_date": WEEK_ONE.isoformat(), "to_date": WEEK_THREE.isoformat()}
    response = await report(cast.manager, **period, group_by=("project",))
    body = response.json()

    assert [keys_of(row) for row in body["rows"]] == [(mine["code"],)]
    assert body["total"]["total_minutes"] == 300
    assert body["total"]["weeks"] == 1
    assert theirs["code"] not in response.text
    assert cast.outsider.employee_id not in response.text

    other_response = await report(cast.other_manager, **period)
    other = other_response.json()
    other_keys = {keys_of(row) for row in other["rows"]}
    assert (theirs["code"],) in other_keys
    # ... and the first project through their report, which is the union's other half.
    assert (mine["code"],) in other_keys
    assert other["total"]["total_minutes"] == 780
    assert cast.outsider.employee_id not in other_response.text


async def test_hr_reads_everybody(platform: Platform, cast: Cast) -> None:
    """**人力资源可看全员.** The control for both refusals above."""
    project = await make_project(cast.manager, department_id=cast.department_a)
    task = await make_task(cast.manager, project["id"])
    for actor, minutes in ((cast.report, 300), (cast.colleague, 480), (cast.manager, 120)):
        await approved_week(
            actor, platform, week=WEEK_ONE, project_id=project["id"],
            task_id=task["id"], minutes=minutes,
        )

    body = (
        await report(
            cast.hr,
            from_date=WEEK_ONE.isoformat(),
            to_date=WEEK_ONE.isoformat(),
            group_by=("employee",),
        )
    ).json()

    assert len(body["rows"]) == 3
    assert body["total"]["total_minutes"] == 900
    assert body["total"]["weeks"] == 1
    # The colleague reports to nobody and works on a project managed by somebody who
    # is not HR: HR's remit does not depend on the row at all.
    assert body["total"]["entries"] == 3


async def test_a_refusal_is_a_403_that_states_nothing(platform: Platform, cast: Cast) -> None:
    """**越权访问返回 403，且被拒绝的资源在响应中完全不存在.**

    Two callers who may read no timesheet at all: an ordinary employee, whose own week
    is a different action, and an administrator, whom §4.1 gives the system rather than
    the personnel record. The refusal names neither a project code nor an employee id
    nor a minute — it is a refusal, not a filtered payload with a warning attached.
    """
    project = await make_project(cast.manager, department_id=cast.department_a)
    task = await make_task(cast.manager, project["id"])
    await approved_week(
        cast.colleague, platform, week=WEEK_ONE, project_id=project["id"],
        task_id=task["id"], minutes=480,
    )
    admin = await platform.account(roles=("admin",))

    period = {"from_date": WEEK_ONE.isoformat(), "to_date": WEEK_ONE.isoformat()}
    for actor in (cast.colleague, admin):
        response = await report(actor, **period)
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == ErrorCode.FORBIDDEN.value
        assert project["code"] not in response.text
        assert cast.colleague.employee_id not in response.text
        assert "billable_minutes" not in response.text
        assert "rows" not in response.json()
        # The export is the same permission and refuses the same way.
        exported = await actor.get("/api/v1/timesheets/report/export", params=period)
        assert exported.status_code == 403, exported.text
        assert project["code"] not in exported.text

    # Every refusal is recorded against the action that was attempted, which is the
    # half of the rule an incident review reads.
    refusals = await platform.sql(
        "SELECT after ->> 'action' FROM audit_log WHERE action = 'access.refused'"
    )
    assert [row[0] for row in refusals] == ["timesheet.read_report"] * len(refusals)
    assert len(refusals) == 4


# --- the file ---------------------------------------------------------------


async def test_the_export_states_the_same_rows_with_a_totals_line(
    platform: Platform, cast: Cast
) -> None:
    """**支持导出为表格文件供财务或客户使用**, and the export is audited.

    The file is the report in another shape, so the assertion is that the two agree
    figure for figure; that the columns are the documented ones and none of them looks
    like money; and that the export wrote its own `data.exported` record, because
    re-running a period is normal and the trail is what makes "who handed this period
    over" answerable.
    """
    project = await make_project(cast.manager, department_id=cast.department_a)
    task = await make_task(cast.manager, project["id"])
    await add_entry(
        cast.colleague, week=WEEK_ONE, project_id=project["id"],
        task_id=task["id"], minutes=480,
    )
    await add_entry(
        cast.colleague, week=WEEK_ONE, project_id=project["id"],
        task_id=task["id"], minutes=90, entry_date=WEEK_ONE + timedelta(days=1),
    )
    sheet = await file_and_approve(cast.colleague, platform, WEEK_ONE)
    locked = await platform.sql(
        "SELECT id FROM timesheet_entries WHERE timesheet_id = :sheet "
        "ORDER BY created_at LIMIT 1",
        {"sheet": sheet},
    )
    await supplement(
        cast.colleague, platform, week=WEEK_ONE, entry_id=str(locked[0][0]), minutes=300
    )

    period = {"from_date": WEEK_ONE.isoformat(), "to_date": WEEK_TWO.isoformat()}
    body = (await report(cast.hr, **period, group_by=("period",))).json()
    response = await cast.hr.get(
        "/api/v1/timesheets/report/export", params={**period, "group_by": ["period"]}
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"] == (
        f'attachment; filename="timesheet-report-{WEEK_ONE}-{WEEK_TWO}.csv"'
    )

    rows = list(csv.reader(io.StringIO(response.text)))
    assert rows[0] == list(EXPORT_COLUMNS)
    assert rows[-1][0] == TOTAL_LABEL
    assert len({len(row) for row in rows}) == 1, "a spreadsheet needs one row width"
    assert rows[1][3] == WEEK_ONE.isoformat()
    assert [int(value) for value in rows[1][4:]] == [
        body["rows"][0]["totals"][field]
        for field in (
            "billable_minutes",
            "non_billable_minutes",
            "total_minutes",
            "gross_minutes",
            "reversal_minutes",
            "entries",
            "weeks",
        )
    ]
    assert [int(value) for value in rows[-1][4:]] == [
        body["total"][field]
        for field in (
            "billable_minutes",
            "non_billable_minutes",
            "total_minutes",
            "gross_minutes",
            "reversal_minutes",
            "entries",
            "weeks",
        )
    ]
    # The reversal is visible in the file as its own column, so a reader can see the
    # correction rather than only its effect.
    assert int(rows[1][8]) == 480

    # No column that would look like money. The exclusions are the contract, so they
    # are asserted rather than left to a reviewer's eye.
    header = " ".join(rows[0]).lower()
    assert not [token for token in EXPORT_EXCLUDES if token in header]
    assert "hourly" not in header and "cost" not in header and "precio" not in header

    trail = await platform.sql(
        "SELECT entity_type, actor_user_id, after FROM audit_log WHERE action = 'data.exported'"
    )
    assert len(trail) == 1, trail
    entity_type, actor_user_id, after = trail[0]
    assert entity_type == "timesheet_report"
    assert str(actor_user_id) == cast.hr.user_id
    assert after["from_date"] == WEEK_ONE.isoformat()
    assert after["to_date"] == WEEK_TWO.isoformat()
    assert after["group_by"] == ["period"]
    assert after["billable_minutes"] == body["total"]["billable_minutes"]
    assert after["non_billable_minutes"] == body["total"]["non_billable_minutes"]
    assert after["weeks"] == body["total"]["weeks"]
    assert after["rows"] == len(body["rows"])


async def test_a_period_with_nothing_approved_is_a_header_and_no_totals_line(
    platform: Platform, cast: Cast
) -> None:
    """A file for a period with no approved hours states nothing.

    No totals line, deliberately: `TOTAL,0,0` would state that the period came to
    nothing, while a period nobody has filed simply has nothing to state — the
    convention the attendance and overtime exports established, and the reason the
    totals line is written only when there are rows.
    """
    response = await cast.hr.get(
        "/api/v1/timesheets/report/export",
        params={
            "from_date": WEEK_ONE.isoformat(),
            "to_date": WEEK_TWO.isoformat(),
            "group_by": ["employee", "period"],
        },
    )

    assert response.status_code == 200, response.text
    rows = list(csv.reader(io.StringIO(response.text)))
    assert rows == [list(EXPORT_COLUMNS)]
    assert TOTAL_LABEL not in response.text
    # A read that changes nothing still leaves its own trail entry.
    assert await platform.scalar(
        "SELECT count(*) FROM audit_log WHERE action = 'data.exported'"
    ) == 1
