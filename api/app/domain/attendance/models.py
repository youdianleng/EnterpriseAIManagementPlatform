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

* `absent` — no events at all, and the schedule expected work. Whether that is leave
  or an unexplained absence is the leave module's question; what this module can
  say is that somebody was expected and did not come.
* `holiday` — no events, and a holiday row is why. The day was not expected of
  anybody, so calling it an absence would be a working-time record asserting
  something false about a person.
* `non_working` — no events, and the schedule expects nothing on that weekday: a
  Saturday, or a day HR has configured as a rest day. Same reasoning as `holiday`,
  and a different fact — which is why it is a different status rather than one
  "nothing was expected" member.
* `working` — a shift is open and the day is today. The employee is at work, and
  the client counts from the open punch.
* `ok` — the day's events pair into shifts with nothing left open.
* `missing_out` — a shift is still open on a day that has ended. Same events as
  `working`, and a different fact: somebody forgot to clock out.
* `incomplete` — the events do not pair into shifts at all: a clock_out with no
  shift to close, or two clock_ins in a row. The ordinary write path refuses both,
  so a day in this state arrived through a correction or a hand-written row, and
  the record says so rather than quietly computing a number nobody should trust.

**Ticket 22 added the three schedule-derived members**, and the order of the three
paragraphs above is the rule: an expectation of zero minutes is never an absence,
and which kind of zero it was is what the record has to say. Before a schedule
exists for somebody, `absent` keeps its ticket-21 meaning — nobody worked, and this
module cannot see why — because `expected_minutes` is null and the honest answer is
the one that claims the least.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from app.domain.schedule.models import DayExpectation


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
    #: Nobody was expected: a holiday (ticket 22's calendar).
    HOLIDAY = "holiday"
    #: Nobody was expected: a weekday the schedule does not work.
    NON_WORKING = "non_working"


#: The statuses that mean "this day was not expected of anybody", and therefore
#: that `absent` would be a false statement. Named as a set because the derivation
#: asks the question once and a second spelling of it would drift.
NOT_EXPECTED_STATUSES = frozenset({DayStatus.HOLIDAY, DayStatus.NON_WORKING})


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
    #: What the schedule expected of this day, in minutes. Null means no schedule
    #: reaches this person — which is not the same as zero, and the difference is
    #: the whole reason ticket 21 could leave the column empty without lying.
    expected_minutes: int | None = None
    overtime_minutes: int | None = None
    #: The schedule the expectation came from, frozen with it (DESIGN §3.2, §8.1):
    #: four years from now the question is "what was this day measured against",
    #: and the schedule table will by then say something else.
    snapshot_schedule_id: UUID | None = None
    recomputed_at: datetime | None = None

    @property
    def is_open(self) -> bool:
        """Whether a shift is still running. The UI's "counting" state."""
        return self.status is DayStatus.WORKING

    @property
    def has_events(self) -> bool:
        return self.status is not DayStatus.ABSENT

    @property
    def was_expected(self) -> bool:
        """Whether anybody was due. False for a holiday and for a rest day."""
        return self.status not in NOT_EXPECTED_STATUSES


#: The service's time source. Typed here so a caller reading the constructor knows
#: what it is being handed.
TimeSource = Callable[[], datetime]


class ExpectationSource(Protocol):
    """What the attendance module asks the scheduling module, and nothing else.

    A Protocol rather than the concrete `ScheduleService` so the dependency is
    stated as the two questions this module has — "what does one day expect" and
    "what does this range expect" — instead of as the whole scheduling surface.
    Ticket 21's tests build an `AttendanceService` with no source at all and keep
    working: a day then has no expectation, which is exactly what the column said
    before ticket 22 existed.
    """

    async def day_expectation(self, employee_id: UUID, business_date: date) -> DayExpectation:
        """The rules for one of somebody's days."""
        ...

    async def day_expectations(
        self, employee_id: UUID, from_date: date, to_date: date
    ) -> dict[date, DayExpectation]:
        """The same for an inclusive range, grouped by date."""
        ...


class OvertimeSource(Protocol):
    """What the attendance module asks the overtime module, and nothing else.

    Ticket 26's half of `attendance_daily.overtime_minutes`, built the way ticket 22
    built `expected_minutes`: the derivation is handed a *value* for the day, and this
    is the seam that produces it. The figure is the overtime module's own answer — what
    HR confirmed, else the settled smaller of the approved and the worked minutes, else
    what was approved — and it is deliberately not re-derived here: a second
    implementation of "取较小值" would eventually disagree with the record the monthly
    export is built from.

    Optional, and that is why the pair exists: an `AttendanceService` built without a
    source leaves `overtime_minutes` null, which is exactly what the column held before
    ticket 26 and what ticket 21's tests still assert.
    """

    async def overtime_minutes(self, employee_id: UUID, business_date: date) -> int | None:
        """One day's approved overtime, or nothing when none was approved."""
        ...

    async def overtime_by_date(
        self, employee_id: UUID, from_date: date, to_date: date
    ) -> dict[date, int]:
        """The same for an inclusive range, grouped by date and omitting empty days."""
        ...


__all__ = [
    "CLOCK_SKEW",
    "MAX_RANGE_DAYS",
    "MAX_SHIFT",
    "NOT_EXPECTED_STATUSES",
    "PUNCH_EVENT_TYPES",
    "TERMINATED_STATUS",
    "AttendanceEvent",
    "DayRecord",
    "DayStatus",
    "EventSource",
    "EventType",
    "ExpectationSource",
    "NewEvent",
    "OvertimeSource",
    "TimeSource",
    "utc_now",
]
