"""What a day's events add up to.

Pure: values in, a value out, no session and no clock. `codebase-design` §5 puts
business-day attribution, DST and the arithmetic of a day in the "in-process"
column precisely so they can be tested with hand-built events, and this is that
part of the module. The service's job is to fetch the right rows, decide which day
a new punch belongs to and write the result back; what the rows *mean* is here.

**A correction replaces an instant, not a row.** The corrected event keeps its
identity, its kind and its place in the day; only the moment moves, and it moves
to the newest correction in the chain that starts at it (DESIGN D25: the original
value stays readable, and what a reader finds is a chain rather than an edit). A
correction that is itself corrected therefore resolves transitively, and a cycle —
which nothing writes, and which a hand-written row could — resolves to whatever it
has reached instead of looping forever.

**Every event list produces a day.** A day with a clock_out nobody opened, or two
clock_ins in a row, is derivable rather than fatal: it becomes `incomplete` and the
arithmetic stops at the point the shape stopped making sense. A derivation that
raised on a malformed day would leave `recompute_day` unable to repair the very
days that need repairing, and the record of a bad day would be its absence.

**Ticket 22 hands it one more input, and it stays pure.** `expected` is the
scheduling module's answer for the date — the minutes, the schedule and whether a
holiday covers it — passed in as a value rather than fetched here, so this module
still depends on nothing but its arguments and the day's arithmetic can still be
tested with hand-built days. Ticket 26's `overtime_minutes` arrives the same way: the
approved overtime for the day is a fact about another module's records, and the
derivation carries it into the snapshot without computing or capping it.
"""

from datetime import UTC, date, datetime
from uuid import UUID

from app.domain.attendance.models import (
    AttendanceEvent,
    DayRecord,
    DayStatus,
    EventType,
)
from app.domain.schedule.models import DayExpectation, ScheduleSource


