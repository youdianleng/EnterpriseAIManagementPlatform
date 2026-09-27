"""Work schedules, holidays, and the monthly expected-hours snapshot.

Five tables, and the shape of each one is an answer to a question the four-year
retention obligation asks (`docs/DESIGN.md` §3.2, §8.1, D6/Q22):

* `work_schedules` + `work_schedule_days` are **a pattern, not a number**: one row
  per weekday, so "Monday to Thursday 8 hours, Friday 6" is expressible at all.
  `weekly_hours` is stored as well, because a contract is agreed in hours a week,
  and it is *derived* by the service from the days rather than typed twice.
* `employee_schedule_overrides` is how one person's week differs from their
  department's, with the dates it differs over. Without the dates the answer to
  "what were the rules in March" would change every time somebody's contract did.
* `holidays` is **data, never code** (Q22): the calendar is imported and edited by
  HR, so a year nobody has written code for is a file somebody uploads.
* `expected_hours_snapshots` is the evidence. It is append-only in the database —
  the runtime role cannot UPDATE or DELETE it (migration 0013) — and each row
  carries the inputs it was computed from, so editing a schedule next year cannot
  rewrite what March's figure was.

**`weekday` is 0 for Monday through 6 for Sunday**, which is Python's convention
(`date.weekday()`), not SQL's `ISODOW`. The resolver is Python and it builds the
lookup; a second convention at the storage boundary would be one off-by-one away
from every Friday being a Sunday, silently, in a table of numbers.
"""

from datetime import date, datetime, time
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db_metadata import Base

#: The five scopes a holiday may carry. The values are the design's (§3.2); the
#: *matching* rule is by `region_code`, and this column says who declared it.
HOLIDAY_SCOPES_SQL = "('national', 'regional', 'local')"

#: The window rule: a day either is not worked at all — no minutes, no window, no
#: break — or is worked from a start to an end that is later than it, with
#: `expected_minutes` equal to what that window is worth once the break is taken
#: out. Validated here rather than derived by the service, so the number a reader
#: finds is the number the schedule states and not one they have to recompute —
#: and so a window that does not add up is refused instead of quietly ignored.
#:
#: The whole expression is a constraint because the two halves are one fact: a row
#: with 480 minutes and a 09:00-14:00 window is not "slightly wrong", it is a row
#: that means two different things to two different readers.
DAY_CONSISTENCY_SQL = """
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


class WorkSchedule(Base):
    """A weekly pattern. `department_id` null is the company default."""

    __tablename__ = "work_schedules"
    __table_args__ = (
        UniqueConstraint("code", name="uq_work_schedules_code"),
        CheckConstraint("weekly_hours > 0 AND weekly_hours <= 168", name="ck_work_schedules_hours"),
        # A default schedule belongs to nobody: it is what applies where no
        # department has said otherwise, and a department-owned row claiming to be
        # the default would be two answers to one question.
        CheckConstraint(
            "NOT is_default OR department_id IS NULL", name="ck_work_schedules_default_scope"
        ),
        # Exactly one company default, enforced rather than guessed at: two would
        # make every employee without a department schedule resolve by whichever
        # row the planner happened to return first.
        Index(
            "uq_work_schedules_default",
            "is_default",
            unique=True,
            postgresql_where=text("is_default"),
        ),
        # One active schedule per department. The table has no validity dates, so
        # two active rows for one department would make "the department's schedule"
        # undecidable; a seasonal change edits the days of this one.
        Index(
            "uq_work_schedules_department",
            "department_id",
            unique=True,
            postgresql_where=text("department_id IS NOT NULL AND is_active"),
        ),
        Index("ix_work_schedules_department_id", "department_id"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name_es: Mapped[str] = mapped_column(String(160), nullable=False)
    name_en: Mapped[str] = mapped_column(String(160), nullable=False)
    #: RESTRICT: a schedule somebody's month was measured against outlives the act
    #: of removing a department from the tree.
    department_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("departments.id", ondelete="RESTRICT"),
        nullable=True,
    )
    #: Derived from the day rows by the service (sum / 60). Stored because it is
    #: what a contract is agreed in, derived because a sum typed twice is a sum
    #: that eventually disagrees with its parts.
    weekly_hours: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Deactivating removes a schedule from the catalogue — no new overrides, and
    #: it is no longer chosen for a department or as the default. It deliberately
    #: does *not* reach somebody an override already points at: deactivating a
    #: part-time pattern must not silently restore a full-time week.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<WorkSchedule {self.code} dept={self.department_id}>"


class WorkScheduleDay(Base):
    """One weekday of a schedule. Days with no row are days nobody works."""

    __tablename__ = "work_schedule_days"
    __table_args__ = (
        CheckConstraint("weekday BETWEEN 0 AND 6", name="ck_work_schedule_days_weekday"),
        CheckConstraint(
            "expected_minutes BETWEEN 0 AND 1440", name="ck_work_schedule_days_minutes"
        ),
        CheckConstraint("break_minutes >= 0", name="ck_work_schedule_days_break"),
        CheckConstraint(DAY_CONSISTENCY_SQL, name="ck_work_schedule_days_consistent"),
        UniqueConstraint("schedule_id", "weekday", name="uq_work_schedule_days_schedule_weekday"),
        Index("ix_work_schedule_days_schedule_id", "schedule_id"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    schedule_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("work_schedules.id", ondelete="RESTRICT"),
        nullable=False,
    )
    #: Monday is 0. See the module docstring for why this is not `ISODOW`.
    weekday: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    expected_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    start_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    end_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    break_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<WorkScheduleDay {self.schedule_id} wd={self.weekday} {self.expected_minutes}m>"


class EmployeeScheduleOverride(Base):
    """One person's week, for the dates it differs over."""

    __tablename__ = "employee_schedule_overrides"
    __table_args__ = (
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_employee_schedule_overrides_window",
        ),
        CheckConstraint("length(btrim(reason)) > 0", name="ck_employee_schedule_overrides_reason"),
        Index("ix_employee_schedule_overrides_employee", "employee_id", "effective_from"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    employee_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="RESTRICT"),
        nullable=False,
    )
    schedule_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("work_schedules.id", ondelete="RESTRICT"),
        nullable=False,
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    #: Null is open-ended: a part-time contract with no agreed end.
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    #: Why this person's week differs. Required, because "somebody changed it" is
    #: not an answer a labour inspector accepts, and the row is the only place the
    #: reason is written down.
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_employee_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<EmployeeScheduleOverride {self.employee_id} {self.effective_from}>"


