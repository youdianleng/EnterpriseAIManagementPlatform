"""Leave value objects: the type, the year's balance, its ledger, and the request.

The vocabulary is small, and each member exists because a question needs it:

* `LeaveType` is the catalogue row — which kind of leave, and the four flags that
  decide what it means to the rest of the system.
* `LeaveBalance` is one person's one year of one type, with the four figures
  `docs/DESIGN.md` §3.2 names. `remaining_days` is derived here rather than stored,
  because a stored remainder is a fifth answer that eventually disagrees with the
  four that produce it.
* `LeaveBalanceEntry` is one movement and the totals that followed it. It is the
  "history of how the balance was computed" the ticket asks for, and it is
  append-only in the database.
* `LeaveRequest` is the document: which type, which dates, how many working days
  they are worth, and where it is in the approval engine. Its *state* is derived —
  `state_of_request` — from the row and the engine, never stored, for the reason
  `attendance/corrections.py` gives: a copied status is the one that goes stale.

**The cross-year rule, stated once because everything else follows from it.** A
request that spans two years is split at the year boundary, each year's share is
the working days that fall *in that year*, and each share is checked against and
reserved from *that year's* balance. There is no "borrow from next year" and no
pro-rating by month: the days somebody is away in December are December's, and the
days in January are January's, which is also how a reader checks the arithmetic by
hand. A year with no balance row yet gets one the moment it is needed —
`leave_balances.entitled_days` materialised from the configured allowance, an
explicit `grant` in that year's ledger, and `carried_over_days = 0` because
carrying days over is a decision somebody makes and not a default this module may
invent. `allocations_of` reads the split back from the ledger rather than
recomputing it, so a schedule edited afterwards cannot change what a past request
was charged to.

**`EXTRA_FIELDS` does not exist here, on purpose.** There is no `reason`, no `note`
and no other free text on a request: §8 of the design records the AEPD position
that a sick leave is a special category of data, and this system records the type
and the dates. The one text a request may carry is `attachment_reference`, which
names a file stored elsewhere and is constrained by the database to look like a
storage key.
"""

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from uuid import UUID

from app.domain.approval.models import ApprovalState, ApprovalStatus

#: The entity type the approval engine files a leave request under. The engine
#: stores it and never interprets it; this module is the only reader.
ENTITY_TYPE = "leave_request"

#: The shape of an attachment reference: a storage key, not prose. Mirrored by
#: `app/models/leave.py`'s `ck_leave_requests_attachment_reference`, exactly as
#: `schedule/calculation.validate_day` mirrors the schedule table's constraint — the
#: service refuses it as a catalogued 422 and the database refuses it as a backstop,
#: and the failure this prevents is a diagnosis typed into the one text field this
#: module has.
ATTACHMENT_REFERENCE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$"


#: Where a balance's days came from, or went. A closed set: an unknown movement
#: would be a number nobody can interpret, and the database refuses one.
class LeaveEntryType(StrEnum):
    """One movement of a balance."""

    #: The allowance materialised into a new row. Written once, when the year is
    #: first needed, so "how did this start" is answerable from the ledger alone.
    GRANT = "grant"
    #: Days brought from the previous year, by somebody who decided to.
    CARRY_OVER = "carry_over"
    #: A figure somebody corrected.
    ADJUSTMENT = "adjustment"
    #: A filed request holds its days: `pending` up.
    RESERVE = "reserve"
    #: A rejection or a withdrawal of a request that never took effect gives the
    #: reserved days back: `pending` down.
    RELEASE = "release"
    #: An approval spends them: `pending` down, `used` up.
    CONSUME = "consume"
    #: An approved leave withdrawn before it started gives *spent* days back:
    #: `used` down. Its own member rather than a second `release`, because the
    #: ledger has to say which of the two happened for a fold over it to be
    #: unambiguous.
    REFUND = "refund"


class LeaveRequestState(StrEnum):
    """What a client shows. Derived from the row and the engine, never stored.

    Six facts, and each is different: nobody has filed it (`draft`), it is with the
    engine (`in_approval`), the engine approved it (`approved`), or it ended
    without one — the engine said no (`rejected`) or the requester stopped it
    (`withdrawn`).
    """

    DRAFT = "draft"
    IN_APPROVAL = "in_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


