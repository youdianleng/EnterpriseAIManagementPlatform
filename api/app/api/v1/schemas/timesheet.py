"""Timesheet request and response shapes.

The response shapes are deliberately *flat* where the grid reads them (a day's
entries, its total and its expectation in one object) and deliberately *absent* where
a value is the server's answer (`is_billable` is returned and never accepted).

Three conventions worth naming:

* **`is_billable` is not a request field.** It comes from the task's configuration,
  through `ProjectService.resolve_record_target`, exactly as ticket 27's decision
  endpoint does, and a request that names a task cannot change it. The response
  carries it so a client can show which rows a report will bill.
* **`week` travels as a query parameter and is a date**, so a client can open a week
  before a row exists and the grid is always for a week somebody named.
* **Ticket 29's three reads are the server's answers, not the client's arithmetic.**
  `supplement_weeks_left`, `can_supplement` and `week_closed` come from the API, so
  the screen offering "correct this week" and the endpoint refusing it cannot
  disagree about which side of the eight-week window the week is on. Each entry
  carries its own `entry_type`, `reverses_entry_id` and `timesheet_id`, which is what
  lets one grid draw a locked original's rows and a draft correction's rows in the
  same table without guessing which is which.
"""

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.api.v1.schemas.base import StrictModel
from app.domain.approval.models import ApprovalState, ApprovalStatus, StepStatus
from app.domain.timesheet.models import (
    MAX_ENTRY_MINUTES,
    SUPPLEMENT_WINDOW_WEEKS,
    CorrectionInput,
    EntryType,
    OverBudgetDay,
    ProjectLabel,
    TaskNet,
    Timesheet,
    TimesheetEntry,
    TimesheetStatus,
    WeekView,
)
from app.domain.timesheet.report import (
    ReportDimension,
    ReportSummary,
    ReportTotals,
)


class EntryWrite(StrictModel):
    """One day's work on one task.

    No `is_billable` and no `employee_id`: the first is the server's answer and the
    second is the caller's own id, and neither is the client's to state. `StrictModel`
    refuses an unknown field rather than ignoring it, so a client that sends
    `is_billable: true` is told this system does not accept it — which is clearer
    than the value being silently dropped.

    No `entry_type` either, and for the same reason: a reversal is written by the
    supplementary flow from a correction the server validated, never posted by a
    client that would like a negative row in its own week.
    """

    entry_date: date
    project_id: UUID
    task_id: UUID
    minutes: int = Field(
        gt=0,
        le=MAX_ENTRY_MINUTES,
        description=(
            f"Positive minutes, at most {MAX_ENTRY_MINUTES} (24 h). The day's expected "
            "hours are a warning, not a limit"
        ),
    )
    note: str | None = Field(default=None, max_length=500)


class EntryUpdate(StrictModel):
    """A change to one entry. Every omitted key means "leave it alone".

    `note` is the one field where an explicit `null` is a value — clearing it — which
    is why the router reads `exclude_unset` rather than the values alone.
    """

    entry_date: date | None = None
    project_id: UUID | None = None
    task_id: UUID | None = None
    minutes: int | None = Field(default=None, gt=0, le=MAX_ENTRY_MINUTES)
    note: str | None = Field(default=None, max_length=500)


class CorrectionWrite(StrictModel):
    """One locked entry a supplementary submission corrects.

    `minutes` is the corrected amount, or `null` for "this entry should not exist" —
    the reversal on its own. `project_id` and `task_id` may move the work to another
    target, and are omitted when the correction is only about the amount, which is
    the ordinary case.
    """

    entry_id: UUID
    minutes: int | None = Field(default=None, gt=0, le=MAX_ENTRY_MINUTES)
    note: str | None = Field(default=None, max_length=500)
    project_id: UUID | None = None
    task_id: UUID | None = None

    def to_input(self) -> CorrectionInput:
        return CorrectionInput(
            entry_id=self.entry_id,
            minutes=self.minutes,
            note=self.note,
            project_id=self.project_id,
            task_id=self.task_id,
        )