class Holiday(Base):
    """A day nobody works. Imported and edited as data (Q22)."""

    __tablename__ = "holidays"
    __table_args__ = (
        CheckConstraint(f"scope IN {HOLIDAY_SCOPES_SQL}", name="ck_holidays_scope"),
        # A regional or local holiday that names no region can never match anybody
        # — the matching key is the region code — so it would be a row that looks
        # like a holiday and changes no figure anywhere.
        CheckConstraint(
            "scope = 'national' OR region_code IS NOT NULL", name="ck_holidays_region_required"
        ),
        # A national holiday naming a region is a mistake rather than a narrower
        # national holiday: everyone gets it either way, so the column would lie.
        CheckConstraint(
            "scope <> 'national' OR region_code IS NULL", name="ck_holidays_region_unexpected"
        ),
        # `year` is derived from `date` by every writer, and stated here so the
        # cache key and the date can never disagree.
        CheckConstraint("year = EXTRACT(YEAR FROM date)", name="ck_holidays_year_matches_date"),
        # One row per day, scope and region. NULLS NOT DISTINCT because a national
        # holiday's region is NULL, and `NULL <> NULL` would let the same national
        # holiday be imported twice.
        UniqueConstraint(
            "date",
            "scope",
            "region_code",
            name="uq_holidays_date_scope_region",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_holidays_year", "year"),
        Index("ix_holidays_date", "date"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    name_es: Mapped[str] = mapped_column(String(160), nullable=False)
    name_en: Mapped[str] = mapped_column(String(160), nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    #: ISO 3166-2, e.g. `ES-MD` for the Community of Madrid. For `local` the same
    #: code space is used: the municipality's holidays are observed by the people
    #: who work in that region, which is the granularity this company has.
    region_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    year: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Holiday {self.date} {self.scope} {self.region_code}>"


class ExpectedHoursSnapshot(Base):
    """What one employee's month was expected to be, and what said so.

    Append-only: `REVOKE UPDATE, DELETE` is issued in migration 0013, so a later
    edit to a schedule or a holiday cannot reach a figure that has already been
    written down. Recomputing appends a revision; the highest revision is the
    answer, and the revisions under it are the history of what the number used to
    be and why.
    """

    __tablename__ = "expected_hours_snapshots"
    __table_args__ = (
        CheckConstraint("month BETWEEN 1 AND 12", name="ck_expected_hours_snapshots_month"),
        CheckConstraint("year BETWEEN 2000 AND 2200", name="ck_expected_hours_snapshots_year"),
        CheckConstraint("revision > 0", name="ck_expected_hours_snapshots_revision"),
        CheckConstraint("expected_minutes >= 0", name="ck_expected_hours_snapshots_minutes"),
        # The inputs are a document, not a column per fact: a month can be governed
        # by more than one schedule (an override starting mid-month) and by more
        # than one region, and a shape that has to be read as a whole is stored as
        # one.
        CheckConstraint(
            "jsonb_typeof(inputs) = 'object'", name="ck_expected_hours_snapshots_inputs"
        ),
        UniqueConstraint(
            "employee_id",
            "year",
            "month",
            "revision",
            name="uq_expected_hours_snapshots_revision",
        ),
        Index("ix_expected_hours_snapshots_employee_period", "employee_id", "year", "month"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    employee_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="RESTRICT"),
        nullable=False,
    )
    year: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    month: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The schedules, minutes, windows, holidays and regions the figure was
    #: computed from, as they were. See `app/domain/schedule/calculation.py` for the
    #: document's shape.
    inputs: Mapped[dict] = mapped_column(JSONB, nullable=False)
    #: Who asked for it. Null means the month-end pass, which is nobody in
    #: particular — and the two must not look alike to a reader.
    computed_by_employee_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<ExpectedHoursSnapshot {self.employee_id} {self.year}-{self.month} "
            f"r{self.revision} {self.expected_minutes}m>"
        )


__all__ = [
    "DAY_CONSISTENCY_SQL",
    "HOLIDAY_SCOPES_SQL",
    "EmployeeScheduleOverride",
    "ExpectedHoursSnapshot",
    "Holiday",
    "WorkSchedule",
    "WorkScheduleDay",
]
