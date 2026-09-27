"""Attendance: an append-only event stream, and the day derived from it.

Two tables and one direction (DESIGN §3.2, D25):

* `attendance_events` is the record. It is append-only *in the database*: the
  runtime role holds INSERT and SELECT on it and nothing else (migration 0012),
  so no code path — including one added later by somebody in a hurry — can edit
  or delete a punch. A correction is a new event pointing at the one it corrects;
  the corrected row stays byte-for-byte as it was written, which is what makes the
  stream evidence rather than a table.
* `attendance_daily` is derived from it: one row per person per business date,
  written only by the derivation (`recompute_day`), never edited by hand. The two
  tables disagreeing means the snapshot is stale, never that the events moved.

**`business_date` is stored, not derived on read.** It is the Madrid calendar day
of the punch (with the cross-midnight rule applied — see
`app/domain/attendance/business_day.py`), computed once on the way in and used for
every read and every aggregation afterwards. Reading it back out of `occurred_at`
is the mistake the risk register names ("考勤业务日与 UTC 混淆"): the same instant
is two different days on the two sides of midnight, and only one of them is the
one this company recorded time against.
"""

from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
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

#: The events a person makes by clicking. A correction is deliberately absent:
#: it is appended by the correction flow (ticket 24) once an approval says so.
PUNCH_TYPES_SQL = "('clock_in', 'clock_out')"

#: The duplicate guard, as SQL. Partial, so a correction is not caught by it: two
#: corrections of two different punches may legitimately land on the same instant,
#: and a correction's identity is its chain rather than its timestamp.
PUNCH_DEDUPE_PREDICATE = "event_type <> 'correction'"


class AttendanceEvent(Base):
    """One punch, or one correction of a punch. Append-only, always."""

    __tablename__ = "attendance_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('clock_in', 'clock_out', 'correction')",
            name="ck_attendance_events_type",
        ),
        CheckConstraint("source IN ('web', 'correction')", name="ck_attendance_events_source"),
        # A correction corrects exactly one event, and nothing else carries a
        # target: the two columns are one fact, so the database states them as one.
        CheckConstraint(
            "(event_type = 'correction') = (correction_of_event_id IS NOT NULL)",
            name="ck_attendance_events_correction_target",
        ),
        CheckConstraint(
            "event_type <> 'correction' OR length(btrim(reason)) > 0",
            name="ck_attendance_events_correction_reason",
        ),
        # A row cannot correct itself: the chain would have no first element, and
        # "what did this replace" would have no answer.
        CheckConstraint(
            "correction_of_event_id IS NULL OR correction_of_event_id <> id",
            name="ck_attendance_events_self_correction",
        ),
        # The one read that matters: one person's day, in order.
        Index(
            "ix_attendance_events_employee_date",
            "employee_id",
            "business_date",
            "occurred_at",
        ),
        # The replay guard. Same employee, same kind, same instant: one row. A
        # retried request collides here and the repository reads the row back
        # instead of writing a second one — a check in the service is what a race
        # runs around, and "the button was pressed twice" must not be able to
        # become two shifts.
        Index(
            "uq_attendance_events_punch",
            "employee_id",
            "event_type",
            "occurred_at",
            unique=True,
            postgresql_where=text(PUNCH_DEDUPE_PREDICATE),
        ),
        # Corrections point at their target; the chain is read from that side.
        Index(
            "ix_attendance_events_correction_of",
            "correction_of_event_id",
            postgresql_where=text("correction_of_event_id IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    #: RESTRICT, not CASCADE: the four-year record outlives the row it points at.
    #: A cascade would let deleting an employee take their working-time record
    #: with them, which is the one thing this table exists to prevent.
    employee_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="RESTRICT"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(16), nullable=False)
    #: The instant the punch happened, in UTC. Never used for date aggregation.
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: The Madrid business day this punch counts against. Computed on write.
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 45 characters, which is the longest an IPv6 address with a zone can be.
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    #: Who wrote the row. No foreign key, for the reason `approval_requests` has
    #: none: the record has to stay readable after the person who wrote it is
    #: archived, and the who-is-this question belongs to the employee module.
    created_by_employee_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    #: The event this one corrects. The original is never touched (D25).
    correction_of_event_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("attendance_events.id", ondelete="RESTRICT"),
        nullable=True,
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<AttendanceEvent {self.employee_id} {self.event_type} {self.occurred_at}>"


class AttendanceDaily(Base):
    """A day, as the events derive it. Rebuilt, never edited."""

    __tablename__ = "attendance_daily"
    __table_args__ = (
        CheckConstraint(
            "status IN ('working', 'ok', 'missing_out', 'incomplete', 'absent', "
            "'holiday', 'non_working')",
            name="ck_attendance_daily_status",
        ),
        CheckConstraint(
            "worked_minutes IS NULL OR worked_minutes >= 0",
            name="ck_attendance_daily_worked_minutes",
        ),
        # One row per person per day, and the conflict target of the rebuild: a
        # recompute overwrites this row rather than appending a second one.
        UniqueConstraint("employee_id", "business_date", name="uq_attendance_daily_employee_date"),
        Index("ix_attendance_daily_employee_date", "employee_id", "business_date"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    employee_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="RESTRICT"),
        nullable=False,
    )
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    first_in: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_out: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Closed intervals only. A shift still running contributes nothing yet: this
    #: table is evidence, and a number that keeps growing while it is stored is a
    #: number nobody can read back with confidence.
    worked_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: The minutes the schedule expected of this day, and the schedule that said so
    #: (ticket 22). Null together, and null means no schedule reaches this person:
    #: zero is "the rules say nobody works today" and is a different answer.
    expected_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Null for the same reason: overtime is the difference against an expectation.
    overtime_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    #: The schedule this day was computed under (DESIGN §3.2). No foreign key: a
    #: snapshot that has to survive four years must not be rewritten — or made
    #: undeletable — by a later edit to the schedule it names.
    snapshot_schedule_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    recomputed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<AttendanceDaily {self.employee_id} {self.business_date} {self.status}>"


__all__ = [
    "PUNCH_DEDUPE_PREDICATE",
    "PUNCH_TYPES_SQL",
    "AttendanceDaily",
    "AttendanceEvent",
]
