"""Personnel change documents: 入转调离 as one table.

One document type, five change types, and one payload shape — a list of
`(field, before, after)` items. The payload is structured rather than prose
because the applier reads field names: a paragraph cannot be applied, and a change
nobody can apply is a change somebody applies by hand.

**Approval and application are different facts, and this table records the
second.** `applied_at` is when the change took effect, which is never the moment
it was approved; `status` is what this module did (DESIGN §3.1's value domain,
with `approved` reserved for a writer that mirrors the engine — this module does
not, because the engine's answer is read from the engine). `approval_request_id`
is the request this change filed; it is a plain UUID rather than a foreign key,
for the reason `approval_requests.entity_id` is: the engine's identifiers are
free-form and its tables are its own.

`employee_id` is empty until a join is applied. Writing the employee when the
draft is created would put a hire into the directory weeks before the day it was
agreed for, which is the leak approval-ahead-of-time exists to prevent.

`applied_values` is what the change actually wrote — the new assignment, the
termination date, the agreed salary. Salary matters most: `salary_records`
arrives with ticket 43, so until then this column *is* the record of the agreed
figure, and ticket 43 will write its row from the same payload.
"""

from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db_metadata import Base

#: The payload shape, as the database sees it. Written out rather than built from
#: `FIELD_SPECS`, because a constraint that follows the code would not constrain
#: it: this is the floor under a payload written by anything.
#:
#: `payload ? 'changes'` is not redundant with the type test beside it. A CHECK
#: fails only when it evaluates to FALSE, and `jsonb_typeof(payload -> 'changes')`
#: is NULL when the key is absent — so a payload with no `changes` key at all would
#: pass a constraint written without the existence test. That is exactly the free
#: text this column exists to refuse.
PAYLOAD_SHAPE_SQL = (
    "jsonb_typeof(payload) = 'object'"
    " AND payload ? 'changes'"
    " AND jsonb_typeof(payload -> 'changes') = 'array'"
    " AND jsonb_array_length(payload -> 'changes') >= 1"
)


class PersonnelChange(Base):
    __tablename__ = "personnel_changes"
    __table_args__ = (
        CheckConstraint(
            "change_type IN ('join', 'transfer', 'promotion', 'salary', 'termination')",
            name="ck_personnel_changes_type",
        ),
        CheckConstraint(
            "status IN ('draft', 'pending', 'approved', 'applied', 'cancelled')",
            name="ck_personnel_changes_status",
        ),
        # Only a join may be written without an employee: the other four are about
        # somebody who is already here, and the module refuses them earlier.
        CheckConstraint(
            "employee_id IS NOT NULL OR change_type = 'join'",
            name="ck_personnel_changes_employee",
        ),
        CheckConstraint(PAYLOAD_SHAPE_SQL, name="ck_personnel_changes_payload"),
        # The two terminal states each have their timestamp, and no row can be in
        # one without it: a cancelled change with no `cancelled_at` would be
        # indistinguishable from a live one in any query that forgot the status.
        CheckConstraint(
            "(status = 'applied') = (applied_at IS NOT NULL)",
            name="ck_personnel_changes_applied_at",
        ),
        CheckConstraint(
            "(status = 'cancelled') = (cancelled_at IS NOT NULL)",
            name="ck_personnel_changes_cancelled_at",
        ),
        CheckConstraint(
            "cancelled_at IS NULL OR length(btrim(cancel_reason)) > 0",
            name="ck_personnel_changes_cancel_reason",
        ),
        # The applier's query: due, unapplied, uncancelled, oldest first.
        Index(
            "ix_personnel_changes_due",
            "effective_date",
            postgresql_where=text("applied_at IS NULL AND cancelled_at IS NULL"),
        ),
        Index("ix_personnel_changes_employee", "employee_id"),
        Index("ix_personnel_changes_status", "status"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    change_type: Mapped[str] = mapped_column(String(16), nullable=False)
    #: Empty until a join is applied: see the module docstring.
    employee_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("employees.id", ondelete="RESTRICT"), nullable=True
    )
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    #: What the change wrote. Empty until it is applied.
    applied_values: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    #: The request this change filed. No foreign key: the engine's identifiers are
    #: free-form by design, and its rows are the engine's to own.
    approval_request_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Who stopped it and why. Employee ids rather than users, matching the
    #: approval engine: the person is who the system reasons about, and the record
    #: has to keep reading after an account is gone.
    cancelled_by_employee_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    cancel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Who drafted it, which is also who the approval request is filed as.
    created_by_employee_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<PersonnelChange {self.change_type} {self.status} {self.id}>"


__all__ = ["PAYLOAD_SHAPE_SQL", "PersonnelChange"]
