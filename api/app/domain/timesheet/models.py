"""Weekly timesheet value objects.

One week per person, seven days, and a list of `(project, task, minutes)` per day.
Five decisions are worth reading before the code, and every one of them is about
where a rule *lives* rather than about what the rule says:

* **The week is identified by its Monday.** `week_start` is the key, not a date and
  not a range: "the week of the 9th" is one row, two clients in two timezones agree
  on which row it is, and the database can make it unique. The module refuses a
  `week_start` that is not a Monday with its own code, and migration 0015 refuses it
  as well — a Tuesday key would silently split one week into two rows, which is the
  one defect that would make every total below wrong at once.

* **Minutes are a positive integer capped per entry at 24 hours, not at the day's
  expected hours.** The cap exists so that a typo — `4800` for `480` — is refused
  rather than stored; it does *not* exist to police a day. A day over its expected
  hours is a **warning**: the schedule says what the company expected, the timesheet
  says what happened, and the two disagreeing legitimately is the normal case for
  overtime, a mis-configured schedule, or a day somebody chose to work through. A
  schema that refused it would be a schema that loses the hour somebody actually
  worked — the silent truncation the ticket forbids. So the ceiling is the clock's
  own limit, which no legitimate entry can exceed, and the day's figure is carried
  as `DayTotal.over_expected_minutes` instead.

* **The project's status and its dates are checked twice.** `EntryRules` is what the
  service applies to produce a catalogued refusal, and migration 0015's trigger is
  what PostgreSQL applies to a row written by anything else. The second one is the
  guarantee ticket 27 asked for: an entry against a non-`active` project, or outside
  the project's own dates, cannot be *stored* — not by this service, not by a
  console, not by a script. The trigger validates on write of the entry and never on
  write of the project, so closing or archiving a project keeps the history it
  already has.

* **A submitted week is read-only, and its history is the engine's.** This module
  keeps a status so a week can be shown and listed without reading the engine, and
  the *decisions* — who, when, with what comment, in which round — are read back
  from `ApprovalState`, which already spans every round. There is no second edition
  of that history here, because two copies of "who rejected this and why" are two
  versions of the truth (`domain/personnel/models.py` makes the same argument for the
  same reason).

* **`rejected` is editable and `Reject` is final at the engine.** The two statements
  are compatible and both are load-bearing: this module's `rejected` is a *timesheet*
  state the employee corrects and files again, while a rejection of that particular
  approval request cannot be resubmitted (`ApprovalService.submit` refuses it). A
  resubmission therefore arrives as a fresh request for the same entity, which the
  engine allows once the earlier one is closed and not open. The week's own status is
  what decides, and the engine is asked only when it says the week is fileable.

Ticket 29 adds three ideas to this module, and each one has a home here rather than in
the service:

* **An approved week is locked for ever, and the way to correct it is a supplement.**
  A supplement is a second `Timesheet` for the *same* Monday that points at the
  original (`supersedes_id`) — the original is never edited, because it is what the
  approver signed. `is_locked` and `can_supplement` are the two questions the screen
  asks, and both are answered from the status plus the window.
* **A correction is a reversal plus a new entry.** `EntryType.REVERSAL` is a negative
  row whose `reverses_entry_id` names the locked entry it cancels, so
  `original + reversal + new` is the net because the pair is each other's negation —
  and the three of them are rows, which is what makes the arithmetic auditable rather
  than asserted.
* **The supplementary window is eight weeks, and it is a *lower* bound only.** A week
  that has fallen out of it is globally locked: no write path in this system may touch
  it, and the refusal names how many weeks are left, which is none. Future weeks are
  deliberately unaffected — planning next week is not back-filling, and the window
  exists to close payroll history rather than to stop anybody looking forward.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum
from uuid import UUID

from app.domain.schedule.models import DayExpectation

#: The seven days of a week, Monday first. Named rather than written as `range(7)`
#: because the grid, the totals and the copy all iterate it and "which day is 0"
#: is the kind of question that should not need a comment at each of them.
DAYS_PER_WEEK = 7

#: The longest an entry may be: one day, to the minute. See the module docstring for
#: why this is the ceiling rather than the day's expected hours.
MAX_ENTRY_MINUTES = 24 * 60

#: What the ticket calls 单条上限合理, named so a test can refer to it and a reader
#: does not have to multiply 24 by 60 to find out what the limit is.
MINUTES_PER_DAY = MAX_ENTRY_MINUTES

#: ISO weekday of Monday. `date.weekday()` numbers Monday 0, which is what the
#: schedule module uses throughout, so this is 0 and the constant exists to say so.
MONDAY = 0

#: 补填窗口为最近 8 周: how far back a locked week may still be corrected. Eight and not
#: twelve because a month of payroll is closed on the fifth working day of the next
#: one, and a correction that reaches further back would land in a period finance has
#: already reported.
SUPPLEMENT_WINDOW_WEEKS = 8


class TimesheetStatus(StrEnum):
    """Where a week stands, as the UI has to tell it apart.

    The first three are the ticket's 草稿 / 审批中 / 已通过 / 已驳回. `rejected` is a
    state of the *week* and not a terminal one: a rejected week is the employee's
    again, and `draft` and `rejected` are the two statuses an entry may be written
    in. Which of the two it is matters to the reader — "you have not filed this"
    and "this came back" are different sentences — which is why being returned for
    correction writes `rejected` rather than quietly resetting to `draft`.

    There is no separate `locked`: **approved is the lock**. Ticket 29 asked for
    已锁定, and a second status beside `approved` would be a state nothing could move
    the week out of and a second answer to "was this approved" (`WeekView.is_locked`
    says which status that is, once).
    """

    DRAFT = "draft"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class EntryType(StrEnum):
    """What an entry *is*, which is also the sign of its minutes.

    `normal` is what somebody recorded; `reversal` is one locked entry's negation,
    written by a supplementary submission and pointing at what it cancels. The two
    are a closed set in the database as well, because the sign rule reads this column.
    """

    NORMAL = "normal"
    REVERSAL = "reversal"


#: The statuses a week may be edited in. Spelled out rather than derived by
#: subtraction, for the reason `project.models.RECORDABLE_STATUSES` gives: a status
#: added later must be *named* here before it becomes writable, and the failure mode
#: of a derived set is a new state that quietly accepts edits.
EDITABLE_STATUSES: frozenset[TimesheetStatus] = frozenset(
    {TimesheetStatus.DRAFT, TimesheetStatus.REJECTED}
)


def monday_of(on_date: date) -> date:
    """The Monday of the week `on_date` falls in.

    The one place this module converts a date to a week, so "which week is the 9th
    of March in" has one answer shared by the route, the service and the grid itself.
    """
    return on_date - timedelta(days=on_date.weekday())


def supplement_weeks_left(week_start: date, current_week: date) -> int:
    """How many weeks of supplementary filing `week_start` still has.

    The whole window for the current week and for anything later, one for the week
    seven back, and zero for the week eight back — the first week the window no longer
    covers. One function rather than the same subtraction at the gate, the refusal and
    the screen: a number a person is shown and a number a rule is enforced with have to
    be the same number.
    """
    elapsed = (current_week - week_start).days // DAYS_PER_WEEK
    return max(0, min(SUPPLEMENT_WINDOW_WEEKS, SUPPLEMENT_WINDOW_WEEKS - elapsed))



def assert_monday(week_start: date) -> None:
    """Refuse a week key that is not a Monday, with the module's own code."""
    if week_start.weekday() != MONDAY:
        from app.domain.errors import DomainError
        from app.domain.timesheet.errors import TimesheetErrorCode

        raise DomainError(
            TimesheetErrorCode.TIMESHEET_WEEK_NOT_MONDAY,
            detail=(
                f"{week_start} is a {week_start.strftime('%A')}; a timesheet week is "
                f"keyed by its Monday ({monday_of(week_start)})"
            ),
        )


