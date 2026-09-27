"""Overtime value objects: the request, the record, its ledger, and the month.

The vocabulary is small, and each member exists because a question needs it:

* `OvertimeRequest` is the document: which day, how many minutes are expected, why,
  and where the approval engine got to. Its *state* is derived —
  `state_of_request` — from the row and the engine, never stored, for the reason
  `attendance/corrections.py` gives: a copied status is the one that goes stale.
* `OvertimeRecord` is the fact an approval produced: the minutes two people agreed to,
  the minutes the day actually held, and the smaller of the two. It is written into a
  month bucket so the monthly summary is a group-by rather than a date range.
* `OvertimeEntry` is one movement of a record, with the three figures that followed it.
  It is the "确认或调整的值必须留痕" the ticket asks for, and it is append-only in the
  database.
* `MonthlyTotal` is one employee's month, which is what "员工可查看自己的加班累计"
  and the export are both summed from.

**Two figures, and never one.** `approved_minutes` is what was agreed in advance,
`worked_minutes` is what the attendance record says the day held, `computed_minutes` is
`min` of the two, and `confirmed_minutes` is HR's — stored beside the computed figure
rather than over it, with the reason. Nothing in this module ever folds them into one
column: the whole point of the ticket's 取较小值 plus 标记待确认 is that a reader can see
*which* of the two produced the number, and a single "final minutes" column would answer
that question with "trust me".

**There is no rate and no amount anywhere in this vocabulary**, which is the ticket's
requirement stated where a later ticket would otherwise add one: this system
accumulates and exports hours, and overtime pay is finance's calculation (Q12).
"""

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from uuid import UUID

from app.domain.approval.models import ApprovalState, ApprovalStatus

#: The entity type the approval engine files an overtime request under. The engine
#: stores it and never interprets it; this module is the only reader.
ENTITY_TYPE = "overtime_request"

#: How many minutes a day holds. The same literal as `models/overtime.MAX_DAY_MINUTES`
#: and the migration's CHECK: a request for more than a day cannot be worked, and the
#: bound exists to refuse a typo rather than to express policy.
MAX_DAY_MINUTES = 1440

#: How far ahead a request may be filed. A year: overtime is about a day somebody is
#: about to work, and a request dated 2035 is either a typo or a calendar somebody
#: should not be keeping here.
MAX_DAYS_AHEAD = 366

#: The years whose months this module will summarise or export. A ledger of overtime is
#: a working-time record, so the bound is the same one the expected-hours snapshot
#: carries rather than "any integer".
MIN_PERIOD_YEAR = 2000
MAX_PERIOD_YEAR = 2200


class OvertimeEntryType(StrEnum):
    """One movement of a record's figures."""

    #: The pre-approved minutes arriving: the record written when the engine approved.
    APPROVE = "approve"
    #: The day's arithmetic: worked minutes read from attendance, and the smaller of
    #: the two figures written. Never runs before the day is over.
    SETTLE = "settle"
    #: HR's figure, with the reason. Stored in its own column and beside the computed
    #: one, never instead of it.
    CONFIRM = "confirm"


class OvertimeRequestState(StrEnum):
    """What a client shows. Derived from the row and the engine, never stored.

    Five facts, and each is different: nobody has filed it (`draft`), it is with the
    engine (`in_approval`), the engine approved it (`approved`), or it ended without
    one — the engine said no (`rejected`) or the requester stopped it (`withdrawn`).
    """

    DRAFT = "draft"
    IN_APPROVAL = "in_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


