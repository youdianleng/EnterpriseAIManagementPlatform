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
    UniqueConstraint,
    func,
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


class Timesheet(Base):
    """One employee's one week."""

    __tablename__ = "timesheets"
    __table_args__ = (
        CheckConstraint(f"status IN {STATUSES_SQL}", name="ck_timesheets_status"),
        # `extract(isodow)` is 1 for Monday. A Tuesday key would split one week into
        # two rows and make every total in the product wrong at once.
        CheckConstraint(
            "extract(isodow from week_start) = 1", name="ck_timesheets_week_start_is_monday"
        ),
        UniqueConstraint("employee_id", "week_start", name="uq_timesheets_employee_week"),
        # The key the entries reference. `id` is unique on its own, so this adds no
        # restriction here; it exists so an entry can be pinned to one week's row
        # rather than merely to *a* week that shares its employee and start date.
        UniqueConstraint("id", "employee_id", "week_start", name="uq_timesheets_identity"),
        Index("ix_timesheets_employee_week_start", "employee_id", "week_start"),
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


class TimesheetEntry(Base):
    """One day's work on one task."""

    __tablename__ = "timesheet_entries"
    __table_args__ = (
        # Positive, and no longer than a day. The ceiling is not the day's expected
        # hours: see the module docstring.
        CheckConstraint(
            f"minutes > 0 AND minutes <= {MAX_ENTRY_MINUTES_SQL}",
            name="ck_timesheet_entries_minutes_range",
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
        # The grid's read: one person's week, in day order.
        Index(
            "ix_timesheet_entries_employee_week",
            "employee_id",
            "week_start",
            "entry_date",
        ),
        Index("ix_timesheet_entries_task", "task_id"),
        Index("ix_timesheet_entries_project", "project_id"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    timesheet_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    employee_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    week_start: Mapped[date] = mapped_column(Date, nullable=False)
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    project_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    task_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The project module's resolved answer, stored rather than derived.
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


__all__ = ["MAX_ENTRY_MINUTES_SQL", "STATUSES_SQL", "Timesheet", "TimesheetEntry"]