@dataclass(frozen=True, slots=True)
class Timesheet:
    """One employee's one week, as stored — or one supplement *of* that week.

    `approval_request_id` is the request the week was filed under. It is kept so the
    status endpoint does not have to search the engine by entity, and it is cleared
    implicitly by being replaced on a resubmission — the engine's own `latest_for`
    is what `state_of` uses, so a round-2 request supersedes round 1 without this
    column needing to be a history.

    `supersedes_id` is set on a supplement and names the original sheet, which is the
    direction the link has to run: the original is the record that may not change, so
    nothing about a later correction is written on it. "Which supplements does this
    week have" is the same column read the other way round (`WeekView.supplements`).
    """

    id: UUID
    employee_id: UUID
    week_start: date
    status: TimesheetStatus
    approval_request_id: UUID | None
    submitted_at: datetime | None
    created_at: datetime
    updated_at: datetime
    supersedes_id: UUID | None = None
    is_supplementary: bool = False

    @property
    def week_end(self) -> date:
        """Sunday, inclusive. A week is seven days and the grid shows all of them."""
        return self.week_start + timedelta(days=DAYS_PER_WEEK - 1)

    @property
    def is_editable(self) -> bool:
        return self.status in EDITABLE_STATUSES

    @property
    def is_locked(self) -> bool:
        """Approved, and therefore never writable again — only correctable.

        The ticket's 永久锁定. There is no operation that unlocks it: the way to change
        what an approved week says is a supplement, which leaves this sheet exactly as
        the approver signed it.
        """
        return self.status is TimesheetStatus.APPROVED

    @property
    def days(self) -> tuple[date, ...]:
        return tuple(
            self.week_start + timedelta(days=offset) for offset in range(DAYS_PER_WEEK)
        )


