"""Attendance value objects: the event, and the day derived from it.

The vocabulary is small on purpose. An `AttendanceEvent` is something a person
did; a `DayRecord` is what a set of those events adds up to. Nothing here knows
about HTTP, schedules or approval — a correction is a row pointing at another row,
and the day is arithmetic over rows.

**The event types are a closed set and so are the day statuses**, because both are
read by people who are not going to check the code: a labour inspector reading a
four-year record, and a client rendering a badge. A status is not a summary of the
events; it is the answer to "was this day all right", and every member earns its
place:

* `working` — a shift is open and the day is today. The employee is at work, and
  the client counts from the open punch.
* `ok` — the day's events pair into shifts with nothing left open.
* `missing_out` — a shift is still open on a day that has ended. Same events as
  `working`, and a different fact: somebody forgot to clock out.
* `incomplete` — the events do not pair into shifts at all: a clock_out with no
  shift to close, or two clock_ins in a row. The ordinary write path refuses both,
  so a day in this state arrived through a correction or a hand-written row, and
  the record says so rather than quietly computing a number nobody should trust.
* `absent` — no events at all. Whether that is a holiday, leave or an absence is
  ticket 22's question: this module can see that nobody punched and cannot see
  why, and inventing a reason here would be the kind of guess a working-time
  record must not contain.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from uuid import UUID


class EventType(StrEnum):
    """What was recorded."""

    CLOCK_IN = "clock_in"
    CLOCK_OUT = "clock_out"
    #: Not a punch. It restates the instant of the event it points at, and the
    #: original row is left exactly as it was (DESIGN D25). Written by the
    #: correction flow (ticket 24) after an approval, never by a clock button.
    CORRECTION = "correction"


class EventSource(StrEnum):
    """How the row arrived."""

    WEB = "web"
    CORRECTION = "correction"


#: The events somebody makes by pressing a button. The set is named because
#: "is this a punch" is asked in three places — the duplicate guard, the snapshot's
#: derivation and the replay check — and a fourth spelling of it would drift.
PUNCH_EVENT_TYPES = frozenset({EventType.CLOCK_IN, EventType.CLOCK_OUT})

#: The `employees.status` value that closes a working-time record. Ticket 18 owns
#: what termination does to the rest of the system; this module only needs to know
#: that the stream of somebody who has left is history, not a punch clock.
TERMINATED_STATUS = "terminated"


class DayStatus(StrEnum):
    """The closed set of day states. See the module docstring for why each exists."""

    WORKING = "working"
    OK = "ok"
    MISSING_OUT = "missing_out"
    INCOMPLETE = "incomplete"
    ABSENT = "absent"


#: How long a shift may last. A clock_out further from the open clock_in than this
#: cannot be the end of that shift, so it does not close it: the earlier day keeps
#: its `missing_out` (which is the anomaly somebody should look at) and the new
#: punch is judged on its own. Sixteen hours is a long shift and a short night's
#: sleep — longer than any legal Spanish shift (12 hours of actual work, RD-ley
#: 8/2019's rest rules) with room for the split days the intensivo calendar
#: produces, and short enough that a forgotten clock_out is not silently closed
#: three weeks later.
MAX_SHIFT = timedelta(hours=16)

#: How far a caller's clock may be ahead of the server's before a punch is refused
#: as a future event. A client a few seconds fast is not lying about when somebody
#: arrived; a punch dated tomorrow is.
CLOCK_SKEW = timedelta(seconds=60)

#: How many days one `range_view` may ask for. Four years, which is the retention
#: the working-time obligation names — enough for a full export, and still a
#: bounded amount of work for one request.
MAX_RANGE_DAYS = 1461


def utc_now() -> datetime:
    """The default time source.

    The service takes a callable instead of calling this directly, because the
    design names the time source as a real seam: without it, "is this day over"
    and the DST transition tests cannot be decided deterministically, and a test
    that waits for midnight is not a test.
    """
    return datetime.now(UTC)


@dataclass(slots=True, frozen=True)
class AttendanceEvent:
    """One row of the stream, as stored. Never updated, never deleted."""

    id: UUID
    employee_id: UUID
    event_type: EventType
    #: The instant it happened, in UTC. Used for arithmetic *within* a day and for
    #: nothing that aggregates by day.
    occurred_at: datetime
    #: The Madrid business day it counts against, computed on write.
    business_date: date
    source: EventSource
    created_at: datetime
    ip_address: str | None = None
    created_by_employee_id: UUID | None = None
    correction_of_event_id: UUID | None = None
    reason: str | None = None

    @property
    def is_punch(self) -> bool:
        return self.event_type in PUNCH_EVENT_TYPES


@dataclass(slots=True, frozen=True)
class NewEvent:
    """An event about to be appended. The stored row comes back as `AttendanceEvent`."""

    employee_id: UUID
    event_type: EventType
    occurred_at: datetime
    business_date: date
    source: EventSource
    ip_address: str | None = None
    created_by_employee_id: UUID | None = None
    correction_of_event_id: UUID | None = None
    reason: str | None = None


@dataclass(slots=True, frozen=True)
class DayRecord:
    """A day, as the events derive it.

    `recomputed_at` is `None` for a record that was derived to answer a read and
    never stored — a day in the middle of a range nobody has worked, or a day
    whose snapshot has not been written because no event ever arrived for it. The
    distinction is visible on purpose: the client can tell a stored snapshot from
    an answer computed just now, and nobody has to wonder which one they got.

    `first_in` and `last_out` are literally that — the first clock_in and the last
    clock_out of the day. On a day with a single shift (the ordinary case) they
    frame it, and on a day with two they still describe the day rather than either
    shift. `worked_minutes` counts **closed** intervals only: a shift that is still
    running has contributed nothing yet, which is why `working` is a status and not
    a number that keeps moving after it has been written down.
    """

    employee_id: UUID
    business_date: date
    status: DayStatus
    first_in: datetime | None = None
    last_out: datetime | None = None
    worked_minutes: int = 0
    #: Null until ticket 22 puts a schedule behind the department. Frozen into the
    #: snapshot when it arrives, so a reader four years from now sees what the day
    #: was measured against rather than what it would be measured against today.
    expected_minutes: int | None = None
    overtime_minutes: int | None = None
    snapshot_schedule_id: UUID | None = None
    recomputed_at: datetime | None = None

    @property
    def is_open(self) -> bool:
        """Whether a shift is still running. The UI's "counting" state."""
        return self.status is DayStatus.WORKING

    @property
    def has_events(self) -> bool:
        return self.status is not DayStatus.ABSENT


#: The service's time source. Typed here so a caller reading the constructor knows
#: what it is being handed.
TimeSource = Callable[[], datetime]


__all__ = [
    "CLOCK_SKEW",
    "MAX_RANGE_DAYS",
    "MAX_SHIFT",
    "PUNCH_EVENT_TYPES",
    "TERMINATED_STATUS",
    "AttendanceEvent",
    "DayRecord",
    "DayStatus",
    "EventSource",
    "EventType",
    "NewEvent",
    "TimeSource",
    "utc_now",
]