class SupplementWrite(StrictModel):
    """A supplementary submission: what the locked week should have said.

    At least one correction: a supplement that changes nothing is a document with no
    subject, and the approval it would spend two people's attention on would decide
    nothing.
    """

    corrections: list[CorrectionWrite] = Field(min_length=1, max_length=50)

    def to_inputs(self) -> list[CorrectionInput]:
        return [correction.to_input() for correction in self.corrections]


class EntryRead(BaseModel):
    """One entry as the grid reads it: the row, plus what it names.

    The codes and names are read rather than stored, so a task renamed last month
    shows its current name here and its old one nowhere — which is right for a grid
    somebody is filling in, and why the audit trail carries ids and minutes.

    `entry_type` and `reverses_entry_id` are what make a correction legible: a row
    with `reversal` and a negative `minutes` is one half of a pair, and the id says
    which locked entry the other half is. `timesheet_id` says which *sheet* the row
    belongs to, so a week holding a locked original and a draft correction can draw
    each row as what it is.
    """

    id: UUID
    timesheet_id: UUID
    entry_date: date
    project_id: UUID
    task_id: UUID
    minutes: int
    entry_type: EntryType
    reverses_entry_id: UUID | None
    is_billable: bool
    note: str | None
    project_code: str | None = None
    task_code: str | None = None
    task_name_es: str | None = None
    task_name_en: str | None = None


class DayRead(BaseModel):
    """One column of the grid.

    `expected_minutes` is null when no schedule reaches this person on this date,
    which is a different fact from a schedule that expects nothing — the grid can say
    "no schedule configured" rather than showing a confident zero.

    `total_minutes` is the net of the day; `gross_minutes` and `reversal_minutes` are
    what it was reached from. A reader has to be able to see "8 h − 2 h" rather than a
    silent 6 h, which is the whole reason the correction is rows and not an edit.
    """

    entry_date: date
    weekday: int
    entries: list[EntryRead]
    total_minutes: int
    gross_minutes: int
    reversal_minutes: int
    expected_minutes: int | None
    expectation_source: str | None
    is_holiday: bool


class TaskNetRead(BaseModel):
    """One task's week, after its reversals: the per-task half of the net view."""

    project_id: UUID
    task_id: UUID
    gross_minutes: int
    reversal_minutes: int
    net_minutes: int


class OverBudgetRead(BaseModel):
    """A day the week ran past, computed on the server from the schedule.

    In the read *and* in the response to a submission. Nothing in the product refuses
    on one: it is the fact an approver will want and the employee can still act on.
    """

    entry_date: date
    total_minutes: int
    expected_minutes: int
    over_minutes: int


class SupplementRead(BaseModel):
    """A correction filed against a week, as the grid lists it.

    `corrects_timesheet_id` is the original it points at, which is the same week: the
    link answers "what is this document correcting", and the week it shares with the
    original is what makes the two sheets one grid.
    """

    timesheet_id: UUID
    status: TimesheetStatus
    submitted_at: datetime | None
    approval_request_id: UUID | None
    corrects_timesheet_id: UUID | None
    week_start: date


class WeekRead(BaseModel):
    """The whole grid: seven days, the totals, and the days that ran long."""

    employee_id: UUID
    week_start: date
    week_end: date
    status: TimesheetStatus
    is_editable: bool
    has_timesheet: bool
    submitted_at: datetime | None
    approval_request_id: UUID | None
    entries_total_minutes: int
    expected_total_minutes: int | None
    over_budget: bool
    over_budget_days: list[OverBudgetRead]
    days: list[DayRead]
    # --- ticket 29: the lock, the correction, and the window -----------------
    is_locked: bool
    week_closed: bool
    #: How many weeks of supplementary filing this week still has. Zero means closed
    #: to every write, and it is the number the refusal names.
    supplement_weeks_left: int
    supplement_window_weeks: int
    can_supplement: bool
    is_supplementary: bool
    corrects_timesheet_id: UUID | None
    editable_timesheet_id: UUID | None
    sheets: list[SupplementRead]
    supplements: list[SupplementRead]
    gross_total_minutes: int
    reversal_total_minutes: int
    tasks: list[TaskNetRead]