@dataclass(frozen=True, slots=True)
class TimesheetEntry:
    """One day's work on one task, as stored — or the reversal of one such row.

    `is_billable` is the value the project module *resolved* (`RecordTarget`), stored
    rather than derived: a task's flag may be reconfigured later, and a timesheet
    that re-resolved it on read would silently restate what a closed month was worth.
    A reversal carries its original's value for the same reason in the other
    direction: a billable subtotal has to fall by exactly what it rose by.
    """

    id: UUID
    timesheet_id: UUID
    employee_id: UUID
    week_start: date
    entry_date: date
    project_id: UUID
    task_id: UUID
    minutes: int
    is_billable: bool
    note: str | None
    created_at: datetime
    updated_at: datetime
    entry_type: EntryType = EntryType.NORMAL
    reverses_entry_id: UUID | None = None

    @property
    def is_reversal(self) -> bool:
        return self.entry_type is EntryType.REVERSAL


@dataclass(frozen=True, slots=True)
class EntryInput:
    """An entry to write: the request's fields, plus what the server resolved.

    `is_billable` is not a field a caller may state. It is computed by the service
    from the task and its project, exactly as ticket 27's `record-time` decision
    endpoint does, and carried here so the repository writes the server's answer.

    `entry_type` and `reverses_entry_id` are the server's too: a reversal is written
    by the supplementary flow and never by a request, which is what stops a negative
    row from being something a client can simply post.
    """

    entry_date: date
    project_id: UUID
    task_id: UUID
    minutes: int
    is_billable: bool
    note: str | None = None
    entry_type: EntryType = EntryType.NORMAL
    reverses_entry_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class CorrectionInput:
    """One locked entry a supplement corrects.

    `minutes` is the *new* amount, or `None` for "this entry should not exist" — the
    case the reversal alone expresses. The original's project, task and day are
    carried over unless the correction names others, because a correction is about
    what the entry was worth and occasionally about which task it belonged to; it is
    never about which week it was in, which is what the supplement's own link says.
    """

    entry_id: UUID
    minutes: int | None = None
    note: str | None = None
    project_id: UUID | None = None
    task_id: UUID | None = None


