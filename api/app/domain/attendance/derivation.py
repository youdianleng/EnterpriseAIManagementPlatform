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
"""

from datetime import UTC, date, datetime
from uuid import UUID

from app.domain.attendance.models import (
    AttendanceEvent,
    DayRecord,
    DayStatus,
    EventType,
)


def derive(
    *,
    employee_id: UUID,
    business_date: date,
    events: list[AttendanceEvent],
    today: date,
) -> DayRecord:
    """The day one person's events describe.

    `today` is passed in rather than read from a clock so that "a shift is still
    running" and "somebody forgot to clock out" — the same events, one day apart —
    are decided by the caller's calendar and the function stays pure.
    """
    punches = [event for event in events if event.is_punch]
    if not punches:
        return DayRecord(
            employee_id=employee_id,
            business_date=business_date,
            status=DayStatus.ABSENT,
        )

    first_in: datetime | None = None
    last_out: datetime | None = None
    #: The clock_in whose shift is still open, if there is one.
    opened: datetime | None = None
    worked_minutes = 0
    unpaired = False

    for punch, instant in _with_effective_instants(punches, events):
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
    )


def _with_effective_instants(
    punches: list[AttendanceEvent], events: list[AttendanceEvent]
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
    """
    corrections: dict[UUID, list[AttendanceEvent]] = {}
    for event in events:
        if event.event_type is EventType.CORRECTION and event.correction_of_event_id is not None:
            corrections.setdefault(event.correction_of_event_id, []).append(event)

    resolved = [
        (punch, _corrected_instant(punch, corrections).astimezone(UTC)) for punch in punches
    ]
    resolved.sort(key=lambda item: (item[1], item[0].created_at, str(item[0].id)))
    return resolved


def _corrected_instant(
    event: AttendanceEvent, corrections: dict[UUID, list[AttendanceEvent]]
) -> datetime:
    """Follow the correction chain from `event` to its newest correction.

    The walk terminates because of the shape of the data rather than because of a
    guard: every step moves to a row that points at the one before it, and a row has
    exactly one target, so the sequence strictly approaches the punch and cannot
    revisit a row. A visited-set here would be dead code, and dead code in a
    resolver reads as "this may loop" — which would be the wrong thing to believe
    about the one function that decides what a day's numbers are.
    """
    current = event
    while True:
        candidates = corrections.get(current.id)
        if not candidates:
            return current.occurred_at
        current = max(candidates, key=lambda item: (item.created_at, str(item.id)))


__all__ = ["derive"]