class TimesheetRead(BaseModel):
    """One row of the caller's own week list."""

    week_start: date
    status: TimesheetStatus
    is_editable: bool
    is_locked: bool
    submitted_at: datetime | None


class TimesheetPageRead(BaseModel):
    items: list[TimesheetRead]
    total: int
    limit: int
    offset: int


class DecisionRead(BaseModel):
    level: int
    round: int
    approver_employee_id: UUID
    decision: StepStatus
    comment: str | None
    decided_at: datetime


class ApprovalRead(BaseModel):
    """The engine's own answer, passed through rather than paraphrased.

    `decisions` spans **every** round: a week that was returned, corrected and filed
    again carries both the return and the second round's outcome, which is the
    历史提交记录 the ticket asks to keep.

    **`initiated_by` and `confirmed_by_user_id` are the transparency annotation**
    (ticket 41). §6.3's fifth requirement is that the request an approver reads says
    「由助手起草、本人确认」 from an *API field* and never from a client-side guess, and
    these are the two fields §3.4 already keeps on `approval_requests` for exactly this:
    `initiated_by` is `user` for a request somebody filed through the ordinary screen and
    `agent` for one the assistant proposed and a person confirmed, and
    `confirmed_by_user_id` names that person. Neither is asked of the entity — a
    hand-submitted week carries the same two columns with the same defaults, which is what
    makes "the approver sees exactly what a hand-submitted document looks like" true
    rather than approximately true. Ticket 53 renders them; nothing here decides the
    wording.
    """

    request_id: UUID
    status: ApprovalStatus
    round: int
    submitted_at: datetime | None
    decided_at: datetime | None
    pending_level: int | None
    decisions: list[DecisionRead]
    #: `user` | `agent` | `system`, straight from `approval_requests`.
    initiated_by: str = "user"
    #: Who confirmed an assistant-drafted request. Null for one a person filed directly.
    confirmed_by_user_id: UUID | None = None


class SheetStatusRead(BaseModel):
    """One sheet's own approval state, for the week's status read.

    Each supplement has a request of its own, so "the week's history" is several
    histories; the client shows the original's and the correction's beside each other
    rather than picking one and hiding the other.
    """

    timesheet_id: UUID
    status: TimesheetStatus
    is_supplementary: bool
    corrects_timesheet_id: UUID | None
    submitted_at: datetime | None
    approval_request_id: UUID | None
    approval: ApprovalRead | None


class StatusRead(BaseModel):
    week_start: date
    status: TimesheetStatus
    is_editable: bool
    is_locked: bool
    submitted_at: datetime | None
    approval_request_id: UUID | None
    approval: ApprovalRead | None
    sheets: list[SheetStatusRead]


# --- the report (ticket 30) -------------------------------------------------


class DimensionRead(BaseModel):
    """One dimension's value on one line of the report, as a labelled key.

    `code` and both names travel rather than a rendered label, for the reason
    `ProjectLabel` records: the interface ships in two languages, and a server that
    picked one would be a server deciding what a Spanish reader sees. `week_start` is
    set on a `period` value and `id` on the three that name a row.
    """

    kind: ReportDimension
    id: UUID | None = None
    code: str | None = None
    name_es: str | None = None
    name_en: str | None = None
    week_start: date | None = None


