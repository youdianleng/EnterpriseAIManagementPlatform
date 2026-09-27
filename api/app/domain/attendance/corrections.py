"""A correction is a document: which punch, what it should have been, and why.

The request is the unit, not the edit. Somebody noticed that the record of their
day is wrong, wrote down what it should say and why (DESIGN D25), and handed that
to the approval engine; **nothing about the punch moves until two levels have
agreed**. Ticket 21 shipped the stream and the derivation, ticket 23 shipped the
anomalies and the entry point a correction closes them through, and this module is
the document in between.

Five decisions are worth reading before the code:

* **The document names a day and a kind, not an event id.** "The clock_out of the
  21st should be 18:00" is what a person knows; an event id is what the database
  knows, and a request form that asked for one would be asking the employee to read
  the stream. `CorrectionTarget` is what the flow resolves from the pair.
* **Approval appends; it never edits.** The appended row is a `correction` event
  pointing at the chain's newest row for that punch, so a second correction reads
  as the continuation of the first rather than as a second opinion beside it —
  `original → correction → correction` is the order the screen shows and the order
  the derivation resolves. The punch itself keeps every byte it was written with,
  and the database role cannot rewrite it (migration 0012).
* **A punch that was never made is made up, in the same chain.** A forgotten
  clock_out has no row to point at, so approval appends the missing punch itself
  (`source = 'correction'`) rather than inventing a correction of nothing: the day
  becomes `ok`, the anomaly the night's pass raised is cleared, and the request that
  produced it says why.
* **The day is re-derived, and only what the day no longer shows is resolved.**
  `AnomalyService.resolve_for_correction` is asked once the append has landed; a
  correction that moves a clock_in to 11:00 leaves the lateness standing, because
  the day still shows it.
* **The chain is what a reader gets, and the newest row is what the day reads.**
  `derivation.chain_tip` decides both, so the screen and the arithmetic cannot
  disagree about which correction is in force.

What the document deliberately does not have: an effective date (a correction is
about a day that has already happened, and "approved" *is* "in force"), a status
column (the three facts a state is derived from are already stored, and a copy is
the one that goes stale), and any in-place edit of a filed document. A request that
was rejected is replaced by a new document, and the two stay readable side by side —
which is the whole reason `attendance_corrections` has no unique key on the punch it
is about.

**What the flow cannot express, and refuses rather than guesses.** The document
names a day and a kind, and a day with two shifts has two clock_outs: "the clock_out
of that day" does not say which one is meant. That case is refused
(`ATTENDANCE_CORRECTION_TARGET_UNRESOLVED`) rather than resolved to whichever row a
query returned first, because restating the wrong punch would leave a working-time
record that is wrong in a way nobody asked about.
"""

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from uuid import UUID

from app.domain.approval.models import ApprovalState, ApprovalStatus
from app.domain.attendance.models import AttendanceEvent, EventType

#: The entity type the approval engine files a correction under. The engine stores
#: it and never interprets it; this module is the only reader, and it is what the
#: audit trail and the notification payload carry.
ENTITY_TYPE = "attendance_correction"


class CorrectionState(StrEnum):
    """What the client shows, derived from the row and the engine and nothing else.

    Six states, and each is a different fact about the document: nobody has filed
    it (`draft`), it is with the engine (`in_approval`), the engine said yes and the
    append has not happened yet (`approved` — the gap is real: a crash, or a retry
    that has not run), it took effect (`applied`), or it ended without one
    (`rejected`, `withdrawn`).

    There is deliberately no `status` column behind this. The three facts the state
    is computed from — `approval_request_id`, the engine's status and `applied_at` —
    are all already stored, so a copied status would be a second answer to "was this
    approved", and the copy is the one that goes stale.
    """

    DRAFT = "draft"
    IN_APPROVAL = "in_approval"
    APPROVED = "approved"
    APPLIED = "applied"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


#: What the engine's status means for a document that has not been applied. Written
#: as a table rather than as a chain of `if`s at each site, so "which engine statuses
#: are in flight" has one answer. `applied` wins over all of it — see
#: `state_of_correction` — because the append is the fact that matters.
_STATE_OF_ENGINE: dict[ApprovalStatus, CorrectionState] = {
    # Returned for correction: back in the requester's hands, which is what `draft`
    # means for this document too — the *round* is what changed, and the engine
    # keeps both.
    ApprovalStatus.DRAFT: CorrectionState.DRAFT,
    ApprovalStatus.PENDING_FIRST: CorrectionState.IN_APPROVAL,
    ApprovalStatus.PENDING_SECOND: CorrectionState.IN_APPROVAL,
    ApprovalStatus.APPROVED: CorrectionState.APPROVED,
    ApprovalStatus.REJECTED: CorrectionState.REJECTED,
    ApprovalStatus.WITHDRAWN: CorrectionState.WITHDRAWN,
}


