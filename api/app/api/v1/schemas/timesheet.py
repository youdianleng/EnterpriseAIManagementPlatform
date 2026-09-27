"""Timesheet request and response shapes.

The response shapes are deliberately *flat* where the grid reads them (a day's
entries, its total and its expectation in one object) and deliberately *absent* where
a value is the server's answer (`is_billable` is returned and never accepted).

Two conventions worth naming:

* **`is_billable` is not a request field.** It comes from the task's configuration,
  through `ProjectService.resolve_record_target`, exactly as ticket 27's decision
  endpoint does, and a request that names a task cannot change it. The response
  carries it so a client can show which rows a report will bill.
* **`week` travels as a query parameter and is a date**, so a client can open a week
  before a row exists and the grid is always for a week somebody named.
"""

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.api.v1.schemas.base import StrictModel
from app.domain.approval.models import ApprovalState, ApprovalStatus, StepStatus
from app.domain.timesheet.models import (
    MAX_ENTRY_MINUTES,
    OverBudgetDay,
    ProjectLabel,
    Timesheet,
    TimesheetEntry,
    TimesheetStatus,
    WeekView,
)


class EntryWrite(StrictModel):
    """One day's work on one task.

    No `is_billable` and no `employee_id`: the first is the server's answer and the
    second is the caller's own id, and neither is the client's to state. `StrictModel`
    refuses an unknown field rather than ignoring it, so a client that sends
    `is_billable: true` is told this system does not accept it — which is clearer
    than the value being silently dropped.
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


class EntryRead(BaseModel):
    """One entry as the grid reads it: the row, plus what it names.

    The codes and names are read rather than stored, so a task renamed last month
    shows its current name here and its old one nowhere — which is right for a grid
    somebody is filling in, and why the audit trail carries ids and minutes.
    """

    id: UUID
    entry_date: date
    project_id: UUID
    task_id: UUID
    minutes: int
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
    """

    entry_date: date
    weekday: int
    entries: list[EntryRead]
    total_minutes: int
    expected_minutes: int | None
    expectation_source: str | None
    is_holiday: bool


class OverBudgetRead(BaseModel):
    """A day the week ran past, computed on the server from the schedule.

    In the read *and* in the response to a submission. Nothing in the product refuses
    on one: it is the fact an approver will want and the employee can still act on.
    """

    entry_date: date
    total_minutes: int
    expected_minutes: int
    over_minutes: int


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


class TimesheetRead(BaseModel):
    """One row of the caller's own week list."""

    week_start: date
    status: TimesheetStatus
    is_editable: bool
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
    """

    request_id: UUID
    status: ApprovalStatus
    round: int
    submitted_at: datetime | None
    decided_at: datetime | None
    pending_level: int | None
    decisions: list[DecisionRead]


class StatusRead(BaseModel):
    week_start: date
    status: TimesheetStatus
    is_editable: bool
    submitted_at: datetime | None
    approval_request_id: UUID | None
    approval: ApprovalRead | None


# --- projections ------------------------------------------------------------


def entry_read(entry: TimesheetEntry, label: ProjectLabel | None) -> EntryRead:
    return EntryRead(
        id=entry.id,
        entry_date=entry.entry_date,
        project_id=entry.project_id,
        task_id=entry.task_id,
        minutes=entry.minutes,
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
                expected_minutes=day.expected_minutes,
                expectation_source=day.expectation_source,
                is_holiday=day.is_holiday,
            )
            for day in view.days
        ],
    )


def timesheet_read(sheet: Timesheet) -> TimesheetRead:
    return TimesheetRead(
        week_start=sheet.week_start,
        status=sheet.status,
        is_editable=sheet.is_editable,
        submitted_at=sheet.submitted_at,
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
    "DayRead",
    "DecisionRead",
    "EntryRead",
    "EntryUpdate",
    "EntryWrite",
    "OverBudgetRead",
    "StatusRead",
    "TimesheetPageRead",
    "TimesheetRead",
    "WeekRead",
    "approval_read",
    "entry_read",
    "over_budget_read",
    "timesheet_read",
    "week_read",
]
