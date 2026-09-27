"""Weekly timesheets and the entries they hold.

Revision ID: 0015
Revises: 0014
Created: 2026-09-30

**Ticket 23's attendance-anomaly revision chains onto this one.** It was drafted as a
second 0015 within about a minute of this file, which Alembic reports as "Revision
0015 is present more than once" rather than as anything schema-shaped; it is being
renumbered to 0016 with this revision as its `down_revision`. Nothing here depends on
its tables or it on these — the chain is linear because Alembic requires a single
line of descent, not because the two tickets share anything.

Two tables (DESIGN §3.3), and six decisions worth reading before the DDL:

* **A week is keyed by its Monday, and the database says so.** `uq_timesheets_employee_week`
  makes "one timesheet per person per week" a fact rather than a service check, and
  `ck_timesheets_week_start_is_monday` refuses a Tuesday key. The second one is not
  belt and braces: a Tuesday key would split one week into two rows silently, and
  every total in the product would then be wrong in a way nothing reports.

* **Minutes are a positive integer capped at 24 hours, not at the day's expected
  hours.** `ck_timesheet_entries_minutes_range` refuses zero, negatives and anything
  longer than a day. The cap is there so that a typed `4800` is refused rather than
  stored; it is deliberately *not* the day's expected hours, because a day over its
  expectation is a warning the product shows and never a schema error — a constraint
  that refused it would lose the hour somebody actually worked, which is the silent
  truncation ticket 28 forbids. The warning is computed from `work_schedules` and
  never stored here.

* **An entry's task must belong to its own project, enforced by the database.**
  `fk_timesheet_entries_task_project` is a composite foreign key onto
  `project_tasks (id, project_id)` (the unique constraint added below). A service
  check alone would leave the pair free to disagree, and a row naming project A with
  a task of project B is a timesheet no report can attribute.

* **A project that is not `active` cannot receive a new entry — as a trigger, not
  as a constraint.** Ticket 27 asked for this explicitly, and a `CHECK` cannot
  express it: a check may not reference another table. `time_entries_guard_project`
  reads the project row for the row being written and refuses it when the status is
  not `active`, or when the entry's date falls outside the project's own dates. Two
  properties of the trigger are the point rather than incidental:

  - it fires on `INSERT` and on `UPDATE OF entry_date, project_id, task_id` of
    `timesheet_entries`, and **never** on a write to `projects`. So closing or
    archiving a project keeps every entry already recorded against it — the history
    ticket 27 promised — and only new time is refused;
  - it raises an exception rather than a constraint violation, so a row written by a
    console or a script is refused exactly as one written by the application is. The
    application refuses first, with a catalogued code; this is the backstop under it.

* **`employee_id` and `week_start` are denormalised onto the entry, on purpose, and
  pinned to the same timesheet.** Both columns exist so the grid's read is one index
  scan by `(employee_id, week_start)` rather than a join, and
  `fk_timesheet_entries_week` checks all three against one `timesheets` row — so they
  cannot drift. The alternative, deriving them by join, would make every grid read a
  join for values that are immutable once written.

* **No `ON DELETE` action that removes history.** `task_id` and `project_id` are
  RESTRICT, as ticket 27's migration said they would be. An entry is removed from a
  *draft* by its author and by nothing else — the audit trail keeps the fact — and a
  timesheet is never deleted at all: an approved week is the record of what the
  company was billed for. The one place a cascade appears is the entry's own composite
  key onto its week, which exists for tests and for a future retention job rather than
  for any path this API offers.

No row-level policy. What governs a timesheet is *whose* it is, which is the kernel's
`SELF_ONLY_ACTIONS` plus the `employee_id` predicate every query in
`repositories/timesheet.py` carries, and a policy that repeated it would be a second
copy of a rule the application already cannot forget — the read that would leak is
the one that has no `employee_id` predicate at all, which no policy of this shape
would catch anyway.

No `GRANT` statement: migration 0007's default privileges cover tables added later.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The statuses a week can be in, as the column states them. Written out rather than
#: built from the enum, so a status added to the module is a migration rather than a
#: silent widening of what the column accepts.
STATUSES_SQL = "('draft', 'pending', 'approved', 'rejected')"

#: The composite key `timesheet_entries` references. Added here rather than in
#: migration 0014 because this is the table that needs it, and a constraint belongs
#: with the thing that depends on it.
TASK_PROJECT_KEY = "uq_project_tasks_id_project"

TABLE = "timesheet_entries"


def upgrade() -> None:
    # The pair an entry points at. `id` is already unique on its own, so this adds no
    # new restriction on `project_tasks`; it exists to be referenceable.
    op.create_unique_constraint(
        TASK_PROJECT_KEY, "project_tasks", ["id", "project_id"]
    )

    op.create_table(
        "timesheets",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("employee_id", sa.UUID(), nullable=False),
        # The Monday. See the module docstring.
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column(
            "status", sa.String(length=16), server_default=sa.text("'draft'"), nullable=False
        ),
        # The request the week is filed under. Null while it is a draft, and replaced
        # on a resubmission: the history of rounds is the engine's, not a column here.
        sa.Column("approval_request_id", sa.UUID(), nullable=True),
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
        sa.CheckConstraint(f"status IN {STATUSES_SQL}", name="ck_timesheets_status"),
        # `extract(isodow)` is 1 for Monday, which is the ISO week the product means.
        sa.CheckConstraint(
            "extract(isodow from week_start) = 1", name="ck_timesheets_week_start_is_monday"
        ),
        # One week per person, as a fact. A service check alone would let two
        # concurrent requests each write "the" week.
        sa.UniqueConstraint("employee_id", "week_start", name="uq_timesheets_employee_week"),
        # The same composite-key trick the entries use: it pins a timesheet's week to
        # a Monday through a column the trigger below can read on the entry side.
        sa.UniqueConstraint("id", "employee_id", "week_start", name="uq_timesheets_identity"),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["approval_request_id"], ["approval_requests.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_timesheets_employee_week_start", "timesheets", ["employee_id", "week_start"]
    )
    # "What is waiting for me" is the approver's query, and it is asked of the status.
    op.create_index("ix_timesheets_status", "timesheets", ["status"])

    op.create_table(
        TABLE,
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("timesheet_id", sa.UUID(), nullable=False),
        # Denormalised on purpose: see the module docstring. Pinned to the timesheet
        # by `fk_timesheet_entries_week` below.
        sa.Column("employee_id", sa.UUID(), nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("entry_date", sa.Date(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("minutes", sa.Integer(), nullable=False),
        # The project module's resolved answer, stored rather than derived: a task's
        # configuration may change later, and re-resolving on read would restate what
        # a closed month was worth.
        sa.Column("is_billable", sa.Boolean(), nullable=False),
        sa.Column("note", sa.String(length=500), nullable=True),
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
        # Positive, and no longer than a day. See the module docstring for why the
        # ceiling is 24 h and not the day's expected hours.
        sa.CheckConstraint(
            "minutes > 0 AND minutes <= 1440", name="ck_timesheet_entries_minutes_range"
        ),
        # `extract(isodow)` is 1 for Monday; the entry's own day has to be in its week.
        sa.CheckConstraint(
            "entry_date >= week_start AND entry_date < week_start + 7",
            name="ck_timesheet_entries_date_in_week",
        ),
        sa.CheckConstraint(
            "length(btrim(note)) > 0 OR note IS NULL",
            name="ck_timesheet_entries_note_not_blank",
        ),
        # All three columns against one timesheets row: an entry cannot land in
        # somebody else's week, or in a week that is not the one it names.
        sa.ForeignKeyConstraint(
            ["timesheet_id", "employee_id", "week_start"],
            ["timesheets.id", "timesheets.employee_id", "timesheets.week_start"],
            name="fk_timesheet_entries_week",
            ondelete="CASCADE",
        ),
        # The task has to belong to the project the entry names. This is the rule the
        # ticket asks to be a database fact rather than a service check.
        sa.ForeignKeyConstraint(
            ["task_id", "project_id"],
            ["project_tasks.id", "project_tasks.project_id"],
            name="fk_timesheet_entries_task_project",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # The grid's read: one person's week, in day order.
    op.create_index(
        "ix_timesheet_entries_employee_week", TABLE, ["employee_id", "week_start", "entry_date"]
    )
    op.create_index("ix_timesheet_entries_task", TABLE, ["task_id"])
    op.create_index("ix_timesheet_entries_project", TABLE, ["project_id"])

    _create_guards()


def _create_guards() -> None:
    """The two backstops: the project's state, and the week key on both tables.

    Written as triggers because a `CHECK` cannot read another table, and because the
    rule is one-directional: an entry may not be *written* against a project that is
    not running, while a project may be archived at any time with its entries intact.
    """
    op.execute(
        """
        CREATE FUNCTION timesheet_entries_guard_project() RETURNS trigger AS $$
        DECLARE
            row_status text;
            row_start  date;
            row_end    date;
            row_code   text;
        BEGIN
            SELECT p.status, p.start_date, p.end_date, p.code
              INTO row_status, row_start, row_end, row_code
              FROM projects p
             WHERE p.id = NEW.project_id;

            IF row_status IS NULL THEN
                RAISE EXCEPTION 'timesheet_entries: no project %', NEW.project_id
                    USING ERRCODE = 'foreign_key_violation';
            END IF;
            IF row_status <> 'active' THEN
                RAISE EXCEPTION
                    'timesheet_entries: project % is %, and only an active project accepts new time',
                    row_code, row_status
                    USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.entry_date < row_start
               OR (row_end IS NOT NULL AND NEW.entry_date > row_end) THEN
                RAISE EXCEPTION
                    'timesheet_entries: % is outside project % (%)',
                    NEW.entry_date, row_code, daterange(row_start, row_end, '[]')
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    # `UPDATE OF` matters: editing a note, or the minutes, must not re-run a check
    # whose answer cannot have changed — and must not fail on a project that was
    # archived since, which would make an old entry's note uncorrectable.
    op.execute(
        f"""
        CREATE TRIGGER time_entries_guard_project
        BEFORE INSERT OR UPDATE OF entry_date, project_id, task_id ON {TABLE}
        FOR EACH ROW EXECUTE FUNCTION timesheet_entries_guard_project()
        """
    )

    # The week key, on both tables, from one function. A Tuesday `week_start` would
    # split one week into two rows and make every total in the product wrong.
    op.execute(
        """
        CREATE FUNCTION timesheet_week_is_monday() RETURNS trigger AS $$
        BEGIN
            IF extract(isodow from NEW.week_start) <> 1 THEN
                RAISE EXCEPTION
                    'a timesheet week is keyed by its Monday, and % is not one',
                    NEW.week_start
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER timesheets_week_start_is_monday
        BEFORE INSERT OR UPDATE OF week_start ON timesheets
        FOR EACH ROW EXECUTE FUNCTION timesheet_week_is_monday()
        """
    )
    # `time_entries` again on the entry side. The composite key already pins the
    # entry's week to a timesheets row, and this states the same rule where the row
    # actually lands — so the refusal names the entry rather than a parent row the
    # writer never mentioned.
    op.execute(
        f"""
        CREATE TRIGGER time_entries_week_start_is_monday
        BEFORE INSERT OR UPDATE OF week_start ON {TABLE}
        FOR EACH ROW EXECUTE FUNCTION timesheet_week_is_monday()
        """
    )


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS time_entries_week_start_is_monday ON {TABLE}")
    op.execute("DROP TRIGGER IF EXISTS timesheets_week_start_is_monday ON timesheets")
    op.execute(f"DROP TRIGGER IF EXISTS time_entries_guard_project ON {TABLE}")
    op.execute("DROP FUNCTION IF EXISTS timesheet_week_is_monday()")
    op.execute("DROP FUNCTION IF EXISTS timesheet_entries_guard_project()")

    op.drop_index("ix_timesheet_entries_project", table_name=TABLE)
    op.drop_index("ix_timesheet_entries_task", table_name=TABLE)
    op.drop_index("ix_timesheet_entries_employee_week", table_name=TABLE)
    op.drop_table(TABLE)
    op.drop_index("ix_timesheets_status", table_name="timesheets")
    op.drop_index("ix_timesheets_employee_week_start", table_name="timesheets")
    op.drop_table("timesheets")
    op.drop_constraint(TASK_PROJECT_KEY, "project_tasks", type_="unique")
