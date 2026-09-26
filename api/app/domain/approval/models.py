"""Approval value objects.

The vocabulary is deliberately small: a request, its steps, the decisions taken on
it, and the state assembled from all three. Nothing here knows what `entity_id`
points at, and nothing here could — the engine's whole job is to move a pair of
identifiers through a state machine.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class ApprovalStatus(StrEnum):
    """Where a request stands.

    `DRAFT` is only ever reached by being returned for correction; a request is
    never created in it. The first three are the open statuses — the ones the
    database's partial unique index covers, so an entity can have at most one
    request in them at a time.
    """

    DRAFT = "draft"
    PENDING_FIRST = "pending_first"
    PENDING_SECOND = "pending_second"
    APPROVED = "approved"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


#: Statuses in which a request is still in flight.
OPEN_STATUSES = frozenset(
    {ApprovalStatus.DRAFT, ApprovalStatus.PENDING_FIRST, ApprovalStatus.PENDING_SECOND}
)

#: The level each open status is waiting on, so "which step is next" is one table
#: rather than a condition spelled out at every call site.
_LEVEL_OF_STATUS: dict[ApprovalStatus, int] = {
    ApprovalStatus.PENDING_FIRST: 1,
    ApprovalStatus.PENDING_SECOND: 2,
}


class StepStatus(StrEnum):
    """The state of one level of one round.

    `approved`, `rejected`, `returned` and `skipped` are also what
    `approval_decisions` stores: a step's state and the decision that produced it
    are the same word in the schema, so they are the same enum here.
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    RETURNED = "returned"
    #: Level 1 when the resolved approver is the requester. Nobody approves their
    #: own request, and the level is passed over rather than handed to somebody
    #: else: the route names the requester's manager, and inventing a substitute
    #: would answer a question the route did not ask.
    SKIPPED = "skipped"


class DecisionKind(StrEnum):
    """What an approver may do with the level in front of them.

    The imperative form: this is what a caller asks for. What gets *recorded* is
    a `StepStatus`, which is the outcome.
    """

    APPROVE = "approve"
    REJECT = "reject"
    RETURN = "return"


#: What each decision records, so the service and its readers cannot disagree
#: about what "approve" meant.
OUTCOME_OF: dict[DecisionKind, StepStatus] = {
    DecisionKind.APPROVE: StepStatus.APPROVED,
    DecisionKind.REJECT: StepStatus.REJECTED,
    DecisionKind.RETURN: StepStatus.RETURNED,
}

#: Why a level-1 step is recorded as skipped. A constant so the reason a reader
#: finds in a decision row is the same sentence wherever it was written.
SELF_APPROVAL_REASON = (
    "self-approval is not allowed: the resolved approver is the requester, "
    "so level 1 was passed over"
)


@dataclass(slots=True, frozen=True)
class ApprovalRequest:
    """One request, as stored. Its steps and decisions are separate rows."""

    id: UUID
    entity_type: str
    entity_id: UUID
    requester_employee_id: UUID
    status: ApprovalStatus
    #: The attempt number. A return-for-correction ends one round; resubmitting
    #: opens the next, and the earlier round's decisions stay readable.
    round: int
    submitted_at: datetime | None
    #: Set only in a terminal state, so `decided_at is not None` says "this
    #: request is closed" without reading `status`.
    decided_at: datetime | None
    created_at: datetime
    updated_at: datetime
    #: user | agent | system. DESIGN §3.4 keeps this on the request because that
    #: is where an agent-proposed document is told apart from one a person filed.
    initiated_by: str = "user"
    confirmed_by_user_id: UUID | None = None


@dataclass(slots=True, frozen=True)
class ApprovalStep:
    """One level of one round."""

    id: UUID
    request_id: UUID
    level: int
    round: int
    #: Null at level 2 by construction: that level is "anyone holding hr", so no
    #: individual is named in advance and the decision row names whoever decided.
    approver_employee_id: UUID | None
    status: StepStatus
    decided_at: datetime | None
    created_at: datetime


@dataclass(slots=True, frozen=True)
class ApprovalDecision:
    """One append-only record of what a level decided.

    The table behind this takes INSERT and SELECT only from the runtime role, so
    a decision cannot be edited or removed once written — the same treatment the
    audit log gets, and what lets the row be read as evidence.
    """

    id: UUID
    request_id: UUID
    level: int
    round: int
    approver_employee_id: UUID
    #: Never `pending`: a decision is the outcome of a level. `skipped` is
    #: written by the engine, not chosen by an approver.
    decision: StepStatus
    comment: str | None
    decided_at: datetime


@dataclass(slots=True, frozen=True)
class PrimaryPosition:
    """The assignment an approval route is resolved from.

    Only the two fields the route needs. The engine never reads the position, the
    department or the employee behind them.
    """

    department_id: UUID
    manager_employee_id: UUID | None


@dataclass(slots=True, frozen=True)
class SubmitContext:
    """How a submission arrived.

    `initiated_by` separates a request a person filed from one the assistant
    proposed, and `confirmed_by_user_id` names the human who confirmed an agent's
    proposal (DESIGN §6.3). The engine stores both and reads neither: they are the
    audit anchor the agent path needs, and adding them later would mean migrating
    rows that already exist.
    """

    initiated_by: str = "user"
    confirmed_by_user_id: UUID | None = None


@dataclass(slots=True, frozen=True)
class ApprovalState:
    """A request with everything decided about it so far.

    `steps` and `decisions` span **every** round, not only the current one. A
    returned request keeps its earlier decisions readable — that is what the
    `round` column exists for, and a caller shown only the latest round could not
    answer "who asked for a correction, and what did they say".
    """

    request: ApprovalRequest
    steps: tuple[ApprovalStep, ...] = ()
    decisions: tuple[ApprovalDecision, ...] = ()

    @property
    def id(self) -> UUID:
        return self.request.id

    @property
    def entity_type(self) -> str:
        return self.request.entity_type

    @property
    def entity_id(self) -> UUID:
        return self.request.entity_id

    @property
    def requester_employee_id(self) -> UUID:
        return self.request.requester_employee_id

    @property
    def status(self) -> ApprovalStatus:
        return self.request.status

    @property
    def round(self) -> int:
        return self.request.round

    @property
    def decided_at(self) -> datetime | None:
        return self.request.decided_at

    @property
    def is_open(self) -> bool:
        return self.request.status in OPEN_STATUSES

    @property
    def pending_step(self) -> ApprovalStep | None:
        """The step awaiting a decision, if the request is awaiting one."""
        level = _LEVEL_OF_STATUS.get(self.request.status)
        if level is None:
            return None
        return next(
            (
                step
                for step in self.steps
                if step.round == self.request.round
                and step.level == level
                and step.status is StepStatus.PENDING
            ),
            None,
        )

    def decisions_of(self, round_number: int) -> tuple[ApprovalDecision, ...]:
        """One round's decisions, oldest first — the history of one attempt."""
        return tuple(
            decision for decision in self.decisions if decision.round == round_number
        )


__all__ = [
    "OPEN_STATUSES",
    "OUTCOME_OF",
    "SELF_APPROVAL_REASON",
    "ApprovalDecision",
    "ApprovalRequest",
    "ApprovalState",
    "ApprovalStatus",
    "DecisionKind",
    "PrimaryPosition",
    "StepStatus",
    "SubmitContext",
]