def state_of_correction(
    correction: "Correction", approval_status: ApprovalStatus | None
) -> CorrectionState:
    """The state a reader sees, from the row and the engine and nothing else.

    Three answers before the engine is consulted at all: an applied document is
    applied whatever the engine says (the append is the fact that matters), a
    document nobody has filed is a draft, and a document that was filed but whose
    engine status cannot be read is in approval — the honest reading of "it left the
    requester's hands and nothing here says where it got to".
    """
    if correction.applied_at is not None:
        return CorrectionState.APPLIED
    if correction.approval_request_id is None:
        return CorrectionState.DRAFT
    if approval_status is None:
        return CorrectionState.IN_APPROVAL
    return _STATE_OF_ENGINE.get(approval_status, CorrectionState.IN_APPROVAL)


@dataclass(slots=True, frozen=True)
class Correction:
    """One document, as stored."""

    id: UUID
    #: Whose punch it is about. Not necessarily the requester: HR files corrections
    #: about other people, and that difference is the whole of 事后修正.
    employee_id: UUID
    business_date: date
    #: `clock_in` or `clock_out` (`PUNCH_EVENT_TYPES`). A correction is about a
    #: punch, so the field cannot carry `correction`.
    kind: EventType
    corrected_at: datetime
    reason: str
    requested_by_employee_id: UUID
    approval_request_id: UUID | None = None
    applied_event_id: UUID | None = None
    applied_at: datetime | None = None
    submitted_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class CorrectionInput:
    """A document about to be written."""

    employee_id: UUID
    business_date: date
    kind: EventType
    corrected_at: datetime
    reason: str
    requested_by_employee_id: UUID


@dataclass(slots=True, frozen=True)
class CorrectionPatch:
    """What a draft may be changed to. `None` on either field leaves it alone."""

    corrected_at: datetime | None = None
    reason: str | None = None


@dataclass(slots=True, frozen=True)
class CorrectionQuery:
    """Which documents to read. `employee_id` is the subject, never the requester."""

    employee_id: UUID | None = None
    state: CorrectionState | None = None
    limit: int = 50
    offset: int = 0


@dataclass(slots=True, frozen=True)
class CorrectionView:
    """A document with the two answers that are not on its own row.

    `state` is the one field a client reads, and `approval` is the engine's request
    with every round's decisions — which is what explains a document that was
    returned, corrected and filed again.
    """

    correction: Correction
    state: CorrectionState
    approval: ApprovalState | None = None


@dataclass(slots=True, frozen=True)
class CorrectionTarget:
    """The punch a document is about, as the stream currently has it.

    `punch` is the row the chain starts at, or `None` when the punch was never
    made; `tip` is the row a new correction points at — the newest row of the
    chain, so the chain reads in order rather than fanning out from the original.
    """

    punch: AttendanceEvent | None
    tip: AttendanceEvent | None

    @property
    def exists(self) -> bool:
        return self.punch is not None


@dataclass(slots=True, frozen=True)
class ApplyFailure:
    """One document that was approved and could not be appended, and why.

    Carried rather than raised by the sweep, for the reason the personnel applier
    gives: one document nobody can apply must not stop the ones behind it. The
    decision path raises it instead, because there the caller is waiting for an
    answer about this document.
    """

    correction_id: UUID
    code: str
    detail: str


@dataclass(slots=True, frozen=True)
class ApplyReport:
    """What one run of the applier did. Empty is the ordinary answer."""

    applied: tuple[Correction, ...] = ()
    failed: tuple[ApplyFailure, ...] = ()

    @property
    def applied_count(self) -> int:
        return len(self.applied)

    def failure_for(self, correction_id: UUID) -> ApplyFailure | None:
        return next(
            (failure for failure in self.failed if failure.correction_id == correction_id),
            None,
        )


__all__ = [
    "ENTITY_TYPE",
    "ApplyFailure",
    "ApplyReport",
    "Correction",
    "CorrectionInput",
    "CorrectionPatch",
    "CorrectionQuery",
    "CorrectionState",
    "CorrectionTarget",
    "CorrectionView",
    "state_of_correction",
]
