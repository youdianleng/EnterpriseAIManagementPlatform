"""Persistence contract for the attendance module.

Three things about this interface are load-bearing:

* **`append_event` is the only write to the stream.** There is no update and no
  delete, and that is not an omission in this Protocol — the runtime role cannot
  perform either (migration 0012), so a method for one would be a method that
  fails in production and passes in a test that connected as the owner.
* **Reads are by business date, never by instant.** `events_for_day` takes a date
  and returns the rows that count against it, `day_records` takes a date range.
  Nothing in this interface accepts a timestamp to aggregate with, which is what
  keeps the timezone conversion in one place (`codebase-design` §2.4).
* **Nothing commits.** The service commits once, so a punch and the snapshot it
  produces land together or not at all. A day that exists without its event, or an
  event without its day, is a working-time record that disagrees with itself.

`events_for_day` and `events_by_date` return a *correction's target day* for a
correction, not the day the correction row happens to carry. The write path sets
the two to the same date, and the read does not depend on that: a correction is
part of the day it corrects, and a reader should not have to know that the writer
was careful.
"""

from datetime import date, datetime
from typing import Protocol
from uuid import UUID

from app.domain.attendance.models import AttendanceEvent, DayRecord, EventType, NewEvent


class AttendanceRepository(Protocol):
    async def employee_status(self, employee_id: UUID) -> str | None:
        """`employees.status`, or None when there is no such employee."""
        ...

    async def find_punch(
        self, employee_id: UUID, event_type: EventType, occurred_at: datetime
    ) -> AttendanceEvent | None:
        """The punch this one would duplicate, if it is already recorded.

        The unique index is the guarantee; this is how the service reads back the
        row it collided with instead of writing a second one.
        """
        ...

    async def latest_punch(self, employee_id: UUID) -> AttendanceEvent | None:
        """The most recent punch, which is what says whether a shift is open."""
        ...

    async def append_event(self, event: NewEvent) -> AttendanceEvent:
        """Append one row and return it as stored."""
        ...

    async def events_for_day(
        self, employee_id: UUID, business_date: date
    ) -> list[AttendanceEvent]:
        """Everything that counts against one day, oldest first."""
        ...

    async def events_by_date(
        self, employee_id: UUID, from_date: date, to_date: date
    ) -> dict[date, list[AttendanceEvent]]:
        """The same, for an inclusive range, grouped by the day each row counts
        against. Days with no events are absent from the mapping."""
        ...

    async def day_record(self, employee_id: UUID, business_date: date) -> DayRecord | None:
        """The stored snapshot for one day, if it has been computed."""
        ...

    async def day_records(
        self, employee_id: UUID, from_date: date, to_date: date
    ) -> list[DayRecord]:
        """The stored snapshots in an inclusive range, oldest first."""
        ...

    async def save_day(self, record: DayRecord) -> None:
        """Write the snapshot for one day, replacing whatever was there."""
        ...

    async def commit(self) -> None: ...


__all__ = ["AttendanceRepository"]