class TotalsRead(BaseModel):
    """The figures a line states, and the arithmetic that relates them.

    `billable_minutes` and `non_billable_minutes` are strictly separated and add up
    to `total_minutes`, which is the net of reversals. There is no rate, no amount and
    no currency anywhere in this shape: this system accumulates and exports minutes
    (DESIGN §7.3), and a money field would be a payroll figure computed from nothing.
    """

    billable_minutes: int
    non_billable_minutes: int
    total_minutes: int
    gross_minutes: int
    reversal_minutes: int
    #: The entry rows the figures were summed from, and the approved weeks they came
    #: from. Exact in a line and in the total, because both are a `count(distinct)`.
    entries: int
    weeks: int


class ReportRowRead(BaseModel):
    """One line of the table: what the group is, and what it came to."""

    dimensions: list[DimensionRead]
    totals: TotalsRead


class ReportRead(BaseModel):
    """The report: a table of rows and the totals row underneath it.

    `group_by` is echoed because the row keys are positional: the same report asked
    for weeks instead of projects is a different table, and a client that rendered the
    rows without knowing the grouping would have to infer it from the values.

    `total` covers the whole filter and is not necessarily the sum of `rows` — `entries`
    and `weeks` are counts over the period, and a week that booked time on two projects
    is one week in the total and a line in each of two rows.
    """

    from_date: date
    to_date: date
    group_by: list[ReportDimension]
    rows: list[ReportRowRead]
    total: TotalsRead


# --- projections ------------------------------------------------------------


def entry_read(entry: TimesheetEntry, label: ProjectLabel | None) -> EntryRead:
    return EntryRead(
        id=entry.id,
        timesheet_id=entry.timesheet_id,
        entry_date=entry.entry_date,
        project_id=entry.project_id,
        task_id=entry.task_id,
        minutes=entry.minutes,
        entry_type=entry.entry_type,
        reverses_entry_id=entry.reverses_entry_id,
        is_billable=entry.is_billable,
        note=entry.note,
        project_code=None if label is None else label.project_code,
        task_code=None if label is None else label.task_code,
        task_name_es=None if label is None else label.task_name_es,
        task_name_en=None if label is None else label.task_name_en,
    )


def over_budget_read(day: OverBudgetDay) -> OverBudgetRead:
    return OverBudgetRead(
        entry_date=day.entry_date,
        total_minutes=day.total_minutes,
        expected_minutes=day.expected_minutes,
        over_minutes=day.over_minutes,
    )


def task_net_read(task: TaskNet) -> TaskNetRead:
    return TaskNetRead(
        project_id=task.project_id,
        task_id=task.task_id,
        gross_minutes=task.gross_minutes,
        reversal_minutes=task.reversal_minutes,
        net_minutes=task.net_minutes,
    )


def sheet_read(sheet: Timesheet) -> SupplementRead:
    return SupplementRead(
        timesheet_id=sheet.id,
        status=sheet.status,
        submitted_at=sheet.submitted_at,
        approval_request_id=sheet.approval_request_id,
        corrects_timesheet_id=sheet.supersedes_id,
        week_start=sheet.week_start,
    )


def week_read(view: WeekView, labels: dict[UUID, ProjectLabel]) -> WeekRead:
    """The grid, with each cell resolved to what it names."""
    return WeekRead(
        employee_id=view.employee_id,
        week_start=view.week_start,
        week_end=view.week_end,
        status=view.status,
        is_editable=view.is_editable,
        has_timesheet=view.has_timesheet,
        submitted_at=view.submitted_at,
        approval_request_id=view.approval_request_id,
        entries_total_minutes=view.entries_total_minutes,
        expected_total_minutes=view.expected_total_minutes,
        over_budget=bool(view.over_budget_days),
        over_budget_days=[over_budget_read(day) for day in view.over_budget_days],
        days=[
            DayRead(
                entry_date=day.entry_date,
                weekday=day.weekday,
                entries=[entry_read(row, labels.get(row.task_id)) for row in day.entries],
                total_minutes=day.total_minutes,
                gross_minutes=day.gross_minutes,
                reversal_minutes=day.reversal_minutes,
                expected_minutes=day.expected_minutes,
                expectation_source=day.expectation_source,
                is_holiday=day.is_holiday,
            )
            for day in view.days
        ],
        is_locked=view.is_locked,
        week_closed=view.week_closed,
        supplement_weeks_left=view.supplement_weeks_left,
        supplement_window_weeks=SUPPLEMENT_WINDOW_WEEKS,
        can_supplement=view.can_supplement,
        is_supplementary=view.is_supplementary,
        corrects_timesheet_id=(
            None if view.timesheet is None else view.timesheet.supersedes_id
        ),
        editable_timesheet_id=view.editable_sheet_id,
        sheets=[sheet_read(sheet) for sheet in view.sheets],
        supplements=[sheet_read(sheet) for sheet in view.supplements],
        gross_total_minutes=view.gross_total_minutes,
        reversal_total_minutes=view.reversal_total_minutes,
        tasks=[task_net_read(task) for task in view.tasks],
    )


