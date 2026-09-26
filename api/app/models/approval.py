"""Approval requests, their steps, and the decisions taken on them.

Three tables, one state machine (DESIGN §3.4):

* `approval_requests` is the request: an entity type, an entity id, who filed it,
  where it stands, and which round it is on. **A partial unique index** allows one
  open request per entity, which is what makes "the second one" impossible rather
  than merely refused by application code.
* `approval_steps` is one level of one round. Its status *is* the request's
  progress: a request is at level 1 or 2 because that step is the pending one, so
  there is no `current_step` column to fall out of step with it.
* `approval_decisions` is the append-only record. The runtime role holds INSERT
  and SELECT on it and nothing else (migration 0009), so a decision cannot be
  rewritten by the application — the same treatment `audit_log` gets.

`requester_employee_id` and the approver columns are plain UUIDs rather than
foreign keys to `employees`, for the reason `employee_assignments.manager_employee_id`
is: an approval route has to keep resolving, and a request has to keep its
history, after somebody leaves the company and their row is archived.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db_metadata import Base

#: Kept as a literal in the index predicate rather than built from the enum: the
#: database compares against what the migration wrote, and a Python-side change
#: must fail a test rather than silently widen the index.
OPEN_STATUSES_SQL = "status IN ('draft', 'pending_first', 'pending_second')"


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'pending_first', 'pending_second', "
            "'approved', 'rejected', 'withdrawn')",
            name="ck_approval_requests_status",
        ),
        CheckConstraint("round >= 1", name="ck_approval_requests_round"),
        CheckConstraint(
            "initiated_by IN ('user', 'agent', 'system')",
            name="ck_approval_requests_initiated_by",
        ),
        CheckConstraint(
            "length(btrim(entity_type)) > 0", name="ck_approval_requests_entity_type"
        ),
        # One open request per entity. A partial index rather than a check in the
        # service, because the service is what a race runs around.
        Index(
            "uq_approval_requests_open",
            "entity_type",
            "entity_id",
            unique=True,
            postgresql_where=text(OPEN_STATUSES_SQL),
        ),
        # The lookup `state_of` makes: the latest request for an entity, open or
        # closed. The partial index above does not answer it for closed rows.
        Index("ix_approval_requests_entity", "entity_type", "entity_id"),
        Index("ix_approval_requests_requester", "requester_employee_id"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    #: Free-form on purpose: the engine never interprets it, so a new kind of
    #: document needs no change here (DESIGN §3.4).
    entity_type: Mapped[str] = mapped_column(String(60), nullable=False)
    entity_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    requester_employee_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    #: The attempt. A return-for-correction closes one round; the next submission
    #: opens the next, which is what keeps earlier decisions readable.
    round: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Only set in a terminal state, so it answers "is this closed".
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: user | agent | system: how the request was filed.
    initiated_by: Mapped[str] = mapped_column(String(16), nullable=False, server_default="user")
    confirmed_by_user_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ApprovalRequest {self.entity_type}:{self.entity_id} {self.status}>"


class ApprovalStep(Base):
    __tablename__ = "approval_steps"
    __table_args__ = (
        CheckConstraint("level IN (1, 2)", name="ck_approval_steps_level"),
        CheckConstraint("round >= 1", name="ck_approval_steps_round"),
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'returned', 'skipped')",
            name="ck_approval_steps_status",
        ),
        # One step per level per round. Without it a retry could leave two
        # pending level-1 steps and "which one is current" would have no answer.
        UniqueConstraint("request_id", "round", "level", name="uq_approval_steps_round_level"),
        Index("ix_approval_steps_request", "request_id"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    request_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("approval_requests.id", ondelete="CASCADE"),
        nullable=False,
    )
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    round: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Null at level 2: that level is "anyone holding hr", so it names nobody
    #: until somebody decides, and the decision row names whoever did.
    approver_employee_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ApprovalStep {self.request_id} r{self.round} l{self.level} {self.status}>"


class ApprovalDecision(Base):
    """Append-only. `eam_app` may insert and read, never update or delete."""

    __tablename__ = "approval_decisions"
    __table_args__ = (
        CheckConstraint("level IN (1, 2)", name="ck_approval_decisions_level"),
        CheckConstraint("round >= 1", name="ck_approval_decisions_round"),
        CheckConstraint(
            "decision IN ('approved', 'rejected', 'returned', 'skipped')",
            name="ck_approval_decisions_decision",
        ),
        # One decision per level per round: a level is decided once, and the
        # request can only be at that level once in that round.
        UniqueConstraint(
            "request_id", "round", "level", name="uq_approval_decisions_round_level"
        ),
        Index("ix_approval_decisions_request", "request_id"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    #: RESTRICT, not CASCADE: a delete of the request must not be able to take the
    #: decision history with it. The cascade path runs with the table owner's
    #: privileges, so it would otherwise walk straight past the revoked DELETE and
    #: leave the append-only guarantee with a hole in it.
    request_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("approval_requests.id", ondelete="RESTRICT"),
        nullable=False,
    )
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    round: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Who decided. Never null, and never the requester (the engine refuses
    #: self-approval; a skipped level records the requester as the reason).
    approver_employee_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ApprovalDecision {self.request_id} r{self.round} l{self.level} {self.decision}>"


__all__ = [
    "OPEN_STATUSES_SQL",
    "ApprovalDecision",
    "ApprovalRequest",
    "ApprovalStep",
]
