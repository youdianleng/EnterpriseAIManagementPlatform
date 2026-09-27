"""Work schedules, the holiday table, and the monthly expected-hours snapshot.

Revision ID: 0013
Revises: 0012
Created: 2026-09-30

Five new tables and two amendments to what is already there (DESIGN §3.2, Q22,
D6). What is worth reading before the DDL:

* **The schedule is a pattern, not a number.** `work_schedule_days` is one row per
  weekday, so the intensivo calendar — Monday to Thursday eight hours, Friday six
  — is expressible. `expected_minutes` is *validated* against `start_time`,
  `end_time` and `break_minutes` by a CHECK constraint rather than derived from
  them, so the stored number is the one a reader uses and a window that does not
  add up is refused at the door. `weekly_hours` is the derived one: it is a sum of
  the days and computing it in one place is what stops it disagreeing with them.
* **Overrides have dates, and cannot overlap.** Two rows covering one day would
  make "the rules in March" undecidable, and `btree_gist`'s exclusion constraint
  refuses the second one rather than leaving the resolution order to whichever row
  a query returns first. `effective_to IS NULL` is an open-ended window and the
  range is `[]`-bounded, so an override that ends on the day another begins is
  still an overlap.
* **`holidays` is data.** A year is imported from a file and edited by HR; nothing
  in Python knows a date. `year` is stored and constrained to `EXTRACT(YEAR FROM
  date)` so the cache key and the date cannot disagree, and the uniqueness rule is
  `NULLS NOT DISTINCT` because a national holiday's region is NULL and
  `NULL <> NULL` would let the same national holiday be imported twice.
* **`expected_hours_snapshots` is append-only in the database.** `REVOKE UPDATE,
  DELETE` is what makes "editing a schedule next year does not change what March's
  figure was" a property of PostgreSQL rather than of the code paths that exist
  today. Recomputing appends a revision; nothing rewrites one.
* **`departments.region_code`** is where a person's region comes from. There is no
  per-employee field for it and inventing one would put a fact about a workplace on
  a person; the department a shift is worked in is what decides which regional and
  local holidays are observed.
* **`attendance_daily.status` gains `holiday` and `non_working`.** Ticket 21 left
  the constraint closed over the five statuses derivable from events alone, and
  said so; a day the schedule does not expect is not an absence, and once a
  schedule exists the record can say which it was. `late` is deliberately absent —
  that is ticket 23's, and it needs a schedule *and* a threshold.

No `GRANT` statement: migration 0007 set default privileges, so tables added later
are reachable by the runtime role. `SELECT, INSERT` alone on the snapshot table is
the exception, and it is the point of it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "eam_app"

HOLIDAY_SCOPES = "('national', 'regional', 'local')"

#: Matches `app/models/schedule.py`'s `DAY_CONSISTENCY_SQL`. Kept as a literal
#: rather than imported: a migration describes the schema at one moment, and an
#: import would let a later edit to the model rewrite what this revision did.
DAY_CONSISTENCY = """
    (
        expected_minutes = 0
        AND start_time IS NULL
        AND end_time IS NULL
        AND break_minutes = 0
    )
    OR (
        expected_minutes > 0
        AND start_time IS NOT NULL
        AND end_time IS NOT NULL
        AND end_time > start_time
        AND expected_minutes
            = CAST(EXTRACT(EPOCH FROM (end_time - start_time)) / 60 AS integer) - break_minutes
    )
