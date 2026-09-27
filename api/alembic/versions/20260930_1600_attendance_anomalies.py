"""Attendance anomalies: the five ways a day can be wrong, one row per day.

Revision ID: 0016
Revises: 0015
Created: 2026-09-30

**Chain position:** 0014 (projects and tasks) → 0015
(`20260930_1500_timesheets.py`, ticket 28) → 0016 (this). Both tickets were written
in the same window and both first claimed 0015; this one yielded and was renumbered
onto the timesheets revision, so a reader of the directory sees the order without
opening another file and there is exactly one head.

One table (DESIGN §3.2), and four decisions worth reading before the DDL:

* **`(employee_id, business_date, type)` is unique, and that index is the scan's
  idempotency.** The nightly pass derives the anomalies of a day from the event
  stream and the schedule; running it twice must not produce a second row for the
  same fact, and a check in the service is what a second worker running at the same
  moment races around. `ON CONFLICT DO NOTHING` against this constraint is the
  half that holds. It is stated as an anomaly being a property of a day ("this day
  was late") rather than as an event ("somebody noticed lateness"), which is what
  makes one row the whole answer.
* **`notified_at` is the reminder's record, and it is on the anomaly.** The morning
  pass stamps it when it has told the employee, so a second run — or a restarted
  container — reminds nobody twice. It is a timestamp rather than a boolean for the
  same reason `notifications.read_at` is: "when" is the question asked afterwards,
  and it is the same column.
* **`resolved_by_event_id` points at the event that cleared it.** A correction
  (ticket 24) appends an event and the day is re-derived; the anomaly that the day
  no longer shows is *resolved*, and it is resolved by that row rather than by
  somebody closing it. The column is the whole rule: non-null means resolved, so
  there is no second `is_resolved` flag to disagree with it.
* **No `ON DELETE CASCADE`, and nothing here is deletable.** The four-year record
  outlives both the person and the punch it names: `employee_id` and
  `resolved_by_event_id` are RESTRICT because a cascade runs with the *referenced*
  table's owner privileges and would walk past the revoked DELETE on
  `attendance_events` (the argument migration 0012 makes).

`DELETE` is revoked from the runtime role for the same reason it is on
`attendance_daily`: an anomaly is a record of what the night's pass found, and the
way to stop it being true is to correct the day, not to remove the row. `UPDATE` is
kept — both the reminder and the correction resolve a row in place.

No `GRANT` statement: migration 0007 set default privileges so tables added later
are reachable by the runtime role without one.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "eam_app"

#: The closed set, as a literal: a migration describes the schema it applied, and a
#: constraint that read a Python constant would follow a later edit of it.
TYPES = "('missing_clock_out', 'missing_clock_in', 'late', 'early_leave', 'no_punches')"


def upgrade() -> None:
    op.create_table(
        "attendance_anomalies",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("employee_id", sa.UUID(), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("type", sa.String(length=24), nullable=False),
        sa.Column(
            "detected_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Null until the morning pass has told the employee about this one.
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        # Null while the anomaly stands. Non-null names the correction that cleared
        # it, which is the rule rather than a second flag beside it.
        sa.Column("resolved_by_event_id", sa.UUID(), nullable=True),
        sa.CheckConstraint(f"type IN {TYPES}", name="ck_attendance_anomalies_type"),
        # The scan's idempotency, and the conflict target of its insert.
        sa.UniqueConstraint(
            "employee_id", "business_date", "type", name="uq_attendance_anomalies_day_type"
        ),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="RESTRICT"),
        # RESTRICT for the reason 0012 gives: the row that cleared an anomaly is
        # part of the working-time record and may not be removed by a cascade.
        sa.ForeignKeyConstraint(
            ["resolved_by_event_id"], ["attendance_events.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # One person's days, which is how the day view and the correction flow read it.
    op.create_index(
        "ix_attendance_anomalies_employee_date",
        "attendance_anomalies",
        ["employee_id", "business_date"],
    )
    # The reminder's queue: the unnotified rows of one date. Partial, because a
    # notified anomaly is never asked about again and those are the ones that pile up.
    op.create_index(
        "ix_attendance_anomalies_unnotified",
        "attendance_anomalies",
        ["business_date"],
        postgresql_where=sa.text("notified_at IS NULL"),
    )
    # "What did this correction clear", asked from the event side (ticket 24).
    op.create_index(
        "ix_attendance_anomalies_resolved_by",
        "attendance_anomalies",
        ["resolved_by_event_id"],
        postgresql_where=sa.text("resolved_by_event_id IS NOT NULL"),
    )

    op.execute(f"REVOKE DELETE ON attendance_anomalies FROM {APP_ROLE}")


def downgrade() -> None:
    op.drop_index("ix_attendance_anomalies_resolved_by", table_name="attendance_anomalies")
    op.drop_index("ix_attendance_anomalies_unnotified", table_name="attendance_anomalies")
    op.drop_index("ix_attendance_anomalies_employee_date", table_name="attendance_anomalies")
    op.drop_table("attendance_anomalies")
