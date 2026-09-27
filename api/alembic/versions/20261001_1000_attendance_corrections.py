"""Attendance corrections: the document that changes a punch, and never the punch.

Revision ID: 0017
Revises: 0016
Created: 2026-10-01

**Chain position:** 0016 (`20260930_1600_attendance_anomalies.py`, ticket 23) →
0017 (this, ticket 24) → 0018 (the daily digest, ticket 20, which chained onto this
revision). Read the directory immediately before writing a migration: two ids
collided in one afternoon, and this one claimed 0017 while the head was 0016.

One table, and five decisions worth reading before the DDL:

* **A correction is a document, not an event.** `attendance_events` keeps only what
  happened; this table is the *request* to restate a punch — which day, which kind,
  what instant, why, who asked, and where the engine got to with it. Approval is
  what appends the event, so the request has to survive the wait between the two,
  and `applied_event_id` is the row it eventually wrote.
* **`applied_event_id` is the whole of "it took effect".** Non-null means the
  append happened and names the row that did it; there is no second boolean to
  disagree with it, and the append is a correction event (or, when the punch never
  happened at all, a punch carrying `source='correction'`). RESTRICT, like every
  other reference into the stream: a cascade runs with the *referenced* table's
  owner privileges and would walk past the revoked DELETE on `attendance_events`.
* **There is no `status` column, deliberately.** The state a reader sees is derived
  from three facts that are already here — whether the document was filed, what the
  engine's request says, and whether the append happened — so a status copied onto
  this row would be a second copy of "was this approved", and the one that goes
  stale is always the copy. The list query computes the same state in SQL (a `CASE`
  over this table and the engine's), which is the shape `personnel_changes` uses,
  and the tests run both expressions over one corpus of rows.
* **`(kind, business_date, employee_id)` is not unique, deliberately.** A first
  correction can be rejected and a second filed for the same punch, and both
  documents are part of the record of how that punch was argued about. What may not
  happen twice is an *open* request for the same punch, which is the flow's own
  check (one document, one entity, and the engine refuses a second open request for
  an entity) rather than a constraint here.
* **No row-level policy on this table or on `attendance_events`.** Migration 0012
  asked ticket 24 — the first ticket to read somebody else's attendance — to decide.
  The decision is none, and the reason is what a predicate would have to express:
  "this is the employee's own record, or their manager's, or HR's". The first half
  is `app.current_employee_id`; the second is a reporting relationship that lives in
  `employee_assignments`, and a policy here would have to read it under the
  *caller's* context. So the 403 is the kernel's, from the principal's snapshot
  (`reports_employee_ids`), decided before any query runs; the database's own
  contribution stays what it already is on the stream: nobody, including this flow,
  may UPDATE or DELETE a punch.

`DELETE` is revoked from the runtime role for the reason the two tables beside it
give: a correction is the reason a working-time figure changed, and the way to stop
it being true is another document, not the absence of this one. `UPDATE` is kept —
the engine's request id and the applied event are written in place.

No `GRANT` statement: migration 0007 set default privileges so tables added later
are reachable by the runtime role without one.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "eam_app"

#: The two punches a correction may restate, as a literal: a migration describes
#: the schema it applied, and a constraint that read a Python constant would follow
#: a later edit of it. `correction` is deliberately absent — this table records the
#: request, and a request to correct a correction is a request about the punch.
KINDS = "('clock_in', 'clock_out')"


def upgrade() -> None:
    op.create_table(
        "attendance_corrections",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("employee_id", sa.UUID(), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("corrected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("approval_request_id", sa.UUID(), nullable=True),
        sa.Column("applied_event_id", sa.UUID(), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requested_by_employee_id", sa.UUID(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(f"kind IN {KINDS}", name="ck_attendance_corrections_kind"),
        # A correction with no reason is a row nobody can read, which is the same
        # rule the stream states for its own correction events.
        sa.CheckConstraint(
            "length(btrim(reason)) > 0", name="ck_attendance_corrections_reason"
        ),
        # Effect is one fact with two witnesses, and they are written together or
        # not at all: a row claiming to have changed a punch without naming the row
        # it appended (or the reverse) is a row nobody can audit.
        sa.CheckConstraint(
            "(applied_at IS NULL) = (applied_event_id IS NULL)",
            name="ck_attendance_corrections_applied",
        ),
        # RESTRICT, like every other reference to the stream: the working-time
        # record outlives the person, and the row that changed it may not be
        # removed by a cascade.
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["requested_by_employee_id"], ["employees.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["applied_event_id"], ["attendance_events.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # The two reads the flow makes: "this person's corrections, newest first" and
    # "what is filed and not yet appended", the applier's queue.
    op.create_index(
        "ix_attendance_corrections_employee_date",
        "attendance_corrections",
        ["employee_id", "business_date"],
    )
    op.create_index(
        "ix_attendance_corrections_unapplied",
        "attendance_corrections",
        ["approval_request_id"],
        postgresql_where=sa.text("applied_at IS NULL AND approval_request_id IS NOT NULL"),
    )

    op.execute(f"REVOKE DELETE ON attendance_corrections FROM {APP_ROLE}")


def downgrade() -> None:
    op.drop_index(
        "ix_attendance_corrections_unapplied", table_name="attendance_corrections"
    )
    op.drop_index(
        "ix_attendance_corrections_employee_date", table_name="attendance_corrections"
    )
    op.drop_table("attendance_corrections")
