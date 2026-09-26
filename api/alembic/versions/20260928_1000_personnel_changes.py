"""Personnel change documents.

Revision ID: 0010
Revises: 0009
Created: 2026-09-28

One table for 入转调离, and four decisions worth reading before the DDL:

* **`employee_id` is nullable, and a check constraint says which type may leave it
  empty.** Only a join creates the person it is about; writing the employee row
  when the draft was filed would put a hire into the directory weeks before the
  day it was agreed for, which is the leak the ticket exists to prevent. The
  column is filled in when the join is applied.
* **The payload shape is a constraint, not a convention.** `payload` is JSONB of
  `{"changes": [{"field", "before", "after"}, ...]}`, and the database refuses an
  object without a non-empty `changes` array — including one with no `changes` key
  at all, which needs its own existence test because a CHECK only fails when it is
  FALSE and a missing key makes the type test NULL. This is the floor under
  "structured, never free text" for anything that writes the table, a script
  included.
* **`employee_id` is ON DELETE RESTRICT**, unlike the assignments table's cascade.
  A personnel change is the record of a personnel action and outlives the row it
  acted on; a cascade would let deleting an employee take the history of why they
  left with it.
* **`approval_request_id` carries no foreign key.** The engine treats the pair
  `(entity_type, entity_id)` as free-form and never reads it (DESIGN §3.4); the
  request it filed is recorded here so the change can name it, and the engine's
  tables stay the engine's.

The two terminal states carry their own timestamps, and the constraints tie them
to the status: a row cannot claim to be applied without an `applied_at`, or
cancelled without a `cancelled_at` and a reason.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PAYLOAD_SHAPE_SQL = (
    "jsonb_typeof(payload) = 'object'"
    # Not redundant with the type test: a CHECK fails only when it is FALSE, and
    # `jsonb_typeof(payload -> 'changes')` is NULL when the key is missing — so
    # without this, a payload carrying no `changes` at all would pass.
    " AND payload ? 'changes'"
    " AND jsonb_typeof(payload -> 'changes') = 'array'"
    " AND jsonb_array_length(payload -> 'changes') >= 1"
)


def upgrade() -> None:
    op.create_table(
        "personnel_changes",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("change_type", sa.String(length=16), nullable=False),
        sa.Column("employee_id", sa.UUID(), nullable=True),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("applied_values", postgresql.JSONB(), nullable=True),
        sa.Column("approval_request_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_by_employee_id", sa.UUID(), nullable=True),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        sa.Column("created_by_employee_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "change_type IN ('join', 'transfer', 'promotion', 'salary', 'termination')",
            name="ck_personnel_changes_type",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'pending', 'approved', 'applied', 'cancelled')",
            name="ck_personnel_changes_status",
        ),
        sa.CheckConstraint(
            "employee_id IS NOT NULL OR change_type = 'join'",
            name="ck_personnel_changes_employee",
        ),
        sa.CheckConstraint(PAYLOAD_SHAPE_SQL, name="ck_personnel_changes_payload"),
        sa.CheckConstraint(
            "(status = 'applied') = (applied_at IS NOT NULL)",
            name="ck_personnel_changes_applied_at",
        ),
        sa.CheckConstraint(
            "(status = 'cancelled') = (cancelled_at IS NOT NULL)",
            name="ck_personnel_changes_cancelled_at",
        ),
        sa.CheckConstraint(
            "cancelled_at IS NULL OR length(btrim(cancel_reason)) > 0",
            name="ck_personnel_changes_cancel_reason",
        ),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    # Partial: the applier only ever looks for rows that are still to take effect.
    op.create_index(
        "ix_personnel_changes_due",
        "personnel_changes",
        ["effective_date"],
        postgresql_where=sa.text("applied_at IS NULL AND cancelled_at IS NULL"),
    )
    op.create_index("ix_personnel_changes_employee", "personnel_changes", ["employee_id"])
    op.create_index("ix_personnel_changes_status", "personnel_changes", ["status"])


def downgrade() -> None:
    op.drop_index("ix_personnel_changes_status", table_name="personnel_changes")
    op.drop_index("ix_personnel_changes_employee", table_name="personnel_changes")
    op.drop_index("ix_personnel_changes_due", table_name="personnel_changes")
    op.drop_table("personnel_changes")
