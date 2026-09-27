"""The attendance event stream, and the day derived from it.

Revision ID: 0012
Revises: 0011
Created: 2026-09-29

Four decisions are worth reading before the DDL:

* **`attendance_events` is append-only in the database.** The blanket grant
  migration 0007 wrote gave every table INSERT/UPDATE/DELETE; this revokes UPDATE
  and DELETE on this one and leaves SELECT and INSERT, exactly as `audit_log` is
  treated and for the same reason. A punch is the evidence a working-time record
  is made of, and "nobody may rewrite it, HR included" is a claim about
  PostgreSQL rather than about the code paths that exist today.
* **`(employee_id, event_type, occurred_at)` is unique, for punches only.** The
  partial predicate excludes corrections, because a correction's identity is the
  chain it belongs to and two corrections of two different punches may legitimately
  share an instant. That index *is* the replay guard: a retried request collides on
  it and is read back rather than appended twice. Checking in the service first
  would be the race the index exists to close.
* **`business_date` is stored on every row.** It is the Madrid calendar day,
  computed on the way in, and it is the only column any aggregation reads. A query
  that groups by `occurred_at::date` is the risk the register names
  ("考勤业务日与 UTC 混淆"), and it is not possible to write one by accident here.
* **No row-level policy, deliberately, for now.** These rows belong to one person
  and every read in ticket 21 is the person's own, but the *enable* decision would
  also apply to the derivation: a recompute is a write made on somebody's behalf
  and, later, a worker's nightly pass has no request context to publish at all.
  The 403 is decided in the kernel, from `Resource.owner_employee_id`, and ticket
  24 — which is the first ticket to read somebody else's attendance — decides what
  the database rule should be, as ticket 31 does for documents.

The foreign keys are RESTRICT. The four-year record outlives the person it names
and must not be deleted along with them, and `correction_of_event_id` points at a
row of this same table, which no cascade may remove.

No `GRANT` statement: migration 0007 set default privileges so tables added later
are reachable by the runtime role without one. `UPDATE` on `attendance_daily` is
kept — the rebuild writes it with `ON CONFLICT DO UPDATE` — and `DELETE` on it is
revoked with the same reasoning as the stream: a day is recomputed, never removed.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "eam_app"

#: The closed set this ticket can decide from the events alone. Ticket 22 adds the
#: schedule-derived ones (`late`, `holiday`, `leave`, DESIGN §3.2) by extending
#: this constraint, which is exactly the moment they become decidable.
STATUSES = "('working', 'ok', 'missing_out', 'incomplete', 'absent')"


def upgrade() -> None:
    op.create_table(
        "attendance_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("employee_id", sa.UUID(), nullable=False),
        sa.Column("event_type", sa.String(length=16), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("ip_address", sa.String(length=45), nullable=True),
        sa.Column("created_by_employee_id", sa.UUID(), nullable=True),
        sa.Column("correction_of_event_id", sa.UUID(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "event_type IN ('clock_in', 'clock_out', 'correction')",
            name="ck_attendance_events_type",
        ),
        sa.CheckConstraint("source IN ('web', 'correction')", name="ck_attendance_events_source"),
        sa.CheckConstraint(
            "(event_type = 'correction') = (correction_of_event_id IS NOT NULL)",
            name="ck_attendance_events_correction_target",
        ),
        sa.CheckConstraint(
            "event_type <> 'correction' OR length(btrim(reason)) > 0",
            name="ck_attendance_events_correction_reason",
        ),
        sa.CheckConstraint(
            "correction_of_event_id IS NULL OR correction_of_event_id <> id",
            name="ck_attendance_events_self_correction",
        ),
        # RESTRICT, like `personnel_changes.employee_id`: the record of somebody's
        # working time outlives their employee row.
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="RESTRICT"),
        # RESTRICT rather than CASCADE: a cascade runs with the *referenced*
        # table's owner privileges, so it would walk past the revoked DELETE and
        # take a corrected punch with the correction.
        sa.ForeignKeyConstraint(
            ["correction_of_event_id"], ["attendance_events.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_attendance_events_employee_date",
        "attendance_events",
        ["employee_id", "business_date", "occurred_at"],
    )
    op.create_index(
        "uq_attendance_events_punch",
        "attendance_events",
        ["employee_id", "event_type", "occurred_at"],
        unique=True,
        postgresql_where=sa.text("event_type <> 'correction'"),
    )
    op.create_index(
        "ix_attendance_events_correction_of",
        "attendance_events",
        ["correction_of_event_id"],
        postgresql_where=sa.text("correction_of_event_id IS NOT NULL"),
    )

    op.create_table(
        "attendance_daily",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("employee_id", sa.UUID(), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("first_in", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_out", sa.DateTime(timezone=True), nullable=True),
        sa.Column("worked_minutes", sa.Integer(), nullable=True),
        # Null until ticket 22 puts a schedule behind the department. The columns
        # exist now because the snapshot is where an expectation is *frozen*: a
        # reader four years from now must not have to re-derive it from a schedule
        # that has since been edited.
        sa.Column("expected_minutes", sa.Integer(), nullable=True),
        sa.Column("overtime_minutes", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("snapshot_schedule_id", sa.UUID(), nullable=True),
        sa.Column(
            "recomputed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(f"status IN {STATUSES}", name="ck_attendance_daily_status"),
        sa.CheckConstraint(
            "worked_minutes IS NULL OR worked_minutes >= 0",
            name="ck_attendance_daily_worked_minutes",
        ),
        # One row per person per day: this constraint is the conflict target of the
        # rebuild, so a recompute overwrites rather than appends.
        sa.UniqueConstraint(
            "employee_id", "business_date", name="uq_attendance_daily_employee_date"
        ),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_attendance_daily_employee_date", "attendance_daily", ["employee_id", "business_date"]
    )

    op.execute(f"REVOKE UPDATE, DELETE ON attendance_events FROM {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON attendance_events TO {APP_ROLE}")
    op.execute(f"REVOKE DELETE ON attendance_daily FROM {APP_ROLE}")


def downgrade() -> None:
    # The stream first: `correction_of_event_id` references it, and the corrections
    # would otherwise have to be deleted one chain at a time.
    op.drop_index("ix_attendance_daily_employee_date", table_name="attendance_daily")
    op.drop_table("attendance_daily")
    op.drop_index("ix_attendance_events_correction_of", table_name="attendance_events")
    op.drop_index("uq_attendance_events_punch", table_name="attendance_events")
    op.drop_index("ix_attendance_events_employee_date", table_name="attendance_events")
    op.drop_table("attendance_events")