"""

#: Ticket 21's set, plus the two a schedule makes decidable.
ATTENDANCE_STATUSES = (
    "('working', 'ok', 'missing_out', 'incomplete', 'absent', 'holiday', 'non_working')"
)
PREVIOUS_ATTENDANCE_STATUSES = "('working', 'ok', 'missing_out', 'incomplete', 'absent')"


def upgrade() -> None:
    # The exclusion constraint on overrides is a gist index over a uuid equality
    # and a range overlap, and gist has no uuid operator class of its own.
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    op.add_column("departments", sa.Column("region_code", sa.String(length=16), nullable=True))

    op.create_table(
        "work_schedules",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name_es", sa.String(length=160), nullable=False),
        sa.Column("name_en", sa.String(length=160), nullable=False),
        sa.Column("department_id", sa.UUID(), nullable=True),
        sa.Column("weekly_hours", sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("weekly_hours > 0 AND weekly_hours <= 168", name="ck_work_schedules_hours"),
        sa.CheckConstraint(
            "NOT is_default OR department_id IS NULL", name="ck_work_schedules_default_scope"
        ),
        # RESTRICT: a month measured against a schedule outlives the department it
        # was configured for.
        sa.ForeignKeyConstraint(["department_id"], ["departments.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_work_schedules_code"),
    )
    op.create_index(
        "uq_work_schedules_default",
        "work_schedules",
        ["is_default"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )
    op.create_index(
        "uq_work_schedules_department",
        "work_schedules",
        ["department_id"],
        unique=True,
        postgresql_where=sa.text("department_id IS NOT NULL AND is_active"),
    )
    op.create_index("ix_work_schedules_department_id", "work_schedules", ["department_id"])

    op.create_table(
        "work_schedule_days",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("schedule_id", sa.UUID(), nullable=False),
        sa.Column("weekday", sa.SmallInteger(), nullable=False),
        sa.Column("expected_minutes", sa.Integer(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=True),
        sa.Column("end_time", sa.Time(), nullable=True),
        sa.Column("break_minutes", sa.Integer(), nullable=False),
        sa.CheckConstraint("weekday BETWEEN 0 AND 6", name="ck_work_schedule_days_weekday"),
        sa.CheckConstraint(
            "expected_minutes BETWEEN 0 AND 1440", name="ck_work_schedule_days_minutes"
        ),
        sa.CheckConstraint("break_minutes >= 0", name="ck_work_schedule_days_break"),
        sa.CheckConstraint(DAY_CONSISTENCY, name="ck_work_schedule_days_consistent"),
        sa.ForeignKeyConstraint(["schedule_id"], ["work_schedules.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "schedule_id", "weekday", name="uq_work_schedule_days_schedule_weekday"
        ),
    )
    op.create_index("ix_work_schedule_days_schedule_id", "work_schedule_days", ["schedule_id"])

    op.create_table(
        "employee_schedule_overrides",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("employee_id", sa.UUID(), nullable=False),
        sa.Column("schedule_id", sa.UUID(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_by_employee_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_employee_schedule_overrides_window",
        ),
        sa.CheckConstraint(
            "length(btrim(reason)) > 0", name="ck_employee_schedule_overrides_reason"
        ),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["schedule_id"], ["work_schedules.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_employee_schedule_overrides_employee",
        "employee_schedule_overrides",
        ["employee_id", "effective_from"],
    )
    op.execute(
        """
        ALTER TABLE employee_schedule_overrides
        ADD CONSTRAINT ex_employee_schedule_overrides_window
        EXCLUDE USING gist (
            employee_id WITH =,
            daterange(effective_from, effective_to, '[]') WITH &&
        )
        """
    )

    op.create_table(
        "holidays",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("name_es", sa.String(length=160), nullable=False),
        sa.Column("name_en", sa.String(length=160), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("region_code", sa.String(length=16), nullable=True),
        sa.Column("year", sa.SmallInteger(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(f"scope IN {HOLIDAY_SCOPES}", name="ck_holidays_scope"),
        # Regional and local holidays are matched by region code, so one without a
        # region could never apply to anybody.
        sa.CheckConstraint(
            "scope = 'national' OR region_code IS NOT NULL", name="ck_holidays_region_required"
        ),
        sa.CheckConstraint(
            "scope <> 'national' OR region_code IS NULL", name="ck_holidays_region_unexpected"
        ),
        sa.CheckConstraint("year = EXTRACT(YEAR FROM date)", name="ck_holidays_year_matches_date"),
        sa.PrimaryKeyConstraint("id"),
        # NULLS NOT DISTINCT: a national holiday carries no region, and without
        # this the same one could be imported on every run.
        sa.UniqueConstraint(
            "date",
            "scope",
            "region_code",
            name="uq_holidays_date_scope_region",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index("ix_holidays_year", "holidays", ["year"])
    op.create_index("ix_holidays_date", "holidays", ["date"])

    op.create_table(
        "expected_hours_snapshots",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("employee_id", sa.UUID(), nullable=False),
        sa.Column("year", sa.SmallInteger(), nullable=False),
        sa.Column("month", sa.SmallInteger(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("expected_minutes", sa.Integer(), nullable=False),
        sa.Column("inputs", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("computed_by_employee_id", sa.UUID(), nullable=True),
        sa.Column(
            "computed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("month BETWEEN 1 AND 12", name="ck_expected_hours_snapshots_month"),
        sa.CheckConstraint("year BETWEEN 2000 AND 2200", name="ck_expected_hours_snapshots_year"),
        sa.CheckConstraint("revision > 0", name="ck_expected_hours_snapshots_revision"),
        sa.CheckConstraint("expected_minutes >= 0", name="ck_expected_hours_snapshots_minutes"),
        sa.CheckConstraint(
            "jsonb_typeof(inputs) = 'object'", name="ck_expected_hours_snapshots_inputs"
        ),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "employee_id", "year", "month", "revision", name="uq_expected_hours_snapshots_revision"
        ),
    )
    op.create_index(
        "ix_expected_hours_snapshots_employee_period",
        "expected_hours_snapshots",
        ["employee_id", "year", "month"],
    )
    # The evidence rule, as a privilege rather than a convention.
    op.execute(f"REVOKE UPDATE, DELETE ON expected_hours_snapshots FROM {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON expected_hours_snapshots TO {APP_ROLE}")

    op.drop_constraint("ck_attendance_daily_status", "attendance_daily", type_="check")
    op.create_check_constraint(
        "ck_attendance_daily_status", "attendance_daily", f"status IN {ATTENDANCE_STATUSES}"
    )


def downgrade() -> None:
    op.drop_constraint("ck_attendance_daily_status", "attendance_daily", type_="check")
    op.create_check_constraint(
        "ck_attendance_daily_status",
        "attendance_daily",
        f"status IN {PREVIOUS_ATTENDANCE_STATUSES}",
    )

    op.drop_index(
        "ix_expected_hours_snapshots_employee_period", table_name="expected_hours_snapshots"
    )
    op.drop_table("expected_hours_snapshots")
    op.drop_index("ix_holidays_date", table_name="holidays")
    op.drop_index("ix_holidays_year", table_name="holidays")
    op.drop_table("holidays")
    op.execute(
        "ALTER TABLE employee_schedule_overrides DROP CONSTRAINT ex_employee_schedule_overrides_window"
    )
    op.drop_index(
        "ix_employee_schedule_overrides_employee", table_name="employee_schedule_overrides"
    )
    op.drop_table("employee_schedule_overrides")
    op.drop_index("ix_work_schedule_days_schedule_id", table_name="work_schedule_days")
    op.drop_table("work_schedule_days")
    op.drop_index("ix_work_schedules_department_id", table_name="work_schedules")
    op.drop_index("uq_work_schedules_department", table_name="work_schedules")
    op.drop_index("uq_work_schedules_default", table_name="work_schedules")
    op.drop_table("work_schedules")
    op.drop_column("departments", "region_code")
    # btree_gist is left in place: dropping an extension another object may depend
    # on is not this revision's to do.