#: What the engine's status means for a filed request. `draft` here is a document the
#: engine *returned for correction*: back in the requester's hands, which is the same
#: place a draft sits.
_STATE_OF_ENGINE: dict[ApprovalStatus, OvertimeRequestState] = {
    ApprovalStatus.DRAFT: OvertimeRequestState.DRAFT,
    ApprovalStatus.PENDING_FIRST: OvertimeRequestState.IN_APPROVAL,
    ApprovalStatus.PENDING_SECOND: OvertimeRequestState.IN_APPROVAL,
    ApprovalStatus.APPROVED: OvertimeRequestState.APPROVED,
    ApprovalStatus.REJECTED: OvertimeRequestState.REJECTED,
    ApprovalStatus.WITHDRAWN: OvertimeRequestState.WITHDRAWN,
}


def state_of_request(
    request: "OvertimeRequest", approval_status: ApprovalStatus | None
) -> OvertimeRequestState:
    """The state a reader sees, from the row and the engine and nothing else.

    The order of the three local answers is the order of the facts: a withdrawal is
    the requester's own act and closes the document whatever the engine says, an
    approval that has been resolved is in force, and a request nobody filed is a draft
    without asking the engine.
    """
    if request.withdrawn_at is not None:
        return OvertimeRequestState.WITHDRAWN
    if request.approved_at is not None:
        return OvertimeRequestState.APPROVED
    if request.approval_request_id is None:
        return OvertimeRequestState.DRAFT
    if approval_status is None:
        # Filed, and the request row cannot be read: in flight is the honest answer.
        return OvertimeRequestState.IN_APPROVAL
    return _STATE_OF_ENGINE.get(approval_status, OvertimeRequestState.IN_APPROVAL)


def month_bucket_of(business_date: date) -> str:
    """The month a day's overtime is counted in: `YYYY-MM`.

    The business day is Madrid by construction — the attendance module converts once,
    on the way in — so its month *is* the Madrid month. Converting an instant here
    would be exactly the confusion `attendance/business_day.py` exists to prevent, and
    it would put a night shift that ends at 00:30 into the wrong month half the year.
    """
    return f"{business_date.year:04d}-{business_date.month:02d}"


def period_of(month: str) -> tuple[int, int]:
    """`YYYY-MM` as a year and a month, or `None` when it is not one.

    The one place a period string is parsed: the export, the summary and the settle
    sweep all take the same argument, and three parsers would eventually disagree
    about what `2026-13` means. Returns nothing rather than raising, so the refusal —
    which is a catalogued error and therefore the service's vocabulary — is raised by
    the caller that knows which act was being attempted.
    """
    parts = month.split("-")
    # The width is part of the shape: `2026-3` is a month somebody meant and not the one
    # this module stores, and accepting it would answer an empty month rather than
    # refusing a malformed one.
    if (
        len(parts) != 2
        or len(parts[0]) != 4
        or len(parts[1]) != 2
        or not all(part.isdigit() and part.isascii() for part in parts)
    ):
        return 0, 0
    year, number = int(parts[0]), int(parts[1])
    if not MIN_PERIOD_YEAR <= year <= MAX_PERIOD_YEAR or not 1 <= number <= 12:
        return 0, 0
    return year, number


@dataclass(slots=True, frozen=True)
class OvertimeRequest:
    """One request, as stored."""

    id: UUID
    employee_id: UUID
    business_date: date
    expected_minutes: int
    reason: str
    approval_request_id: UUID | None = None
    submitted_at: datetime | None = None
    approved_at: datetime | None = None
    withdrawn_at: datetime | None = None
    settled_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def is_approved(self) -> bool:
        """In force: the engine approved it and nobody has withdrawn it."""
        return self.approved_at is not None and self.withdrawn_at is None

    @property
    def needs_resolving(self) -> bool:
        """Filed, and this module has not finished with the document yet."""
        return self.approval_request_id is not None and self.settled_at is None


@dataclass(slots=True, frozen=True)
class OvertimeRequestInput:
    """A request about to be written."""

    employee_id: UUID
    business_date: date
    expected_minutes: int
    reason: str


@dataclass(slots=True, frozen=True)
class OvertimeRequestPatch:
    """What an edit to a draft may change. `None` leaves a field alone."""

    business_date: date | None = None
    expected_minutes: int | None = None
    reason: str | None = None