#: What the engine's status means for a filed request. `draft` here is a document
#: the engine *returned for correction*: back in the requester's hands, which is the
#: same place a draft sits.
_STATE_OF_ENGINE: dict[ApprovalStatus, LeaveRequestState] = {
    ApprovalStatus.DRAFT: LeaveRequestState.DRAFT,
    ApprovalStatus.PENDING_FIRST: LeaveRequestState.IN_APPROVAL,
    ApprovalStatus.PENDING_SECOND: LeaveRequestState.IN_APPROVAL,
    ApprovalStatus.APPROVED: LeaveRequestState.APPROVED,
    ApprovalStatus.REJECTED: LeaveRequestState.REJECTED,
    ApprovalStatus.WITHDRAWN: LeaveRequestState.WITHDRAWN,
}


def state_of_request(
    request: "LeaveRequest", approval_status: ApprovalStatus | None
) -> LeaveRequestState:
    """The state a reader sees, from the row and the engine and nothing else.

    The order of the three local answers is the order of the facts: a withdrawal is
    the requester's own act and closes the document whatever the engine says, an
    approval that has been settled is in force, and a request nobody filed is a
    draft without asking the engine.
    """
    if request.withdrawn_at is not None:
        return LeaveRequestState.WITHDRAWN
    if request.approved_at is not None:
        return LeaveRequestState.APPROVED
    if request.approval_request_id is None:
        return LeaveRequestState.DRAFT
    if approval_status is None:
        # Filed, and the request row cannot be read: in flight is the honest answer.
        return LeaveRequestState.IN_APPROVAL
    return _STATE_OF_ENGINE.get(approval_status, LeaveRequestState.IN_APPROVAL)


@dataclass(slots=True, frozen=True)
class LeaveType:
    """One kind of leave, as stored."""

    id: UUID
    code: str
    name_es: str
    name_en: str
    is_paid: bool
    requires_attachment: bool
    counts_against_annual: bool
    is_active: bool

    def __str__(self) -> str:  # pragma: no cover - display aid
        return self.code


@dataclass(slots=True, frozen=True)
class LeaveTypeInput:
    """A type about to be written."""

    code: str
    name_es: str
    name_en: str
    is_paid: bool = True
    requires_attachment: bool = False
    counts_against_annual: bool = False


@dataclass(slots=True, frozen=True)
class LeaveTypePatch:
    """What an edit may change. `None` leaves a field alone; `code` is not here.

    The code is what a request, a report and an import name a type by, and the
    balances refer to the row it identifies — renaming it would rewrite what an old
    report meant, so it is fixed for good rather than patchable.
    """

    name_es: str | None = None
    name_en: str | None = None
    is_paid: bool | None = None
    requires_attachment: bool | None = None
    counts_against_annual: bool | None = None
    is_active: bool | None = None


@dataclass(slots=True, frozen=True)
class LeaveBalance:
    """One person's one year of one type."""

    employee_id: UUID
    year: int
    leave_type_id: UUID
    entitled_days: int
    carried_over_days: int = 0
    used_days: int = 0
    pending_days: int = 0
    #: None on a *projected* row: the allowance a year nobody has needed yet would
    #: get, shown by a read that must not create the ledger account as a side effect.
    id: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def remaining_days(self) -> int:
        """Entitled plus carried over, less what is spent and what is reserved.

        The one figure a refusal has to state, and the figure a request is checked
        against — computed in one place so the two cannot differ.
        """
        return (
            self.entitled_days + self.carried_over_days - self.used_days - self.pending_days
        )


@dataclass(slots=True, frozen=True)
class LeaveBalanceEntry:
    """One movement, with the totals that followed it."""

    id: UUID
    balance_id: UUID
    entry_type: LeaveEntryType
    days: int
    entitled_days: int
    carried_over_days: int
    used_days: int
    pending_days: int
    leave_request_id: UUID | None = None
    note: str | None = None
    created_by_employee_id: UUID | None = None
    created_at: datetime | None = None

    @property
    def remaining_days(self) -> int:
        return (
            self.entitled_days + self.carried_over_days - self.used_days - self.pending_days
        )


