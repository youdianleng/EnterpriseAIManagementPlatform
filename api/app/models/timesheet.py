"""Timesheet models.

Two tables for 周工时表 → 工时条目 (DESIGN §3.3), and the columns that carry a
decision. Migration 0015 is where the argument for each one lives; what follows is
the part a reader of the ORM needs:

* **`week_start` is a Monday.** Unique with `employee_id`, so "one timesheet per
  person per week" is a constraint rather than a service check, and the trigger
  refuses a key that is not a Monday on both tables.
* **`minutes` is a positive integer no longer than a day.** The ceiling exists so a
  mistyped `4800` is refused; it is emphatically not the day's expected hours, which
  is a warning the product computes and never a schema error.
* **`employee_id` and `week_start` are on the entry as well as on the week,** and a
  composite foreign key checks all three against one row. The duplication is what
  makes the grid's read one index scan instead of a join, and the composite key is
  what makes it impossible for the two copies to disagree.
* **`(task_id, project_id)` is a composite foreign key** onto `project_tasks`, so an
  entry cannot name project A with a task of project B.
* **`is_billable` is stored, and it is the project module's answer.** A task's
  configuration may change; re-resolving on read would restate what a closed month
  was worth.
* **The project's status and dates are guarded by a trigger**, not by a foreign key:
  a `CHECK` cannot read another table, and the rule is one-directional — an entry may
  not be written against a project that is not running, while a project may be
  archived at any time with its entries intact.

Ticket 29 adds a second sheet per week and a negative row, and three columns carry
that (migration 0022 is where the argument lives):

* **`supersedes_timesheet_id` is set on the *supplement*, never on the original.** The
  original is the record that may not change, so the pointer travels with the row
  that was written later; `uq_timesheets_identity` is what the composite foreign key
  onto `(id, employee_id, week_start)` references, which is what makes "a supplement
  is another sheet of the same person's same week" a database fact.
* **One *original* sheet per person per week.** `uq_timesheets_employee_week` is a
  partial unique index over the rows that are not supplements — ticket 28's rule,
  narrowed to the rows it was about.
* **`entry_type` and `reverses_entry_id` make the reversal pair.** A reversal is the
  negation of one locked entry, so `minutes` is no longer always positive: the range
  check keeps its 24-hour ceiling and refuses zero, and the *sign* is the entry
  type's business. A trigger refuses a reversal that does not negate its original
  exactly, and refuses a change to an entry that has already been reversed.

No row-level policy: whose week it is, is what governs it, and that is the kernel's
`SELF_ONLY_ACTIONS` plus the `employee_id` predicate every query in
`repositories/timesheet.py` carries.
"""

from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db_metadata import Base

#: The closed status set, as the database states it. Written out rather than built
#: from the enum, so a status added to the module is a migration rather than a
#: silent widening of what the column accepts.
STATUSES_SQL = "('draft', 'pending', 'approved', 'rejected')"

#: A day, to the minute. Named so the constraint and `timesheet.MAX_ENTRY_MINUTES`
#: are visibly the same number rather than two that happen to agree.
MAX_ENTRY_MINUTES_SQL = 24 * 60

#: The two entry kinds, as the database states them. Written out for the reason the
#: status list is: a kind added to the module is a migration.
ENTRY_TYPES_SQL = "('normal', 'reversal')"

#: `minutes` is non-zero, no longer than a day in either direction, and the sign is the
#: entry type's. Ticket 28's ceiling is kept — it is what refuses a typed `4800` — and
#: the widening is exactly "a reversal is negative", with zero refused both ways.
MINUTES_RANGE_SQL = (
    "minutes <> 0 AND abs(minutes) <= 1440 AND ("
    "(entry_type = 'normal' AND minutes > 0) OR "
    "(entry_type = 'reversal' AND minutes < 0))"
)


class Timesheet(Base):
    """One employee's one week, or one supplement *of* that week.

    A week of somebody's work is one original sheet plus however many supplements
    have been filed against it; they all carry the same Monday, which is why the
    uniqueness below is a partial index rather than a plain constraint.
    """

    __tablename__ = "timesheets"
    __table_args__ = (
        CheckConstraint(f"status IN {STATUSES_SQL}", name="ck_timesheets_status"),
        # `extract(isodow)` is 1 for Monday. A Tuesday key would split one week into
        # two rows and make every total in the product wrong at once.
        CheckConstraint(
            "extract(isodow from week_start) = 1", name="ck_timesheets_week_start_is_monday"
        ),
        # The link and the flag are one fact; neither may move without the other.
        CheckConstraint(
            "is_supplementary = (supersedes_timesheet_id IS NOT NULL)",
            name="ck_timesheets_supplementary_link",
        ),
        CheckConstraint(
            "supersedes_timesheet_id IS NULL OR supersedes_timesheet_id <> id",
            name="ck_timesheets_not_self_superseding",
        ),
        # One *original* sheet per person per week. Ticket 28's constraint, narrowed to
        # the rows it was about: a supplement is a second sheet for the same Monday.
        Index(
            "uq_timesheets_employee_week",
            "employee_id",
            "week_start",
            unique=True,
            postgresql_where=text("supersedes_timesheet_id IS NULL"),
        ),
        # The key the entries reference, and the key a supplement references to prove it
        # corrects the same person's same week.
        UniqueConstraint("id", "employee_id", "week_start", name="uq_timesheets_identity"),
        # A supplement corrects one person's one week, or it is refused.
        ForeignKeyConstraint(
            ["supersedes_timesheet_id", "employee_id", "week_start"],
            ["timesheets.id", "timesheets.employee_id", "timesheets.week_start"],
            name="fk_timesheets_supersedes",
            ondelete="RESTRICT",
        ),
        Index("ix_timesheets_employee_week_start", "employee_id", "week_start"),
        # "Which supplements does this week have" is this index.
        Index("ix_timesheets_supersedes", "supersedes_timesheet_id"),
        # "What is waiting for me" is asked of the status.
        Index("ix_timesheets_status", "status"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    employee_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="RESTRICT"),
        nullable=False,
    )
    #: The Monday of the week. See the module docstring.
    week_start: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    #: The week this sheet corrects, set on the supplement and null on the original. The
    #: original is never edited, so the pointer travels with the row written later.
    supersedes_timesheet_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    is_supplementary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    #: The request the week is filed under. Null while it is a draft, and replaced on
    #: a resubmission: the rounds are the engine's history, not a column here.
    approval_request_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("approval_requests.id", ondelete="RESTRICT"),
        nullable=True,
    )
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Timesheet {self.employee_id} week={self.week_start} {self.status}>"