@dataclass(slots=True, frozen=True)
class OvertimeRequestQuery:
    """Which requests to read. `employee_id` is the subject, never the requester."""

    employee_id: UUID | None = None
    state: OvertimeRequestState | None = None
    limit: int = 50
    offset: int = 0


@dataclass(slots=True, frozen=True)
class OvertimeRequestView:
    """A request with the two answers that are not on its own row."""

    request: OvertimeRequest
    state: OvertimeRequestState
    approval: ApprovalState | None = None
    #: The record an approval wrote, when there is one. Present so a client can route
    #: from the document to the fact without a second request.
    record_id: UUID | None = None


@dataclass(slots=True, frozen=True)
class OvertimeRecord:
    """Approved overtime for one person's one day, as stored."""

    id: UUID
    request_id: UUID
    employee_id: UUID
    business_date: date
    month_bucket: str
    approved_minutes: int
    worked_minutes: int | None = None
    computed_minutes: int | None = None
    needs_confirmation: bool = False
    confirmed_minutes: int | None = None
    confirmed_by_employee_id: UUID | None = None
    confirmed_at: datetime | None = None
    confirmation_note: str | None = None
    settled_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def is_settled(self) -> bool:
        """Whether the day's arithmetic has run. The stamp and the figure agree."""
        return self.settled_at is not None and self.computed_minutes is not None

    @property
    def effective_minutes(self) -> int:
        """The figure in force: HR's, else the settled smaller-of, else the approved.

        The one place the three columns are folded into one answer, and it is a
        *read*: nothing stores the result. It is what `attendance_daily.overtime_minutes`
        carries and what the monthly summary sums, so the day a reader opens and the
        file finance downloads are the same number.
        """
        if self.confirmed_minutes is not None:
            return self.confirmed_minutes
        if self.computed_minutes is not None:
            return self.computed_minutes
        return self.approved_minutes

    @property
    def difference_minutes(self) -> int | None:
        """How far the day's actual hours were from the approved ones, if known."""
        if self.worked_minutes is None:
            return None
        return abs(self.approved_minutes - self.worked_minutes)


@dataclass(slots=True, frozen=True)
class NewOvertimeRecord:
    """The record an approval writes. Every figure but the approved one is unknown."""

    request_id: UUID
    employee_id: UUID
    business_date: date
    month_bucket: str
    approved_minutes: int


@dataclass(slots=True, frozen=True)
class OvertimeEntry:
    """One movement of a record, with the figures that followed it."""

    id: UUID
    record_id: UUID
    entry_type: OvertimeEntryType
    approved_minutes: int
    computed_minutes: int | None = None
    confirmed_minutes: int | None = None
    note: str | None = None
    created_by_employee_id: UUID | None = None
    created_at: datetime | None = None

    @property
    def effective_minutes(self) -> int:
        """The record's figure in force after this movement. Same fold as the record."""
        if self.confirmed_minutes is not None:
            return self.confirmed_minutes
        if self.computed_minutes is not None:
            return self.computed_minutes
        return self.approved_minutes


@dataclass(slots=True, frozen=True)
class OvertimeRecordView:
    """A record with the history that produced it."""

    record: OvertimeRecord
    history: tuple[OvertimeEntry, ...] = ()


@dataclass(slots=True, frozen=True)
class OvertimeRecordQuery:
    """Which records to read. `employee_id` is the subject, never the requester."""

    employee_id: UUID | None = None
    month: str | None = None
    #: HR's queue, when a screen wants it: only the records waiting for a person.
    needs_confirmation: bool | None = None
    limit: int = 50
    offset: int = 0