class Unset:
    """The type of `UNSET`: a distinct object, never a `None` in disguise."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "UNSET"


#: "This field was not in the patch." One module-level instance, compared by
#: identity — the same sentinel convention the project module uses, and for the same
#: reason: a patch that cannot tell "omitted" from "explicitly null" cannot clear a
#: note.
UNSET = Unset()


@dataclass(frozen=True, slots=True)
class EntryPatch:
    """A change to one entry. Every absent field means "leave it alone".

    `note` carries the `UNSET` sentinel rather than a bare `None`, and the reason is
    the one `project.models.ProjectPatch` records: clearing a note and leaving it
    alone are both real operations, and a convention that read `None` as "absent"
    would make a note impossible to remove.
    """

    entry_date: date | None = None
    project_id: UUID | None = None
    task_id: UUID | None = None
    minutes: int | None = None
    note: str | None | Unset = UNSET


@dataclass(frozen=True, slots=True)
class DayTotal:
    """One day of the grid: what was recorded, and what was expected of it.

    `expected_minutes` comes from the schedule (`ScheduleService.day_expectations`),
    never from the client and never from a stored copy in this module. It is `None`
    when nobody has configured a schedule that reaches this person on this date,
    which is a different fact from a schedule that expects nothing — the same
    distinction `DayExpectation` draws, and the reason the grid can say "no schedule"
    rather than showing a confident zero.

    `total_minutes` is the **net**: the sum of every row of the day, reversals
    included. `gross_minutes` and `reversal_minutes` are what it was reached from, and
    they are carried rather than derived on the client because the two numbers a
    reader has to be able to see — "eight hours minus two" and "six hours" — are not
    the same statement, and a grid that showed only the second would hide the
    correction that produced it.
    """

    entry_date: date
    weekday: int
    entries: tuple[TimesheetEntry, ...] = ()
    total_minutes: int = 0
    expected_minutes: int | None = None
    expectation_source: str | None = None
    is_holiday: bool = False
    gross_minutes: int = 0
    reversal_minutes: int = 0

    @property
    def over_expected_minutes(self) -> int:
        """How far past the day's expectation it went. Zero when it did not.

        A day nobody was expected to work counts too, and that is deliberate: a
        holiday or a rest day is expected to hold *nothing*, so an entry on one is
        over by every minute of it. Excluding those days would silently lose the one
        case where the warning matters most.

        Measured on the net, which is the day as it now stands: a day that recorded
        nine hours and was corrected down to eight is not over any more, and warning
        about it would be reporting a state of affairs the correction removed.
        """
        if self.expected_minutes is None:
            return 0
        return max(0, self.total_minutes - self.expected_minutes)

    @property
    def is_over_expected(self) -> bool:
        return self.over_expected_minutes > 0


@dataclass(frozen=True, slots=True)
class TaskNet:
    """What one task came to over the week, after its reversals.

    The per-task half of the ticket's 净额视图: a day can net to zero while two tasks
    on it moved in opposite directions, and a report that grouped by day alone would
    show a quiet day rather than a correction.
    """

    project_id: UUID
    task_id: UUID
    gross_minutes: int = 0
    reversal_minutes: int = 0
    net_minutes: int = 0


@dataclass(frozen=True, slots=True)
class OverBudgetDay:
    """A day the grid warns about, in the vocabulary the response carries.

    Server-computed, from the schedule, and returned by both the read and the
    submit response: the warning is a fact about the week rather than a client-side
    impression, and a client that computed its own total could disagree with the
    one the server stored.
    """

    entry_date: date
    total_minutes: int
    expected_minutes: int
    over_minutes: int


@dataclass(frozen=True, slots=True)
class WeekView:
    """The grid: seven days, the week's total, and what was expected of it.

    Always seven days, gaps included and empty. A grid that returned only the days
    with entries would make "I forgot Thursday" look like "Thursday does not exist",
    which is the opposite of what somebody filling in a week needs to see.

    `timesheet` is `None` for a week that has never been written — the read that
    creates nothing. Everything a reader needs is still answerable: an unwritten
    week is a draft with no entries, and `status` says so rather than making every
    caller write `timesheet.status if timesheet else "draft"`.

    A week with a supplement in it has **several sheets and one set of entries**: the
    grid is the week, not the document, because that is the only reading under which
    the totals mean what a reader takes them to mean. `timesheet` is the original,
    `supplements` are the corrections filed against it, and `editable_sheet_id` is
    which of them the next entry would land in — which is what lets one screen show a
    locked Monday and an editable correction side by side.
    """

    employee_id: UUID
    week_start: date
    days: tuple[DayTotal, ...]
    timesheet: Timesheet | None = None
    entries_total_minutes: int = 0
    expected_total_minutes: int | None = None
    over_budget_days: tuple[OverBudgetDay, ...] = ()
    sheets: tuple[Timesheet, ...] = ()
    #: The corrections filed against this week, oldest first. Read from the link the
    #: other way round, which is the "which supplements does this week have" half.
    supplements: tuple[Timesheet, ...] = ()
    editable_sheet_id: UUID | None = None
    gross_total_minutes: int = 0
    reversal_total_minutes: int = 0
    tasks: tuple[TaskNet, ...] = ()
    supplement_weeks_left: int = 0
    week_closed: bool = False

    @property
    def week_end(self) -> date:
        return self.week_start + timedelta(days=DAYS_PER_WEEK - 1)

    @property
    def status(self) -> TimesheetStatus:
        """`draft` for a week nobody has written: an empty week is a draft.

        The *original* sheet's status once there is one, which is the week's own
        answer: a week whose original is approved is approved, whatever a supplement
        filed afterwards is doing.
        """
        return self.timesheet.status if self.timesheet is not None else TimesheetStatus.DRAFT

    @property
    def is_editable(self) -> bool:
        """Whether anything in the week may still be written.

        True when one of its sheets is the employee's — a supplement in draft is
        editable even though the original it corrects is locked for ever — and true for
        a week nobody has written at all, because there the *first* write is what
        creates the original sheet. `editable_sheet_id` is `None` in that case and says
        which sheet a write would land in, which is a different question from whether a
        write is allowed.

        A globally closed week is never editable, whatever its sheets say: the window
        and the lock are what the write paths refuse on, so a grid that offered a cell
        there would be offering a refusal.
        """
        if self.week_closed:
            return False
        return self.editable_sheet_id is not None or not self.sheets

    @property
    def is_locked(self) -> bool:
        """Approved: 永久锁定. The entries may not be written by anybody."""
        return self.status is TimesheetStatus.APPROVED

    @property
    def is_supplementary(self) -> bool:
        return self.timesheet is not None and self.timesheet.is_supplementary

    @property
    def can_supplement(self) -> bool:
        """Whether this week could be corrected right now.

        Three conditions, and all three are the ticket's: the week is locked (there is
        nothing to correct in a draft), the window is still open, and no correction is
        already in flight — a second supplement beside an undecided one would be two
        answers to what the week now says.
        """
        open_supplement = any(sheet.is_editable for sheet in self.supplements)
        pending = any(
            sheet.status is TimesheetStatus.PENDING for sheet in self.supplements
        )
        return (
            self.is_locked
            and not self.week_closed
            and self.supplement_weeks_left > 0
            and not open_supplement
            and not pending
        )

    @property
    def approval_request_id(self) -> UUID | None:
        return None if self.timesheet is None else self.timesheet.approval_request_id

    @property
    def submitted_at(self) -> datetime | None:
        return None if self.timesheet is None else self.timesheet.submitted_at

    @property
    def has_timesheet(self) -> bool:
        return self.timesheet is not None


@dataclass(frozen=True, slots=True)
class ProjectLabel:
    """What a cell shows instead of two uuids: the project and the task, named.

    Carried separately from the entry because it is *read* rather than stored: the
    entry keeps ids, and a task renamed last month shows its current name here and
    its old one nowhere — which is right for a grid somebody is filling in, and the
    reason the audit trail carries minutes rather than labels.
    """

    project_id: UUID
    task_id: UUID
    project_code: str | None = None
    task_code: str | None = None
    task_name_es: str | None = None
    task_name_en: str | None = None


@dataclass(frozen=True, slots=True)
class WeekInput:
    """A week a caller asked about, before anything has been read or written."""

    employee_id: UUID
    week_start: date


@dataclass(frozen=True, slots=True)
class TimesheetPage:
    """The caller's own weeks, newest first."""

    items: list[Timesheet] = field(default_factory=list)
    total: int = 0
    limit: int = 50
    offset: int = 0