@dataclass(slots=True, frozen=True)
class LeaveBalanceView:
    """A balance, the type it is of, and the history that produced it.

    `projected` marks the row a read returned without writing: the year has no
    ledger account yet, and the balance shown is the allowance the configured
    parameter would grant. Its `id` is None, so a client cannot mistake it for a row
    it may act on.
    """

    balance: LeaveBalance
    leave_type: LeaveType
    history: tuple[LeaveBalanceEntry, ...] = ()
    projected: bool = False


@dataclass(slots=True, frozen=True)
class BalanceGrant:
    """What HR may set on somebody's year. Omitted fields are left alone."""

    entitled_days: int | None = None
    carried_over_days: int | None = None
    #: Why the figure was changed. About the allowance, never about anybody's
    #: health — and the only prose this module stores.
    note: str | None = None


@dataclass(slots=True, frozen=True)
class LeaveRequest:
    """One request, as stored."""

    id: UUID
    employee_id: UUID
    leave_type_id: UUID
    start_date: date
    end_date: date
    business_days_count: int
    approval_request_id: UUID | None = None
    attachment_reference: str | None = None
    submitted_at: datetime | None = None
    approved_at: datetime | None = None
    withdrawn_at: datetime | None = None
    settled_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def covers(self, on_date: date) -> bool:
        return self.start_date <= on_date <= self.end_date

    @property
    def is_on_leave(self) -> bool:
        """In force: approved, and not withdrawn by the requester.

        What the attendance scan asks through this module's `LeaveLookup`.
        """
        return self.approved_at is not None and self.withdrawn_at is None

    @property
    def needs_settling(self) -> bool:
        return self.approval_request_id is not None and self.settled_at is None


@dataclass(slots=True, frozen=True)
class LeaveRequestInput:
    """A request about to be written. The day count is the service's answer."""

    employee_id: UUID
    leave_type_id: UUID
    start_date: date
    end_date: date
    business_days_count: int
    attachment_reference: str | None = None


@dataclass(slots=True, frozen=True)
class LeaveRequestQuery:
    """Which requests to read. `employee_id` is the subject, never the requester."""

    employee_id: UUID | None = None
    state: LeaveRequestState | None = None
    limit: int = 50
    offset: int = 0


@dataclass(slots=True, frozen=True)
class YearAllocation:
    """How much of a request one year's balance was charged, and where.

    Read back from the ledger rather than recomputed: the split was decided when
    the request was reserved, and a schedule edited afterwards must not change what
    a past request cost.
    """

    year: int
    days: int
    balance_id: UUID


@dataclass(slots=True, frozen=True)
class LeaveRequestView:
    """A request with the three answers that are not on its own row."""

    request: LeaveRequest
    state: LeaveRequestState
    leave_type: LeaveType
    approval: ApprovalState | None = None
    allocations: tuple[YearAllocation, ...] = ()


@dataclass(slots=True, frozen=True)
class LeaveDay:
    """One date covered by approved leave, for the calendar an attendance screen shows."""

    employee_id: UUID
    business_date: date
    leave_type_code: str
    leave_type_id: UUID
    request_id: UUID


@dataclass(slots=True, frozen=True)
class SettleFailure:
    """One request whose reservation could not be resolved, and why."""

    request_id: UUID
    code: str
    detail: str


@dataclass(slots=True, frozen=True)
class SettleReport:
    """What one run of the settle sweep did. Nothing to do is the ordinary answer."""

    settled: tuple[LeaveRequest, ...] = ()
    failed: tuple[SettleFailure, ...] = ()

    @property
    def settled_count(self) -> int:
        return len(self.settled)

    def failure_for(self, request_id: UUID) -> SettleFailure | None:
        return next(
            (failure for failure in self.failed if failure.request_id == request_id), None
        )


__all__ = [
    "ENTITY_TYPE",
    "BalanceGrant",
    "LeaveBalance",
    "LeaveBalanceEntry",
    "LeaveBalanceView",
    "LeaveDay",
    "LeaveEntryType",
    "LeaveRequest",
    "LeaveRequestInput",
    "LeaveRequestQuery",
    "LeaveRequestState",
    "LeaveRequestView",
    "LeaveType",
    "LeaveTypeInput",
    "LeaveTypePatch",
    "SettleFailure",
    "SettleReport",
    "YearAllocation",
    "state_of_request",
]
