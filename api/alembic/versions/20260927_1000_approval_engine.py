"""The approval engine's three tables, and an append-only decision record.

Revision ID: 0009
Revises: 0008
Created: 2026-09-27

Three decisions are worth reading before the DDL:

* **One open request per entity is a partial unique index**, not a check in the
  service. `draft`, `pending_first` and `pending_second` are the open statuses, so
  the index covers exactly those; a closed request frees the entity again. What
  makes this the right layer is that the service's check is what a concurrent pair
  of submissions both passes.
* **`approval_decisions` is append-only.** The blanket grant migration 0007 wrote
  gave every table INSERT/UPDATE/DELETE; this revokes UPDATE and DELETE on this one
  and leaves SELECT and INSERT, exactly as `audit_log` is treated. The runtime role
  can add a decision; nothing it can call rewrites one.
* **`approval_decisions.request_id` is ON DELETE RESTRICT** rather than CASCADE.
  A cascade runs with the *referenced* table's owner privileges, so it would walk
  past the revoked DELETE and take the decision history with the request — the
  guarantee would hold against the runtime role and fail against anybody with the
  owner's credentials, which is precisely the case the guarantee exists for.
  Deleting a decided request is therefore refused by the database, and an erasure
  has to delete the decisions explicitly first.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "eam_app"


def upgrade() -> None:
    op.create_table(
        "approval_requests",
        sa.Column("id", sa.UUID(), nullable=False),
        # Free-form: the engine stores what it is told and never interprets it, so
        # a new kind of document needs no migration (DESIGN §3.4).
        sa.Column("entity_type", sa.String(length=60), nullable=False),
        sa.Column("entity_id", sa.UUID(), nullable=False),
        # Plain UUIDs, no foreign key to employees: a request keeps its history and
        # its route keeps resolving after somebody leaves. Same reasoning as
        # `employee_assignments.manager_employee_id`.
        sa.Column("requester_employee_id", sa.UUID(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("round", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "initiated_by", sa.String(length=16), server_default=sa.text("'user'"), nullable=False
        ),
        sa.Column("confirmed_by_user_id", sa.UUID(), nullable=True),
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
            "status IN ('draft', 'pending_first', 'pending_second', "
            "'approved', 'rejected', 'withdrawn')",
            name="ck_approval_requests_status",
        ),
        # A round is an attempt: the first, or the next one after a return.
        sa.CheckConstraint("round >= 1", name="ck_approval_requests_round"),
        sa.CheckConstraint(
            "initiated_by IN ('user', 'agent', 'system')",
            name="ck_approval_requests_initiated_by",
        ),
        sa.CheckConstraint(
            "length(btrim(entity_type)) > 0", name="ck_approval_requests_entity_type"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_approval_requests_open",
        "approval_requests",
        ["entity_type", "entity_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('draft', 'pending_first', 'pending_second')"),
    )
    # `state_of` reads the latest request for an entity, which may be closed; the
    # partial index above only answers that for open ones.
    op.create_index(
        "ix_approval_requests_entity", "approval_requests", ["entity_type", "entity_id"]
    )
    op.create_index(
        "ix_approval_requests_requester", "approval_requests", ["requester_employee_id"]
    )

    op.create_table(
        "approval_steps",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("request_id", sa.UUID(), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False),
        # Null at level 2: that level is "anyone holding hr", so nobody is named
        # until they decide, and the decision row names them.
        sa.Column("approver_employee_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("level IN (1, 2)", name="ck_approval_steps_level"),
        sa.CheckConstraint("round >= 1", name="ck_approval_steps_round"),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'returned', 'skipped')",
            name="ck_approval_steps_status",
        ),
        sa.ForeignKeyConstraint(["request_id"], ["approval_requests.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # One step per level per round: "which step is pending" is the request's
        # progress, so two pending level-1 steps would leave it unanswerable.
        sa.UniqueConstraint("request_id", "round", "level", name="uq_approval_steps_round_level"),
    )
    op.create_index("ix_approval_steps_request", "approval_steps", ["request_id"])

    op.create_table(
        "approval_decisions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("request_id", sa.UUID(), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False),
        sa.Column("approver_employee_id", sa.UUID(), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("level IN (1, 2)", name="ck_approval_decisions_level"),
        sa.CheckConstraint("round >= 1", name="ck_approval_decisions_round"),
        sa.CheckConstraint(
            "decision IN ('approved', 'rejected', 'returned', 'skipped')",
            name="ck_approval_decisions_decision",
        ),
        sa.ForeignKeyConstraint(["request_id"], ["approval_requests.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        # A level is decided once per round, so this is the shape of the history
        # rather than a duplicate guard.
        sa.UniqueConstraint(
            "request_id", "round", "level", name="uq_approval_decisions_round_level"
        ),
    )
    op.create_index("ix_approval_decisions_request", "approval_decisions", ["request_id"])

    op.execute(f"REVOKE UPDATE, DELETE ON approval_decisions FROM {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON approval_decisions TO {APP_ROLE}")


def downgrade() -> None:
    # Dropping the table removes its privileges, so there is nothing to revoke for
    # the role here; the decision rows have to go before the requests they point
    # at, which the RESTRICT above would otherwise refuse.
    op.execute("DELETE FROM approval_decisions")
    op.drop_index("ix_approval_decisions_request", table_name="approval_decisions")
    op.drop_table("approval_decisions")
    op.drop_index("ix_approval_steps_request", table_name="approval_steps")
    op.drop_table("approval_steps")
    op.drop_index("ix_approval_requests_requester", table_name="approval_requests")
    op.drop_index("ix_approval_requests_entity", table_name="approval_requests")
    op.drop_index("uq_approval_requests_open", table_name="approval_requests")
    op.drop_table("approval_requests")
