"""Overtime: the request made in advance, the record, and its append-only ledger.

Revision ID: 0020
Revises: 0019
Created: 2026-10-03

**Chain position:** 0019 (`20261002_1000_leave.py`, ticket 25) → 0020 (this, ticket
26). The directory was read immediately before this file was written — `alembic
heads` reported `0019` and nothing else — because the ids have collided four times in
this project, and ticket 26 and ticket 25 were written in the same window.

Three tables (DESIGN §3.2, §7.3, Q12) and what is worth reading:

* **`month_bucket` is the monthly summary.** `YYYY-MM` of the Madrid business day, so
  the month-end export is a group-by rather than a date range re-derived from
  timestamps. The CHECK states the shape, which is what stops a hand-written row
  filing a day under a bucket no month matches.
* **`ck_overtime_records_smaller` is the ticket's settlement rule.** `computed_minutes
  = LEAST(approved_minutes, worked_minutes)`, asserted by the database rather than
  only by the service that computes it: "取较小值" is the rule, and a path that chose
  the approved figure while the worked one was smaller cannot store the result. The
  computed figure and its stamp are one fact with two witnesses, and neither is ever
  rewritten when HR confirms — the confirmed value sits in its own column, with the
  reason it changed in `confirmation_note` and in the ledger.
* **`overtime_entries` is append-only** (`REVOKE UPDATE, DELETE`), like the leave
  ledger and the expected-hours snapshot, and for the same reason: it is the history
  of how a figure was reached, each row carries the record's three figures after its
  movement, and evidence that can be edited is not evidence. "确认或调整的值必须留痕
  （保留原值与原因）" is this table plus the two columns beside it.
* **No rate, no multiplier, no amount.** Not one column of these three tables holds
  money: this system accumulates and exports *hours*, and what an hour costs is
  finance's calculation (Q12). That is a decision the schema records rather than a
  gap somebody fills in later — the test suite asserts no such column exists.
* **`uq_overtime_requests_open_day` is partial and that is the rule.** One live
  request per person per day (`settled_at IS NULL`), so a rejected or withdrawn
  request releases the day while two intentions can never be filed at once. Overtime
  is counted once per day per person, and a race cannot make it twice.

No `GRANT` statement: migration 0007 set default privileges, so tables added later are
reachable by the runtime role without one. No row-level security either: `employee_private`
carries policies because those columns are the ones an administrative query must not
read, while overtime is read through the kernel's actions — `overtime.read_own`,
`overtime.read_report`, `overtime.read_all` — exactly as attendance and leave are. The
export joins `employee_private` for the staff number, and the RLS policy there is what
keeps that column unreadable to a caller the export action does not admit.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "eam_app"

#: Matches `app/models/overtime.py`. Literals rather than imports: a migration
#: describes the schema at one moment, and an import would let a later edit to the
#: model rewrite what this revision did.
ENTRY_TYPES = "('approve', 'settle', 'confirm')"
MONTH_BUCKET = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
MAX_DAY_MINUTES = 1440


def upgrade() -> None:
    op.create_table(
        "overtime_requests",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("employee_id", sa.UUID(), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("expected_minutes", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("approval_request_id", sa.UUID(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
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
            f"expected_minutes BETWEEN 1 AND {MAX_DAY_MINUTES}",
            name="ck_overtime_requests_minutes",
        ),
        # The ticket's 事由: a request states why. Blank is not a reason.
        sa.CheckConstraint("length(btrim(reason)) > 0", name="ck_overtime_requests_reason"),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_overtime_requests_employee_date", "overtime_requests", ["employee_id", "business_date"]
    )
    # One live intention per person per day. Partial: a rejected or withdrawn request
    # is closed (`settled_at`), and closing it must give the day back.
    op.create_index(
        "uq_overtime_requests_open_day",
        "overtime_requests",
        ["employee_id", "business_date"],
        unique=True,
        postgresql_where=sa.text("settled_at IS NULL"),
    )
    op.create_index(
        "ix_overtime_requests_unsettled",
        "overtime_requests",
        ["submitted_at"],
        postgresql_where=sa.text("approval_request_id IS NOT NULL AND settled_at IS NULL"),
    )

    op.create_table(
        "overtime_records",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("request_id", sa.UUID(), nullable=False),
        sa.Column("employee_id", sa.UUID(), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("month_bucket", sa.String(length=7), nullable=False),
        sa.Column("approved_minutes", sa.Integer(), nullable=False),
        sa.Column("worked_minutes", sa.Integer(), nullable=True),
        sa.Column("computed_minutes", sa.Integer(), nullable=True),
        sa.Column("needs_confirmation", sa.Boolean(), nullable=False),
        sa.Column("confirmed_minutes", sa.Integer(), nullable=True),
        sa.Column("confirmed_by_employee_id", sa.UUID(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmation_note", sa.Text(), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(f"month_bucket ~ '{MONTH_BUCKET}'", name="ck_overtime_records_bucket"),
        sa.CheckConstraint(
            f"approved_minutes BETWEEN 1 AND {MAX_DAY_MINUTES}",
            name="ck_overtime_records_approved",
        ),
        sa.CheckConstraint(
            f"worked_minutes IS NULL OR worked_minutes BETWEEN 0 AND {MAX_DAY_MINUTES}",
            name="ck_overtime_records_worked",
        ),
        sa.CheckConstraint(
            f"computed_minutes IS NULL OR computed_minutes BETWEEN 0 AND {MAX_DAY_MINUTES}",
            name="ck_overtime_records_computed",
        ),
        sa.CheckConstraint(
            "(settled_at IS NULL) = (computed_minutes IS NULL)",
            name="ck_overtime_records_settled",
        ),
        # "取较小值", as an equation the database performs.
        sa.CheckConstraint(
            "computed_minutes IS NULL OR (worked_minutes IS NOT NULL "
            "AND computed_minutes = LEAST(approved_minutes, worked_minutes))",
            name="ck_overtime_records_smaller",
        ),
        sa.CheckConstraint(
            f"confirmed_minutes IS NULL OR confirmed_minutes BETWEEN 0 AND {MAX_DAY_MINUTES}",
            name="ck_overtime_records_confirmed_minutes",
        ),
        sa.CheckConstraint(
            "(confirmed_minutes IS NULL) = (confirmed_at IS NULL)",
            name="ck_overtime_records_confirmation",
        ),
        sa.CheckConstraint(
            "confirmed_at IS NULL OR length(btrim(confirmation_note)) > 0",
            name="ck_overtime_records_confirmation_note",
        ),
        sa.ForeignKeyConstraint(["request_id"], ["overtime_requests.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_id", name="uq_overtime_records_request"),
    )
    op.create_index(
        "ix_overtime_records_month",
        "overtime_records",
        ["month_bucket", "employee_id", "business_date"],
    )
    op.create_index(
        "ix_overtime_records_employee_date", "overtime_records", ["employee_id", "business_date"]
    )
    op.create_index(
        "ix_overtime_records_unsettled",
        "overtime_records",
        ["business_date"],
        postgresql_where=sa.text("settled_at IS NULL"),
    )
    op.create_index(
        "ix_overtime_records_needs_confirmation",
        "overtime_records",
        ["month_bucket"],
        postgresql_where=sa.text("needs_confirmation"),
    )

    op.create_table(
        "overtime_entries",
        sa.Column("id", sa.UUID(), nullable=False),
        # The reading order. `created_at` is the transaction's start time, so every
        # entry one settlement writes shares it and ordering by it would shuffle the
        # ledger — the same reason the audit trail and the leave ledger number theirs.
        sa.Column("seq", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("record_id", sa.UUID(), nullable=False),
        sa.Column("entry_type", sa.String(length=16), nullable=False),
        sa.Column("approved_minutes", sa.Integer(), nullable=False),
        sa.Column("computed_minutes", sa.Integer(), nullable=True),
        sa.Column("confirmed_minutes", sa.Integer(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by_employee_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(f"entry_type IN {ENTRY_TYPES}", name="ck_overtime_entries_type"),
        sa.CheckConstraint(
            f"approved_minutes BETWEEN 1 AND {MAX_DAY_MINUTES}",
            name="ck_overtime_entries_approved",
        ),
        sa.CheckConstraint(
            f"computed_minutes IS NULL OR computed_minutes BETWEEN 0 AND {MAX_DAY_MINUTES}",
            name="ck_overtime_entries_computed",
        ),
        sa.CheckConstraint(
            f"confirmed_minutes IS NULL OR confirmed_minutes BETWEEN 0 AND {MAX_DAY_MINUTES}",
            name="ck_overtime_entries_confirmed",
        ),
        sa.ForeignKeyConstraint(["record_id"], ["overtime_records.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("seq", name="uq_overtime_entries_seq"),
    )
    op.create_index("ix_overtime_entries_record", "overtime_entries", ["record_id", "seq"])
    # The history is evidence: it cannot be rewritten or removed by the role that
    # serves requests, which is what makes "what did this figure read before HR
    # confirmed it" answerable.
    op.execute(f"REVOKE UPDATE, DELETE ON overtime_entries FROM {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON overtime_entries TO {APP_ROLE}")
    # A request two people decided is the record of that decision, and the record is
    # what a month was paid from; the same reasoning as `leave_requests`. The UPDATE is
    # deliberately kept: settlement and confirmation write the record's own columns,
    # and the ledger above is what keeps the before/after pair.
    op.execute(f"REVOKE DELETE ON overtime_requests FROM {APP_ROLE}")
    op.execute(f"REVOKE DELETE ON overtime_records FROM {APP_ROLE}")


def downgrade() -> None:
    op.drop_index("ix_overtime_entries_record", table_name="overtime_entries")
    op.drop_table("overtime_entries")
    op.drop_index("ix_overtime_records_needs_confirmation", table_name="overtime_records")
    op.drop_index("ix_overtime_records_unsettled", table_name="overtime_records")
    op.drop_index("ix_overtime_records_employee_date", table_name="overtime_records")
    op.drop_index("ix_overtime_records_month", table_name="overtime_records")
    op.drop_table("overtime_records")
    op.drop_index("ix_overtime_requests_unsettled", table_name="overtime_requests")
    op.drop_index("uq_overtime_requests_open_day", table_name="overtime_requests")
    op.drop_index("ix_overtime_requests_employee_date", table_name="overtime_requests")
    op.drop_table("overtime_requests")
