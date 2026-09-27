"""The five ways a day can be wrong, and the arithmetic that decides them.

Pure: values in, values out, no session and no clock — the same split as
`derivation.py`, and for the same reason. The scan's job is to fetch the day's
events and its expectation; what those two *mean* together is here, so the rules
can be tested with hand-built days and a date nobody has to wait for.

**Each member is a different fact, and they are not a scale:**

* `no_punches` — the schedule expected work and the stream has nothing at all.
  Deliberately not two anomalies: "no clock_in and no clock_out" is one thing that
  happened to somebody, and reporting it as two would double every absence in every
  count that reads this table. Which punch is missing is unknowable when neither
  exists.
* `missing_clock_in` — there are punches and none of them opens a shift: a
  clock_out with nothing to close. The write path refuses one, so a day in this
  state arrived through a correction, an offline punch that outran its partner, or a
  hand-written row.
* `missing_clock_out` — a shift was opened and never closed. The same events as
  `working`, and a different fact: the day is over and somebody forgot.
* `late` — the first clock_in is later than the day's window says, by more than
  `PUNCH_TOLERANCE`.
* `early_leave` — the last clock_out is earlier than the window says, by more than
  the same tolerance.

**The tolerance is the ticket's real decision**, and it is one constant for both
directions: a punch clock is a door people queue at, and a minute or two either side
of a start time is not a fact about anybody. Five minutes is short enough that a
genuine lateness is never missed and long enough that a clock two minutes fast does
not generate one. Without it every schedule would produce an anomaly per employee
per day, and a table that flags everybody tells nobody anything.

**Late and early are judged against the day's window, in Madrid wall-clock
minutes**, because that is what "arrived at 09:07" means: a schedule states a local
window, and a punch is an instant. A punch whose Madrid date is not the business
date it counts against is not measured against the window at all — the window
describes a day, and that punch is not in it.

**A day nobody was expected to work has nothing wrong with it.** A holiday, a rest
day, and a day covered by approved leave all produce the empty list; the check is
here rather than in the caller so that no caller can decide on its own that an
absence is worth reporting. Ticket 25 fills the leave half of it.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from uuid import UUID

from app.core.errors import ErrorCode
from app.domain.attendance.business_day import MADRID
from app.domain.attendance.derivation import effective_punches
from app.domain.attendance.models import AttendanceEvent, EventType
from app.domain.errors import DomainError
from app.domain.schedule.models import DayExpectation

#: How far a punch may be from the window's edge before it is an anomaly. See the
#: module docstring: one constant for both directions, because "a few minutes is not
#: a fact about anybody" is one rule rather than two.
PUNCH_TOLERANCE = timedelta(minutes=5)


class AnomalyType(StrEnum):
    """What is wrong with a day. A closed set, and every member is a judgement."""

    MISSING_CLOCK_OUT = "missing_clock_out"
    MISSING_CLOCK_IN = "missing_clock_in"
    LATE = "late"
    EARLY_LEAVE = "early_leave"
    NO_PUNCHES = "no_punches"


#: The order anomalies are reported in a day's answer. The enum's own order, named
#: once so a client grouping by type and a test comparing lists agree.
ANOMALY_ORDER: tuple[AnomalyType, ...] = tuple(AnomalyType)


@dataclass(slots=True, frozen=True)
class Anomaly:
    """One row of `attendance_anomalies`, as stored."""

    id: UUID
    employee_id: UUID
    business_date: date
    type: AnomalyType
    detected_at: datetime
    notified_at: datetime | None = None
    resolved_by_event_id: UUID | None = None

    @property
    def is_resolved(self) -> bool:
        """The whole rule: an anomaly with a resolving event is resolved.

        Not a second flag beside the column, because two answers to one question
        eventually disagree — and the one that would go stale is the flag.
        """
        return self.resolved_by_event_id is not None

    @property
    def was_notified(self) -> bool:
        return self.notified_at is not None


@dataclass(slots=True, frozen=True)
class NewAnomaly:
    """An anomaly the scan has just decided, before it has an id or a timestamp."""

    employee_id: UUID
    business_date: date
    type: AnomalyType


@dataclass(slots=True, frozen=True)
class AnomalyFailure:
    """One employee whose day could not be examined, and why.

    Carried rather than raised, for the reason the personnel applier gives: one
    person with data nobody can read must not stop the other ninety-nine from being
    checked, and a pass that raised would be a scheduler reporting failure for ever.
    """

    employee_id: UUID
    code: str
    detail: str


def failure_of(employee_id: UUID, error: Exception) -> AnomalyFailure:
    """One employee's failure, in the vocabulary the rest of the system uses.

    `DomainError` is the only exception carrying a catalogued code; anything else is
    an internal error with a message somebody will read in a log rather than in a
    response.
    """
    code = (
        error.code.value if isinstance(error, DomainError) else ErrorCode.INTERNAL_ERROR.value
    )
    return AnomalyFailure(employee_id=employee_id, code=code, detail=str(error))


@dataclass(slots=True, frozen=True)
class AnomalyScanReport:
    """What one night's pass found.

    `examined` counts the people the schedule expected work from — the population
    the pass actually had a question about — so "nothing was created" is
    distinguishable from "nobody was looked at".
    """

    business_date: date
    examined: int = 0
    created: tuple[Anomaly, ...] = ()
    existing: int = 0
    failed: tuple[AnomalyFailure, ...] = ()

    @property
    def created_count(self) -> int:
        return len(self.created)


@dataclass(slots=True, frozen=True)
class AnomalyReminderReport:
    """What the morning pass told people about.

    `reminded` are the rows it raised a notification for and stamped; `duplicates`
    are the ones whose notification already existed (a crash between the raise and
    the stamp, most likely), which are stamped too because the employee has been
    told either way.
    """

    business_date: date
    reminded: tuple[Anomaly, ...] = ()
    duplicates: int = 0
    failed: tuple[AnomalyFailure, ...] = ()

    @property
    def reminded_count(self) -> int:
        return len(self.reminded)


def detect(
    *,
    employee_id: UUID,
    business_date: date,
    events: list[AttendanceEvent],
    expected: DayExpectation | None,
    on_leave: bool = False,
    tolerance: timedelta = PUNCH_TOLERANCE,
) -> list[NewAnomaly]:
    """Every anomaly this day has, in `ANOMALY_ORDER`.

    `on_leave` is the leave module's answer, passed in as a value rather than asked
    for here, so this function stays pure and ticket 25 decides what leave is. It
    suppresses the whole day rather than only the missing-punch kinds: an approved
    day off is not measured against a window either.

    `expected` being `None` means no schedule reaches this person, which is not a
    day with everything wrong — it is a day this module has no question about. The
    same for zero minutes: a holiday and a rest day are both "nobody was due".
    """
    if on_leave or expected is None or not expected.is_working_day:
        return []

    punches = effective_punches(events)
    if not punches:
        return [_anomaly(employee_id, business_date, AnomalyType.NO_PUNCHES)]

    found: set[AnomalyType] = set()
    #: The clock_in whose shift is still open, if there is one.
    opened: datetime | None = None
    first_in: datetime | None = None
    last_out: datetime | None = None

    for punch, instant in punches:
        if punch.event_type is EventType.CLOCK_IN:
            if first_in is None:
                first_in = instant
            # A second clock_in while one is open is not a second anomaly: the
            # first is unclosed either way, which is what `missing_clock_out`
            # reports, and the derivation marks the day `incomplete` for the pair.
            if opened is None:
                opened = instant
            continue

        last_out = instant
        if opened is None:
            found.add(AnomalyType.MISSING_CLOCK_IN)
        else:
            opened = None

    if opened is not None:
        found.add(AnomalyType.MISSING_CLOCK_OUT)

    window = expected.schedule_day
    if window is not None and window.start_time and window.end_time:
        if _after(first_in, business_date, window.start_time, tolerance):
            found.add(AnomalyType.LATE)
        if _before(last_out, business_date, window.end_time, tolerance):
            found.add(AnomalyType.EARLY_LEAVE)

    return [
        _anomaly(employee_id, business_date, kind)
        for kind in ANOMALY_ORDER
        if kind in found
    ]


def _anomaly(employee_id: UUID, business_date: date, kind: AnomalyType) -> NewAnomaly:
    return NewAnomaly(employee_id=employee_id, business_date=business_date, type=kind)


def _after(
    instant: datetime | None, business_date: date, edge: time, tolerance: timedelta
) -> bool:
    """Whether a punch is later than the window's start, the tolerance allowed."""
    minutes = _local_minutes(instant, business_date)
    return minutes is not None and minutes > _minutes(edge) + _tolerance(tolerance)