class TimesheetWeekLock(Base):
    """A week nobody may write to any more, whoever is asking.

    One row per closed week. It is a record rather than a comparison against the clock
    because the answer has to be the same tomorrow: `time_entries_guard_week_lock` reads
    this table, so a console writing a week that fell out of the eight-week window is
    refused by the same fact the service refuses it with.
    """

    __tablename__ = "timesheet_weeks_lock"
    __table_args__ = (
        CheckConstraint(
            "extract(isodow from week_start) = 1", name="ck_timesheet_weeks_lock_is_monday"
        ),
    )

    week_start: Mapped[date] = mapped_column(Date, primary_key=True)
    locked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    #: Null when the system closed the week (it fell out of the window); a person's id
    #: when somebody closed it deliberately, which is a different fact afterwards.
    locked_by_employee_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="RESTRICT"),
        nullable=True,
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<TimesheetWeekLock {self.week_start}>"


class TimesheetEntry(Base):
    """One day's work on one task, or the reversal of one such row.

    `normal` rows are what somebody recorded; `reversal` rows are the other half of a
    supplementary correction — the negation of one locked row, pointing at it. Both
    live in this table because a total is a `sum(minutes)` over both, and a second
    table would be a second place every report has to remember to look.
    """

    __tablename__ = "timesheet_entries"
    __table_args__ = (
        # Non-zero, no longer than a day, and signed by the entry type. Ticket 28's
        # ceiling is kept: it is what refuses a typed `4800`, and it is not the day's
        # expected hours. See the module docstring.
        CheckConstraint(MINUTES_RANGE_SQL, name="ck_timesheet_entries_minutes_range"),
        CheckConstraint(f"entry_type IN {ENTRY_TYPES_SQL}", name="ck_timesheet_entries_type"),
        # A reversal without an original is a negative row nobody can account for; an
        # original with one is a pair whose other half is missing.
        CheckConstraint(
            "(entry_type = 'reversal') = (reverses_entry_id IS NOT NULL)",
            name="ck_timesheet_entries_reversal_link",
        ),
        CheckConstraint(
            "entry_date >= week_start AND entry_date < week_start + 7",
            name="ck_timesheet_entries_date_in_week",
        ),
        CheckConstraint(
            "length(btrim(note)) > 0 OR note IS NULL",
            name="ck_timesheet_entries_note_not_blank",
        ),
        # All three columns against one `timesheets` row, so an entry cannot land in
        # somebody else's week or in a week that is not the one it names.
        ForeignKeyConstraint(
            ["timesheet_id", "employee_id", "week_start"],
            ["timesheets.id", "timesheets.employee_id", "timesheets.week_start"],
            name="fk_timesheet_entries_week",
            ondelete="CASCADE",
        ),
        # The task has to belong to the project the entry names.
        ForeignKeyConstraint(
            ["task_id", "project_id"],
            ["project_tasks.id", "project_tasks.project_id"],
            name="fk_timesheet_entries_task_project",
            ondelete="RESTRICT",
        ),
        # The row this one cancels. RESTRICT rather than CASCADE: a reversal is evidence,
        # and the entry it reverses is what makes it readable.
        ForeignKeyConstraint(
            ["reverses_entry_id"],
            ["timesheet_entries.id"],
            name="fk_timesheet_entries_reverses",
            ondelete="RESTRICT",
        ),
        # The grid's read: one person's week, in day order.
        Index(
            "ix_timesheet_entries_employee_week",
            "employee_id",
            "week_start",
            "entry_date",
        ),
        Index("ix_timesheet_entries_task", "task_id"),
        Index("ix_timesheet_entries_project", "project_id"),
        Index("ix_timesheet_entries_reverses", "reverses_entry_id"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    timesheet_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    employee_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    week_start: Mapped[date] = mapped_column(Date, nullable=False)
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    project_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    task_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    #: `normal` or `reversal`. The sign of `minutes` follows from this.
    entry_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="normal", server_default=text("'normal'")
    )
    #: The entry this one cancels, set on reversals and null on everything else.
    reverses_entry_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    #: The project module's resolved answer, stored rather than derived. A reversal
    #: carries its original's value, so a billable subtotal nets to zero with it.
    is_billable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<TimesheetEntry {self.entry_date} task={self.task_id} {self.minutes}m>"


__all__ = [
    "ENTRY_TYPES_SQL",
    "MAX_ENTRY_MINUTES_SQL",
    "MINUTES_RANGE_SQL",
    "STATUSES_SQL",
    "Timesheet",
    "TimesheetEntry",
    "TimesheetWeekLock",
]