@dataclass(slots=True, frozen=True)
class MonthlyTotal:
    """One employee's overtime in one month.

    What "员工可查看自己的加班累计" is answered with, and the shape the monthly export
    is a detail of. `awaiting_confirmation` is the count of records the two figures
    disagreed about and nobody has looked at yet — the number that says whether the
    month is finished rather than merely summed.
    """

    employee_id: UUID
    employee_name: str
    records: int
    approved_minutes: int
    effective_minutes: int
    awaiting_confirmation: int


@dataclass(slots=True, frozen=True)
class MonthlySummary:
    """A month, per employee, plus the month's own totals."""

    month: str
    totals: tuple[MonthlyTotal, ...] = ()

    @property
    def approved_minutes(self) -> int:
        return sum(row.approved_minutes for row in self.totals)

    @property
    def effective_minutes(self) -> int:
        return sum(row.effective_minutes for row in self.totals)

    @property
    def awaiting_confirmation(self) -> int:
        return sum(row.awaiting_confirmation for row in self.totals)


@dataclass(slots=True, frozen=True)
class OvertimeExportRow:
    """One line of the monthly file: the six facts the ticket names, and nothing else.

    Employee number, name, department, date, approved minutes and confirmed minutes —
    a *file* shape rather than a domain object, which is why it carries the name as
    the file writes it (`Apellidos, Nombre`) and the department as a string. No rate,
    no multiplier and no amount: what an hour costs is finance's calculation (Q12),
    and this system exports hours.
    """

    employee_no: str | None
    employee_name: str
    department: str | None
    business_date: date
    approved_minutes: int
    #: What the day came to: what HR confirmed, or the settled smaller-of. Empty while
    #: the day is still open — the approved column already states what was agreed, and
    #: repeating it here would make a file that cannot tell a computed day from a
    #: running one.
    confirmed_minutes: int | None


@dataclass(slots=True, frozen=True)
class ResolveFailure:
    """One request whose record could not be written, and why."""

    request_id: UUID
    code: str
    detail: str


@dataclass(slots=True, frozen=True)
class ResolveReport:
    """What one run of the resolve sweep did. Nothing to do is the ordinary answer."""

    resolved: tuple[OvertimeRecord, ...] = ()
    failed: tuple[ResolveFailure, ...] = ()

    @property
    def resolved_count(self) -> int:
        return len(self.resolved)

    def failure_for(self, request_id: UUID) -> ResolveFailure | None:
        return next(
            (failure for failure in self.failed if failure.request_id == request_id), None
        )


@dataclass(slots=True, frozen=True)
class SettleFailure:
    """One record whose day could not be computed, and why."""

    record_id: UUID
    code: str
    detail: str


@dataclass(slots=True, frozen=True)
class SettleReport:
    """What one run of the settle sweep did.

    `skipped` counts the records whose day has not ended yet: they are examined and
    deliberately left alone, which is a different answer from "there was nothing to
    do" and is worth reporting on its own.
    """

    settled: tuple[OvertimeRecord, ...] = ()
    skipped: int = 0
    failed: tuple[SettleFailure, ...] = ()

    @property
    def settled_count(self) -> int:
        return len(self.settled)


__all__ = [
    "ENTITY_TYPE",
    "MAX_DAYS_AHEAD",
    "MAX_DAY_MINUTES",
    "MAX_PERIOD_YEAR",
    "MIN_PERIOD_YEAR",
    "MonthlySummary",
    "MonthlyTotal",
    "NewOvertimeRecord",
    "OvertimeEntry",
    "OvertimeEntryType",
    "OvertimeExportRow",
    "OvertimeRecord",
    "OvertimeRecordQuery",
    "OvertimeRecordView",
    "OvertimeRequest",
    "OvertimeRequestInput",
    "OvertimeRequestPatch",
    "OvertimeRequestQuery",
    "OvertimeRequestState",
    "OvertimeRequestView",
    "ResolveFailure",
    "ResolveReport",
    "SettleFailure",
    "SettleReport",
    "month_bucket_of",
    "period_of",
    "state_of_request",
]