def _before(
    instant: datetime | None, business_date: date, edge: time, tolerance: timedelta
) -> bool:
    """Whether a punch is earlier than the window's end, the tolerance allowed."""
    minutes = _local_minutes(instant, business_date)
    return minutes is not None and minutes < _minutes(edge) - _tolerance(tolerance)


def _local_minutes(instant: datetime | None, business_date: date) -> int | None:
    """A punch's minute of the day, Madrid-side, or None when it is another day's.

    The None is the cross-midnight rule seen from this side: a clock_out at 06:00
    that closes a shift begun the evening before counts against the previous
    business date, and comparing its wall clock to *this* day's window would invent
    an early leave. The day it belongs to gets to judge it.
    """
    if instant is None:
        return None
    local = instant.astimezone(MADRID)
    if local.date() != business_date:
        return None
    return local.hour * 60 + local.minute


def _minutes(edge: time) -> int:
    return edge.hour * 60 + edge.minute


def _tolerance(tolerance: timedelta) -> int:
    """Whole minutes, because a window's edges are whole minutes (`validate_day`).

    Truncated rather than rounded so a tolerance of 90 seconds is a tolerance of
    one minute: the alternative moves an edge by a minute nobody asked for.
    """
    return int(tolerance.total_seconds() // 60)


__all__ = [
    "ANOMALY_ORDER",
    "PUNCH_TOLERANCE",
    "Anomaly",
    "AnomalyFailure",
    "AnomalyReminderReport",
    "AnomalyScanReport",
    "AnomalyType",
    "NewAnomaly",
    "detect",
    "failure_of",
]