def timesheet_read(sheet: Timesheet) -> TimesheetRead:
    return TimesheetRead(
        week_start=sheet.week_start,
        status=sheet.status,
        is_editable=sheet.is_editable,
        is_locked=sheet.is_locked,
        submitted_at=sheet.submitted_at,
    )


def totals_read(totals: ReportTotals) -> TotalsRead:
    return TotalsRead(
        billable_minutes=totals.billable_minutes,
        non_billable_minutes=totals.non_billable_minutes,
        total_minutes=totals.total_minutes,
        gross_minutes=totals.gross_minutes,
        reversal_minutes=totals.reversal_minutes,
        entries=totals.entries,
        weeks=totals.weeks,
    )


def report_read(summary: ReportSummary) -> ReportRead:
    """The report, as the response states it: a table and its totals row."""
    return ReportRead(
        from_date=summary.filter.from_date,
        to_date=summary.filter.to_date,
        group_by=list(summary.filter.group_by),
        rows=[
            ReportRowRead(
                dimensions=[
                    DimensionRead(
                        kind=value.kind,
                        id=value.id,
                        code=value.code,
                        name_es=value.name_es,
                        name_en=value.name_en,
                        week_start=value.week_start,
                    )
                    for value in row.dimensions
                ],
                totals=totals_read(row.totals),
            )
            for row in summary.rows
        ],
        total=totals_read(summary.totals),
    )


def approval_read(state: ApprovalState) -> ApprovalRead:
    pending = state.pending_step
    return ApprovalRead(
        request_id=state.id,
        status=state.status,
        round=state.round,
        submitted_at=state.request.submitted_at,
        decided_at=state.decided_at,
        pending_level=None if pending is None else pending.level,
        # Ticket 41's transparency annotation: §3.4's two columns, read off the engine's own
        # request rather than inferred from the caller or the entity.
        initiated_by=state.request.initiated_by,
        confirmed_by_user_id=state.request.confirmed_by_user_id,
        decisions=[
            DecisionRead(
                level=decision.level,
                round=decision.round,
                approver_employee_id=decision.approver_employee_id,
                decision=decision.decision,
                comment=decision.comment,
                decided_at=decision.decided_at,
            )
            # Every round, oldest first: the engine orders them by level within a
            # round, and a reader reconstructs the attempts from `round`.
            for decision in state.decisions
        ],
    )


__all__ = [
    "ApprovalRead",
    "CorrectionWrite",
    "DayRead",
    "DecisionRead",
    "DimensionRead",
    "EntryRead",
    "EntryUpdate",
    "EntryWrite",
    "OverBudgetRead",
    "ReportRead",
    "ReportRowRead",
    "SheetStatusRead",
    "StatusRead",
    "SupplementRead",
    "SupplementWrite",
    "TaskNetRead",
    "TimesheetPageRead",
    "TimesheetRead",
    "TotalsRead",
    "WeekRead",
    "approval_read",
    "entry_read",
    "over_budget_read",
    "report_read",
    "sheet_read",
    "task_net_read",
    "timesheet_read",
    "totals_read",
    "week_read",
]
