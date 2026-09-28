"""Supplementary submissions: the permanent lock, the reversal pair, the week lock.

Revision ID: 0023
Revises: 0022
Created: 2026-10-05

**It chains onto ticket 32's chunk-embeddings revision, which was written first.**
Two tickets were in flight at once and both found 0021 as the head, which Alembic
reports as "Revision 0022 is present more than once" rather than as anything
schema-shaped — the same collision ticket 28's migration records, and the same
answer: the one written second takes the next number and the chain stays linear.
Nothing here depends on ticket 32's tables or it on these; the line of descent is
Alembic's requirement, not a relationship.

Ticket 29 (DESIGN §7.4). Five changes, and each one is a rule that had to move
somewhere rather than a column that had to exist:

* **One *original* sheet per person per week, not one sheet.** A supplement is a
  second `timesheets` row for the *same* Monday — it is how a locked week is
  corrected without touching it — so `uq_timesheets_employee_week` becomes a
  partial unique index over the rows that are not supplements. The constraint that
  ticket 28 made a database fact ("one timesheet per person per week") survives,
  narrowed to the rows it was always about; the supplements are the exception the
  ticket asks for, and they are an exception of exactly one kind.

* **The link points from the supplement to the week it corrects, and its shape is
  a database fact.** `supersedes_timesheet_id` is set on the *new* row, never on
  the original: the original is the record that must not change, and a column on it
  would have to be rewritten every time somebody corrected it. `fk_timesheets_supersedes`
  is a **composite** foreign key onto `(id, employee_id, week_start)` — the key
  ticket 28 already added — so a supplement cannot correct another person's week or
  a week it does not itself name; and the trigger below refuses a supplement whose
  target is another supplement, which keeps the chain one hop deep and makes "the
  week's sheets" a flat list rather than a tree.

* **A reversal is a negative entry, so the minutes CHECK is widened deliberately.**
  `ck_timesheet_entries_minutes_range` was `minutes > 0 AND minutes <= 1440`.
  Ticket 28's argument for it still holds — the ceiling exists so a typed `4800` is
  refused, not to police a day — and it is *kept*; what changes is that the sign is
  now the entry type's business rather than always positive. Zero is refused in
  both directions (it is not an amount), the 24-hour ceiling applies to the
  magnitude, and a `normal` row may only be positive while a `reversal` row may
  only be negative. The alternative — leaving the column positive and storing a
  `direction` flag — was rejected because every total in the product (the grid, the
  report ticket 30 builds, an export) is a `sum(minutes)`, and a sum that has to
  know about a flag is a sum somebody eventually writes without it.

* **The reversal pair is guarded where it is written, not where it is read.**
  `time_entries_guard_reversal` refuses a reversal that does not negate its
  original exactly (same day, project, task, employee, billable flag, and
  `minutes = -original.minutes`), a reversal of a reversal, and a change to an
  entry that has already been reversed. That is the whole guarantee the ticket's
  arithmetic rests on: `original + reversal + new` is the net because the pair
  cancels, and a pair that could be edited apart would make the net a story.

* **A locked week cannot be written by anything, and neither can a closed one.**
  `time_entries_guard_week_lock` refuses INSERT, UPDATE and DELETE on
  `timesheet_entries` when the entry's sheet is `approved` (ticket 29's permanent
  lock — the service refuses first, with a catalogued code, and this is the backstop
  under it) or when the entry's week appears in the new `timesheet_weeks_lock`
  table. The global week lock is a **record** rather than a clock comparison, and
  that is deliberate: a `CHECK` may not read the clock at all, and a trigger that
  compared `now()` to `week_start` would refuse a restored dump of last year's
  history and would make the answer to "may this be written" depend on the moment
  the question is asked. The service computes the eight-week window from the clock
  and writes the lock row when a week falls out of it; the row is then the fact
  everybody else — the trigger, a report, the interface — reads.

`timesheet_weeks_lock` carries `locked_by_employee_id` because a lock can also be
taken deliberately by HR (a closed payroll month), and `reason` because the two
cases read differently afterwards. Its key is the Monday, and the Monday rule is
enforced by the function ticket 28's migration already installed rather than by a
second copy of it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SHEETS = "timesheets"
ENTRIES = "timesheet_entries"
LOCKS = "timesheet_weeks_lock"

#: The two entry kinds, as the column states them. Written out rather than built from
#: the enum, for the reason ticket 28's migration gives for the status list: a kind
#: added to the module is a migration rather than a silent widening.
ENTRY_TYPES_SQL = "('normal', 'reversal')"

#: `minutes` is non-zero, no longer than a day in either direction, and its sign is
#: the entry type's. See the module docstring.
MINUTES_RANGE_SQL = (
    "minutes <> 0 AND abs(minutes) <= 1440 AND ("
    "(entry_type = 'normal' AND minutes > 0) OR "
    "(entry_type = 'reversal' AND minutes < 0))"
)


def upgrade() -> None:
    _extend_sheets()
    _extend_entries()
    _create_week_lock()
    _create_guards()


def _extend_sheets() -> None:
    """The supplement link, and the uniqueness narrowed to the originals."""
    op.add_column(
        SHEETS, sa.Column("supersedes_timesheet_id", sa.UUID(), nullable=True)
    )
    # The flag is the link restated, and the CHECK below is what stops the two from
    # ever disagreeing. It exists so a list or a report can filter on it without a
    # second join, which is the same argument ticket 28 made for the denormalised
    # `employee_id` on an entry.
    op.add_column(
        SHEETS,
        sa.Column(
            "is_supplementary",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_check_constraint(
        "ck_timesheets_supplementary_link",
        SHEETS,
        "is_supplementary = (supersedes_timesheet_id IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_timesheets_not_self_superseding",
        SHEETS,
        "supersedes_timesheet_id IS NULL OR supersedes_timesheet_id <> id",
    )
    # All three columns against one row of this same table: a supplement is another
    # sheet *of the week it corrects*, for the same person. `uq_timesheets_identity`
    # is ticket 28's key and is what makes this referenceable.
    op.create_foreign_key(
        "fk_timesheets_supersedes",
        SHEETS,
        SHEETS,
        ["supersedes_timesheet_id", "employee_id", "week_start"],
        ["id", "employee_id", "week_start"],
        ondelete="RESTRICT",
    )
    # "Which supplements does this week have" is this index, and it is also what keeps
    # the delete of an original from being a table scan.
    op.create_index("ix_timesheets_supersedes", SHEETS, ["supersedes_timesheet_id"])

    # The constraint ticket 28 added, replaced by the same rule over the rows that are
    # not supplements. Dropped and recreated in one migration rather than kept beside a
    # second index, because two unique indexes that disagree about what "the week" is
    # would let a race write two originals.
    op.drop_constraint("uq_timesheets_employee_week", SHEETS, type_="unique")
    op.create_index(
        "uq_timesheets_employee_week",
        SHEETS,
        ["employee_id", "week_start"],
        unique=True,
        postgresql_where=sa.text("supersedes_timesheet_id IS NULL"),
    )


def _extend_entries() -> None:
    """The entry kind, the pointer at the original, and the widened range check."""
    op.add_column(
        ENTRIES,
        sa.Column(
            "entry_type",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'normal'"),
        ),
    )
    op.add_column(ENTRIES, sa.Column("reverses_entry_id", sa.UUID(), nullable=True))
    op.create_check_constraint(
        "ck_timesheet_entries_type", ENTRIES, f"entry_type IN {ENTRY_TYPES_SQL}"
    )
    # A reversal without an original would be a negative row nobody can account for, and
    # an original *with* one would be a pair whose other half is missing.
    op.create_check_constraint(
        "ck_timesheet_entries_reversal_link",
        ENTRIES,
        "(entry_type = 'reversal') = (reverses_entry_id IS NOT NULL)",
    )
    op.create_foreign_key(
        "fk_timesheet_entries_reverses",
        ENTRIES,
        ENTRIES,
        ["reverses_entry_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_timesheet_entries_reverses", ENTRIES, ["reverses_entry_id"])

    op.drop_constraint("ck_timesheet_entries_minutes_range", ENTRIES, type_="check")
    op.create_check_constraint(
        "ck_timesheet_entries_minutes_range", ENTRIES, MINUTES_RANGE_SQL
    )


def _create_week_lock() -> None:
    """The global week lock: one row per week that no write path may touch."""
    op.create_table(
        LOCKS,
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column(
            "locked_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Nullable: the week that fell out of the window was closed by the system
        # rather than by a person, and attributing it to whoever happened to trigger
        # the sweep would be a lie the record tells about itself.
        sa.Column("locked_by_employee_id", sa.UUID(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "extract(isodow from week_start) = 1", name="ck_timesheet_weeks_lock_is_monday"
        ),
        sa.ForeignKeyConstraint(
            ["locked_by_employee_id"], ["employees.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("week_start"),
    )
    # The Monday rule is ticket 28's function, attached here rather than reimplemented:
    # a week key has one definition in this schema.
    op.execute(
        f"""
        CREATE TRIGGER timesheet_weeks_lock_is_monday
        BEFORE INSERT OR UPDATE OF week_start ON {LOCKS}
        FOR EACH ROW EXECUTE FUNCTION timesheet_week_is_monday()
        """
    )


def _create_guards() -> None:
    """The three backstops: the reversal pair, the sheet kind, and the two locks."""
    op.execute(
        f"""
        CREATE FUNCTION timesheet_entries_guard_reversal() RETURNS trigger AS $$
        DECLARE
            original {ENTRIES}%ROWTYPE;
        BEGIN
            IF NEW.entry_type = 'reversal' THEN
                SELECT * INTO original
                  FROM {ENTRIES}
                 WHERE id = NEW.reverses_entry_id;
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'timesheet_entries: no entry % to reverse', NEW.reverses_entry_id
                        USING ERRCODE = 'foreign_key_violation';
                END IF;
                IF original.entry_type <> 'normal' THEN
                    RAISE EXCEPTION
                        'timesheet_entries: entry % is itself a reversal', original.id
                        USING ERRCODE = 'check_violation';
                END IF;
                IF NEW.minutes <> -original.minutes THEN
                    RAISE EXCEPTION
                        'timesheet_entries: a reversal of % must be %, and % is not',
                        original.minutes, -original.minutes, NEW.minutes
                        USING ERRCODE = 'check_violation';
                END IF;
                IF NEW.entry_date <> original.entry_date
                   OR NEW.project_id <> original.project_id
                   OR NEW.task_id <> original.task_id
                   OR NEW.employee_id <> original.employee_id
                   OR NEW.is_billable <> original.is_billable THEN
                    RAISE EXCEPTION
                        'timesheet_entries: a reversal must name what entry % recorded',
                        original.id
                        USING ERRCODE = 'check_violation';
                END IF;
            ELSIF TG_OP = 'UPDATE'
                  AND (NEW.entry_date <> OLD.entry_date
                       OR NEW.project_id <> OLD.project_id
                       OR NEW.task_id <> OLD.task_id
                       OR NEW.minutes <> OLD.minutes
                       OR NEW.is_billable <> OLD.is_billable)
                  AND EXISTS (
                      SELECT 1 FROM {ENTRIES} r WHERE r.reverses_entry_id = NEW.id
                  ) THEN
                -- The pair cancels because the two rows are each other's negation.
                -- Letting the original move after it was reversed would leave a
                -- reversal nobody asked for and a net that no longer means anything.
                RAISE EXCEPTION
                    'timesheet_entries: entry % was reversed and may not change', NEW.id
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER time_entries_guard_reversal
        BEFORE INSERT OR UPDATE OF entry_date, project_id, task_id, minutes,
                                   is_billable, entry_type, reverses_entry_id
        ON {ENTRIES}
        FOR EACH ROW EXECUTE FUNCTION timesheet_entries_guard_reversal()
        """
    )

    # One hop, always onto an original. A supplement that corrected another supplement
    # would make "which sheet is the week's record" a question with several answers.
    op.execute(
        f"""
        CREATE FUNCTION timesheet_supplements_point_at_originals() RETURNS trigger AS $$
        DECLARE
            target_supersedes uuid;
        BEGIN
            IF NEW.supersedes_timesheet_id IS NULL THEN
                RETURN NEW;
            END IF;
            SELECT supersedes_timesheet_id INTO target_supersedes
              FROM {SHEETS} WHERE id = NEW.supersedes_timesheet_id;
            IF target_supersedes IS NOT NULL THEN
                RAISE EXCEPTION
                    'timesheets: % is a supplement; a correction points at the week''s own sheet',
                    NEW.supersedes_timesheet_id
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER timesheets_supplements_point_at_originals
        BEFORE INSERT OR UPDATE OF supersedes_timesheet_id ON {SHEETS}
        FOR EACH ROW EXECUTE FUNCTION timesheet_supplements_point_at_originals()
        """
    )

    op.execute(
        f"""
        CREATE FUNCTION timesheet_entries_guard_week_lock() RETURNS trigger AS $$
        DECLARE
            row_week   date;
            row_sheet  uuid;
            row_status text;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                row_week := OLD.week_start;
                row_sheet := OLD.timesheet_id;
            ELSE
                row_week := NEW.week_start;
                row_sheet := NEW.timesheet_id;
            END IF;

            IF EXISTS (SELECT 1 FROM {LOCKS} l WHERE l.week_start = row_week) THEN
                RAISE EXCEPTION
                    'timesheet_entries: the week of % is closed to every write', row_week
                    USING ERRCODE = 'check_violation';
            END IF;

            SELECT t.status INTO row_status FROM {SHEETS} t WHERE t.id = row_sheet;
            IF row_status = 'approved' THEN
                RAISE EXCEPTION
                    'timesheet_entries: timesheet % is approved; a locked week is corrected by a supplementary submission',
                    row_sheet
                    USING ERRCODE = 'check_violation';
            END IF;

            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    # Every write, not only the interesting columns: a note edit and a delete are
    # writes to a locked week like any other.
    op.execute(
        f"""
        CREATE TRIGGER time_entries_guard_week_lock
        BEFORE INSERT OR UPDATE OR DELETE ON {ENTRIES}
        FOR EACH ROW EXECUTE FUNCTION timesheet_entries_guard_week_lock()
        """
    )


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS time_entries_guard_week_lock ON {ENTRIES}")
    op.execute(f"DROP TRIGGER IF EXISTS timesheets_supplements_point_at_originals ON {SHEETS}")
    op.execute(f"DROP TRIGGER IF EXISTS time_entries_guard_reversal ON {ENTRIES}")
    op.execute("DROP FUNCTION IF EXISTS timesheet_entries_guard_week_lock()")
    op.execute("DROP FUNCTION IF EXISTS timesheet_supplements_point_at_originals()")
    op.execute("DROP FUNCTION IF EXISTS timesheet_entries_guard_reversal()")

    op.execute(f"DROP TRIGGER IF EXISTS timesheet_weeks_lock_is_monday ON {LOCKS}")
    op.drop_table(LOCKS)

    op.drop_constraint("ck_timesheet_entries_minutes_range", ENTRIES, type_="check")
    op.create_check_constraint(
        "ck_timesheet_entries_minutes_range", ENTRIES, "minutes > 0 AND minutes <= 1440"
    )
    op.drop_index("ix_timesheet_entries_reverses", table_name=ENTRIES)
    op.drop_constraint("fk_timesheet_entries_reverses", ENTRIES, type_="foreignkey")
    op.drop_constraint("ck_timesheet_entries_reversal_link", ENTRIES, type_="check")
    op.drop_constraint("ck_timesheet_entries_type", ENTRIES, type_="check")
    op.drop_column(ENTRIES, "reverses_entry_id")
    op.drop_column(ENTRIES, "entry_type")

    op.drop_index("uq_timesheets_employee_week", table_name=SHEETS)
    op.create_unique_constraint(
        "uq_timesheets_employee_week", SHEETS, ["employee_id", "week_start"]
    )
    op.drop_index("ix_timesheets_supersedes", table_name=SHEETS)
    op.drop_constraint("fk_timesheets_supersedes", SHEETS, type_="foreignkey")
    op.drop_constraint("ck_timesheets_not_self_superseding", SHEETS, type_="check")
    op.drop_constraint("ck_timesheets_supplementary_link", SHEETS, type_="check")
    op.drop_column(SHEETS, "is_supplementary")
    op.drop_column(SHEETS, "supersedes_timesheet_id")