def derive(
    *,
    employee_id: UUID,
    business_date: date,
    events: list[AttendanceEvent],
    today: date,
    expected: DayExpectation | None = None,
    overtime_minutes: int | None = None,
) -> DayRecord:
    """The day one person's events describe.

    `today` is passed in rather than read from a clock so that "a shift is still
    running" and "somebody forgot to clock out" — the same events, one day apart —
    are decided by the caller's calendar and the function stays pure.

    `expected` is the scheduling module's answer for this date (ticket 22), and it
    is optional: without it the derivation is exactly what ticket 21 shipped, and
    `expected_minutes` stays null rather than claiming a figure nobody agreed to.

    `overtime_minutes` is the overtime module's answer (ticket 26), passed in the same
    way and for the same reason: this function is pure, and "what did this day's
    approved overtime come to" is a fact about another module's records rather than
    about the punches. It is carried through untouched — the derivation does not
    compute, cap or compare it, because the smaller-of rule belongs to the module that
    owns the approved figure.

    **A day nobody was expected to work is not an absence.** When there are no
    punches, the expectation decides the status: a holiday, a rest day, or — only
    when the schedule actually expected work, or when there is no schedule at all —
    an absence. A Saturday that read `absent` ninety-six times a year would be a
    working-time record telling a lie about somebody, in the direction that gets
    complained about.
    """
    punches = [event for event in events if event.is_punch]
    if not punches:
        return DayRecord(
            employee_id=employee_id,
            business_date=business_date,
            status=_status_without_events(expected),
            expected_minutes=_expected_minutes(expected),
            overtime_minutes=overtime_minutes,
            snapshot_schedule_id=expected.schedule_id if expected is not None else None,
        )

    first_in: datetime | None = None
    last_out: datetime | None = None
    #: The clock_in whose shift is still open, if there is one.
    opened: datetime | None = None
    worked_minutes = 0
    unpaired = False

    for punch, instant in effective_punches(events):
        if punch.event_type is EventType.CLOCK_IN:
            if first_in is None:
                first_in = instant
            if opened is None:
                opened = instant
            else:
                # A second clock_in while a shift is open: there is no honest way
                # to tell which shift the following clock_out ends, so the day is
                # marked rather than guessed at.
                unpaired = True
            continue

        last_out = instant
        if opened is None:
            unpaired = True
            continue
        worked_minutes += int((instant - opened).total_seconds() // 60)
        opened = None

    if opened is not None:
        status = DayStatus.WORKING if business_date >= today else DayStatus.MISSING_OUT
    elif unpaired:
        status = DayStatus.INCOMPLETE
    else:
        status = DayStatus.OK

    return DayRecord(
        employee_id=employee_id,
        business_date=business_date,
        status=status,
        first_in=first_in,
        last_out=last_out,
        worked_minutes=worked_minutes,
        expected_minutes=_expected_minutes(expected),
        overtime_minutes=overtime_minutes,
        snapshot_schedule_id=expected.schedule_id if expected is not None else None,
    )


def _status_without_events(expected: DayExpectation | None) -> DayStatus:
    """What a day with no punches is, given what was expected of it.

    Three answers, in this order:

    * **No expectation at all** — no scheduling module, or nobody has configured a
      pattern that reaches this person — is `absent`: "somebody was due, or nobody
      said", which is the answer that claims the least.
    * **A holiday** is a holiday, whatever the schedule says, because the calendar
      is company-wide knowledge and does not depend on somebody having written a
      pattern for this employee.
    * **A schedule that expects nothing** is a rest day, not an absence.
    """
    if expected is None:
        return DayStatus.ABSENT
    if expected.is_holiday:
        return DayStatus.HOLIDAY
    if expected.source is ScheduleSource.NONE or expected.is_working_day:
        return DayStatus.ABSENT
    return DayStatus.NON_WORKING


def _expected_minutes(expected: DayExpectation | None) -> int | None:
    """The expectation, or null when there is none.

    Zero and null are different answers and stay different all the way to the
    client: zero is "the rules say nobody works today" — a holiday, or a rest day —
    and null is "there are no rules for this person yet". A holiday reads as zero
    even for somebody no schedule reaches, because the obligation the ticket is
    about is to say what a day was, and "nobody works on the 2nd of April" is true
    whether or not HR has written anybody's week down yet.
    """
    if expected is None:
        return None
    if expected.is_holiday:
        return 0
    if expected.source is ScheduleSource.NONE:
        return None
    return expected.expected_minutes


def effective_punches(
    events: list[AttendanceEvent],
) -> list[tuple[AttendanceEvent, datetime]]:
    """Each punch paired with the instant the day should read it at, in order.

    The order is by instant, and ties are broken by when the row was written and
    then by its id: two punches at the same second are still two punches, and a
    derivation whose order depended on the storage engine would produce two
    different snapshots for one day.

    **Every instant is converted to UTC first, and that is not cosmetic.** Python
    subtracts two aware datetimes that share a `tzinfo` *without* consulting the
    offset — `03:30` minus `01:30` on the day the clocks go forward is two hours,
    though the instants are one hour apart. Rows read back from PostgreSQL are
    already UTC and safe; a hand-built pair carrying Madrid on both sides would
    quietly produce wall-clock minutes, and the day the number is wrong on would be
    exactly the day somebody is checking it.

    Public because the anomaly scan (ticket 23) reads the same day and must read it
    the same way: an anomaly judged against uncorrected instants would contradict
    the day it is about, and a correction that fixed a late arrival would leave the
    lateness standing for ever. Ticket 24's correction flow resolves the chain
    through `chain_tip` below, which is the same walk this function makes.
    """
    corrections = corrections_of(events)
    resolved = [
        (punch, chain_tip(punch, corrections).occurred_at.astimezone(UTC))
        for punch in events
        if punch.is_punch
    ]
    resolved.sort(key=lambda item: (item[1], item[0].created_at, str(item[0].id)))
    return resolved


def corrections_of(
    events: list[AttendanceEvent],
) -> dict[UUID, list[AttendanceEvent]]:
    """The corrections pointing at each row, grouped by the row they restate.

    The stream is read from the pointing side because that is the only direction it
    has: a correction names its target and a punch knows nothing about what came
    after it, which is what leaves the original row untouched.
    """
    corrections: dict[UUID, list[AttendanceEvent]] = {}
    for event in events:
        if event.event_type is EventType.CORRECTION and event.correction_of_event_id is not None:
            corrections.setdefault(event.correction_of_event_id, []).append(event)
    return corrections


def chain_tip(
    event: AttendanceEvent, corrections: dict[UUID, list[AttendanceEvent]]
) -> AttendanceEvent:
    """The newest row in the correction chain that starts at `event`.

    The walk terminates because of the shape of the data rather than because of a
    guard: every step moves to a row that points at the one before it, and a row has
    exactly one target, so the sequence strictly approaches the punch and cannot
    revisit a row. A visited-set here would be dead code, and dead code in a
    resolver reads as "this may loop" — which would be the wrong thing to believe
    about the one function that decides what a day's numbers are.

    Public because ticket 24's flow appends its correction to *this* row rather than
    to the punch: a second correction of one punch is the continuation of the first
    on screen, and the day reads the same row this returns.
    """
    current = event
    while True:
        candidates = corrections.get(current.id)
        if not candidates:
            return current
        current = max(candidates, key=lambda item: (item.created_at, str(item.id)))


__all__ = ["chain_tip", "corrections_of", "derive", "effective_punches"]