@dataclass(frozen=True, slots=True)
class WeekExpectations:
    """The schedule's answer for the seven days of one week, keyed by date."""

    by_date: dict[date, DayExpectation]

    def expected_minutes(self, on_date: date) -> int | None:
        """What the schedule expected, or None when nothing was configured."""
        expectation = self.by_date.get(on_date)
        if expectation is None:
            return None
        return expectation.expected_minutes

    def source(self, on_date: date) -> str | None:
        expectation = self.by_date.get(on_date)
        return None if expectation is None else str(expectation.source)

    def is_holiday(self, on_date: date) -> bool:
        expectation = self.by_date.get(on_date)
        return expectation is not None and expectation.is_holiday


__all__ = [
    "DAYS_PER_WEEK",
    "EDITABLE_STATUSES",
    "MAX_ENTRY_MINUTES",
    "MINUTES_PER_DAY",
    "MONDAY",
    "SUPPLEMENT_WINDOW_WEEKS",
    "UNSET",
    "CorrectionInput",
    "DayTotal",
    "EntryInput",
    "EntryPatch",
    "EntryType",
    "OverBudgetDay",
    "ProjectLabel",
    "TaskNet",
    "Timesheet",
    "TimesheetEntry",
    "TimesheetPage",
    "TimesheetStatus",
    "Unset",
    "WeekExpectations",
    "WeekInput",
    "WeekView",
    "assert_monday",
    "monday_of",
    "supplement_weeks_left",
]
