"""Attendance: the append-only stream, the Madrid business day, and the day derived.

No mocks and no in-memory repository. Half of what this module has to get right is
a *query* — which punch is the open one, which events count against a business
date, whether a row can be rewritten by the role requests actually connect as — and
a substitute would answer those with the test's own assumptions.
`tests/support/platform.py` supplies committed employees and accounts, so the
service runs on its own session the way a request does, and the endpoint tests go
through a real login and the restricted database role.

**The pure half is tested purely.** Business-day attribution, the two DST
transitions and the arithmetic of a day are values-in/values-out, and
`codebase-design` §5 puts exactly those in the "in-process, fixed clock, no
database" column. They are at the top of this file and need no fixtures at all.

**The DST dates are real.** 2026's transitions are 29 March and 25 October, and the
tests assert their offsets from `ZoneInfo` rather than from a number written down
here: the point is that the tz database says so, so an expectation computed by hand
would prove nothing. Those instants are pinned to a `now` after both transitions,
because a test whose result depends on the day it happens to be run is not
evidence.
"""

import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.core.errors import ErrorCode
from app.domain.access.kernel import Reason, Resource, ResourceKind, can
from app.domain.access.permissions import Action
from app.domain.access.principal import SYSTEM_ROLES, Principal
from app.domain.attendance.business_day import (
    MADRID,
    business_date_of,
    dates_between,
    madrid_today,
)
from app.domain.attendance.derivation import derive
from app.domain.attendance.errors import AttendanceErrorCode
from app.domain.attendance.models import (
    MAX_RANGE_DAYS,
    MAX_SHIFT,
    PUNCH_EVENT_TYPES,
    AttendanceEvent,
    DayRecord,
    DayStatus,
    EventSource,
    EventType,
    NewEvent,
    utc_now,
)
from app.domain.attendance.service import AttendanceService
from app.domain.errors import DomainError
from app.repositories.attendance import PostgresAttendanceRepository
from tests.support.platform import Platform

#: The two ends of the year as the tz database has them: the last Sunday in March
#: and the last Sunday in October.
SPRING_FORWARD = date(2026, 3, 29)
FALL_BACK = date(2026, 10, 25)

#: A Monday, so a shift across midnight runs into a working day rather than into a
#: weekend where "nobody worked either day" would explain the same result.
NIGHT_SHIFT_MONDAY = date(2026, 9, 21)

#: The `now` the DST tests run at: after both transitions, so "is this day over" and
#: "is this instant in the future" have the same answer whenever the suite runs.
AFTER_BOTH_TRANSITIONS = datetime(2026, 11, 2, 12, 0, tzinfo=MADRID)


def at(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """An instant as somebody in Madrid would say it, through `ZoneInfo`.

    Never `timezone(timedelta(hours=...))`: the point of the DST tests is that the
    offset is read from the tz database, and a test that hardcoded one would pass
    against a module that hardcoded one too.
    """
    return datetime(year, month, day, hour, minute, tzinfo=MADRID)


def event(
    *,
    employee_id: UUID,
    event_type: EventType,
    occurred_at: datetime,
    business_date: date,
    created_at: datetime | None = None,
    correction_of_event_id: UUID | None = None,
) -> AttendanceEvent:
    """A hand-built row, for the pure tests. The database tests write real ones."""
    return AttendanceEvent(
        id=uuid4(),
        employee_id=employee_id,
        event_type=event_type,
        occurred_at=occurred_at,
        business_date=business_date,
        source=EventSource.CORRECTION if event_type is EventType.CORRECTION else EventSource.WEB,
        created_at=created_at or occurred_at,
        correction_of_event_id=correction_of_event_id,
        reason="a correction" if correction_of_event_id else None,
    )


def punch_event(
    employee_id: UUID, kind: EventType, instant: datetime, day: date
) -> AttendanceEvent:
    return event(employee_id=employee_id, event_type=kind, occurred_at=instant, business_date=day)


@asynccontextmanager
async def attendance(
    platform: Platform, *, now: datetime | None = None
) -> AsyncIterator[AttendanceService]:
    """The service on its own session, the way one request uses it.

    `now` is the seam `codebase-design` §4 names for the time source: the DST tests
    pin it, everything else uses the real clock.
    """
    async with platform.factory() as session:
        clock: Callable[[], datetime] = utc_now if now is None else (lambda: now)
        yield AttendanceService(PostgresAttendanceRepository(session), now=clock)


async def punch(
    platform: Platform,
    employee_id: UUID,
    kind: EventType,
    instant: datetime,
    *,
    now: datetime | None = None,
) -> AttendanceEvent:
    async with attendance(platform, now=now) as service:
        return await service.clock(employee_id, kind, instant, EventSource.WEB)


async def read_day(
    platform: Platform, employee_id: UUID, business_date: date, *, now: datetime | None = None
) -> DayRecord:
    async with attendance(platform, now=now) as service:
        return await service.day_view(employee_id, business_date)


async def worked(
    platform: Platform, employee_id: UUID, day: date, start: int, end: int
) -> None:
    """A complete shift on one day, through the real write path."""
    await punch(platform, employee_id, EventType.CLOCK_IN, at(day.year, day.month, day.day, start))
    await punch(platform, employee_id, EventType.CLOCK_OUT, at(day.year, day.month, day.day, end))


async def new_employee(platform: Platform) -> UUID:
    return UUID(await platform.employee())


def own_resource(employee_id: UUID) -> Resource:
    return Resource(ResourceKind.EMPLOYEE, owner_employee_id=employee_id)


# --- the Madrid business day, purely ----------------------------------------


def test_the_business_day_functions_refuse_a_naive_instant() -> None:
    """A naive instant has already lost the information this module applies, and
    every offset available to guess with is wrong some of the time."""
    with pytest.raises(ValueError):
        business_date_of(datetime(2026, 9, 21, 23, 30))


def test_the_madrid_day_is_not_the_utc_day() -> None:
    """The risk the design register names, as an assertion.

    23:30 in Madrid is 21:30 UTC in summer, and 00:30 in Madrid is *the previous
    day* in UTC. A module that sliced `occurred_at` would file the second punch of
    every night shift under the wrong date, every single night.
    """
    assert business_date_of(at(2026, 7, 15, 23, 30)) == date(2026, 7, 15)
    # The same instant, two dates: 00:30 Madrid is 22:30 UTC of the day before.
    assert at(2026, 7, 16, 0, 30).astimezone(UTC).date() == date(2026, 7, 15)
    assert business_date_of(at(2026, 7, 16, 0, 30)) == date(2026, 7, 16)
    # And in winter, where the offset is +01:00 rather than +02:00.
    assert at(2026, 1, 16, 0, 30).astimezone(UTC).date() == date(2026, 1, 15)
    assert business_date_of(at(2026, 1, 16, 0, 30)) == date(2026, 1, 16)


def test_madrid_has_dst_and_the_module_reads_it_from_the_tz_database() -> None:
    """A fixed offset would pass the test above in one season and fail in the other.

    The offsets below are the tz database's, asserted rather than assumed: winter
    is +01:00, summer is +02:00, and 2026 changes over on 29 March and 25 October.
    """
    assert isinstance(MADRID, ZoneInfo), "a fixed-offset timezone would be wrong half the year"
    assert at(2026, 1, 15, 12).utcoffset() == timedelta(hours=1)
    assert at(2026, 7, 15, 12).utcoffset() == timedelta(hours=2)

    # The day before each transition is on the old offset; the day itself is on the
    # new one. No arithmetic in this repository decides which is which.
    assert at(2026, 3, 28, 12).utcoffset() == timedelta(hours=1)
    assert at(2026, 3, 29, 12).utcoffset() == timedelta(hours=2)
    assert at(2026, 10, 24, 12).utcoffset() == timedelta(hours=2)
    assert at(2026, 10, 25, 12).utcoffset() == timedelta(hours=1)
    assert MADRID.dst(at(2026, 3, 29, 12)) == timedelta(hours=1)
    assert MADRID.dst(at(2026, 10, 25, 12)) == timedelta(0)

    # The hours on either side of the spring gap are the same calendar day, and the
    # hour that does not exist is skipped rather than landing on the 28th.
    assert business_date_of(at(2026, 3, 29, 1, 30)) == SPRING_FORWARD
    assert business_date_of(at(2026, 3, 29, 3, 30)) == SPRING_FORWARD
    # 02:30 happened twice on 25 October 2026, an hour apart. Both are that day.
    ambiguous = datetime(2026, 10, 25, 2, 30, tzinfo=MADRID)
    assert ambiguous.replace(fold=0).utcoffset() == timedelta(hours=2)
    assert ambiguous.replace(fold=1).utcoffset() == timedelta(hours=1)
    assert business_date_of(ambiguous.replace(fold=0)) == FALL_BACK
    assert business_date_of(ambiguous.replace(fold=1)) == FALL_BACK


def test_madrid_today_answers_with_the_business_calendar() -> None:
    """A caller in another timezone asking for "today" gets Madrid's day, which is
    why the API's default comes from here rather than from the browser."""
    assert madrid_today(at(2026, 9, 21, 23, 45)) == date(2026, 9, 21)
    # 23:45 UTC on the 21st is already the 22nd in Madrid.
    assert madrid_today(datetime(2026, 9, 21, 23, 45, tzinfo=UTC)) == date(2026, 9, 22)


def test_dates_between_is_inclusive_and_ordered() -> None:
    assert dates_between(date(2026, 6, 1), date(2026, 6, 3)) == [
        date(2026, 6, 1),
        date(2026, 6, 2),
        date(2026, 6, 3),
    ]
    assert dates_between(date(2026, 6, 1), date(2026, 6, 1)) == [date(2026, 6, 1)]
    assert dates_between(date(2026, 6, 3), date(2026, 6, 1)) == []


# --- what a day's events add up to, purely ----------------------------------


def test_a_day_with_no_punches_is_absent() -> None:
    record = derive(
        employee_id=uuid4(), business_date=date(2026, 6, 1), events=[], today=date(2026, 6, 2)
    )

    assert record.status is DayStatus.ABSENT
    assert record.worked_minutes == 0
    assert (record.first_in, record.last_out) == (None, None)
    assert not record.has_events


def test_a_paired_day_adds_up_its_shifts() -> None:
    """Two shifts in one day: the minutes are the sum, and first/last frame the day."""
    employee_id = uuid4()
    day = date(2026, 6, 1)
    events = [
        punch_event(employee_id, EventType.CLOCK_IN, at(2026, 6, 1, 8), day),
        punch_event(employee_id, EventType.CLOCK_OUT, at(2026, 6, 1, 12), day),
        punch_event(employee_id, EventType.CLOCK_IN, at(2026, 6, 1, 13), day),
        punch_event(employee_id, EventType.CLOCK_OUT, at(2026, 6, 1, 17, 30), day),
    ]

    record = derive(
        employee_id=employee_id, business_date=day, events=events, today=date(2026, 6, 2)
    )

    assert record.status is DayStatus.OK
    assert record.worked_minutes == 240 + 270
    assert record.first_in == at(2026, 6, 1, 8)
    assert record.last_out == at(2026, 6, 1, 17, 30)


def test_an_open_shift_is_working_today_and_missing_out_once_the_day_is_over() -> None:
    """Same events, two facts, and the difference is the calendar rather than the
    data: one is somebody at their desk, the other is a punch nobody closed."""
    employee_id = uuid4()
    day = date(2026, 6, 1)
    events = [punch_event(employee_id, EventType.CLOCK_IN, at(2026, 6, 1, 8), day)]

    working = derive(employee_id=employee_id, business_date=day, events=events, today=day)
    missing = derive(
        employee_id=employee_id, business_date=day, events=events, today=date(2026, 6, 2)
    )

    assert working.status is DayStatus.WORKING
    assert working.is_open
    assert missing.status is DayStatus.MISSING_OUT
    assert not missing.is_open
    # A running shift contributes nothing yet: this number is evidence, not a timer.
    assert (working.worked_minutes, missing.worked_minutes) == (0, 0)


def test_a_day_whose_events_do_not_pair_is_incomplete() -> None:
    """An orphan clock_out, and a second clock_in while a shift is open.

    Neither is reachable through `clock`, which refuses both; both are reachable
    through a correction or a hand-written row, and the derivation has to answer
    for them rather than raise — a day that cannot be derived is a day that cannot
    be repaired.
    """
    employee_id = uuid4()
    day = date(2026, 6, 1)
    orphan = derive(
        employee_id=employee_id,
        business_date=day,
        events=[punch_event(employee_id, EventType.CLOCK_OUT, at(2026, 6, 1, 17), day)],
        today=date(2026, 6, 2),
    )
    doubled = derive(
        employee_id=employee_id,
        business_date=day,
        events=[
            punch_event(employee_id, EventType.CLOCK_IN, at(2026, 6, 1, 8), day),
            punch_event(employee_id, EventType.CLOCK_IN, at(2026, 6, 1, 9), day),
            punch_event(employee_id, EventType.CLOCK_OUT, at(2026, 6, 1, 17), day),
        ],
        today=date(2026, 6, 2),
    )

    assert orphan.status is DayStatus.INCOMPLETE
    assert (orphan.first_in, orphan.last_out) == (None, at(2026, 6, 1, 17))
    assert doubled.status is DayStatus.INCOMPLETE
    assert doubled.has_events


def test_a_correction_moves_the_instant_and_leaves_the_original_readable() -> None:
    """D25's whole content: the event keeps its identity and its kind, and only the
    moment changes."""
    employee_id = uuid4()
    day = date(2026, 6, 1)
    out = punch_event(employee_id, EventType.CLOCK_OUT, at(2026, 6, 1, 16), day)
    events = [
        punch_event(employee_id, EventType.CLOCK_IN, at(2026, 6, 1, 8), day),
        out,
        event(
            employee_id=employee_id,
            event_type=EventType.CORRECTION,
            occurred_at=at(2026, 6, 1, 18),
            business_date=day,
            correction_of_event_id=out.id,
            created_at=at(2026, 6, 2, 9),
        ),
    ]

    record = derive(
        employee_id=employee_id, business_date=day, events=events, today=date(2026, 6, 2)
    )

    assert record.worked_minutes == 600
    assert record.last_out == at(2026, 6, 1, 18)
    assert out.occurred_at == at(2026, 6, 1, 16), "the original value is still there"


def test_the_newest_correction_of_one_punch_wins() -> None:
    """Two corrections of the same punch is what a day corrected twice looks like."""
    employee_id = uuid4()
    day = date(2026, 6, 1)
    out = punch_event(employee_id, EventType.CLOCK_OUT, at(2026, 6, 1, 16), day)
    first = event(
        employee_id=employee_id,
        event_type=EventType.CORRECTION,
        occurred_at=at(2026, 6, 1, 17),
        business_date=day,
        correction_of_event_id=out.id,
        created_at=at(2026, 6, 2, 9),
    )
    second = event(
        employee_id=employee_id,
        event_type=EventType.CORRECTION,
        occurred_at=at(2026, 6, 1, 19),
        business_date=day,
        correction_of_event_id=out.id,
        created_at=at(2026, 6, 3, 9),
    )

    record = derive(
        employee_id=employee_id,
        business_date=day,
        events=[
            punch_event(employee_id, EventType.CLOCK_IN, at(2026, 6, 1, 8), day),
            out,
            first,
            second,
        ],
        today=date(2026, 6, 2),
    )

    assert record.last_out == at(2026, 6, 1, 19)


def test_a_chain_of_corrections_resolves_to_its_newest_row() -> None:
    """A correction that is itself corrected.

    The walk is what terminates it: each step moves to the row that points at the
    one before, and a row has exactly one target, so the sequence strictly
    approaches the punch and can never revisit a row. That argument is the reason
    there is no visited-set here — and this test is what would fail if a future
    version followed targets in the other direction.
    """
    employee_id = uuid4()
    day = date(2026, 6, 1)
    out = punch_event(employee_id, EventType.CLOCK_OUT, at(2026, 6, 1, 16), day)
    first = event(
        employee_id=employee_id,
        event_type=EventType.CORRECTION,
        occurred_at=at(2026, 6, 1, 17),
        business_date=day,
        correction_of_event_id=out.id,
        created_at=at(2026, 6, 2, 9),
    )
    second = event(
        employee_id=employee_id,
        event_type=EventType.CORRECTION,
        occurred_at=at(2026, 6, 1, 18),
        business_date=day,
        correction_of_event_id=first.id,
        created_at=at(2026, 6, 2, 10),
    )
    third = event(
        employee_id=employee_id,
        event_type=EventType.CORRECTION,
        occurred_at=at(2026, 6, 1, 20),
        business_date=day,
        correction_of_event_id=second.id,
        created_at=at(2026, 6, 2, 11),
    )

    record = derive(
        employee_id=employee_id,
        business_date=day,
        events=[
            punch_event(employee_id, EventType.CLOCK_IN, at(2026, 6, 1, 8), day),
            out,
            first,
            second,
            third,
        ],
        today=date(2026, 6, 3),
    )

    assert record.worked_minutes == 720
    assert record.last_out == at(2026, 6, 1, 20)


# --- the interface is four operations and nothing else ----------------------


def test_the_public_surface_is_only_the_operations_a_day_needs() -> None:
    """`codebase-design` §2.4 fixes the interface.

    A new operation is how a timestamp gets back into a date aggregation — a
    `day_for(instant)` would be the first thing a caller reached for, and the
    module's whole risk mitigation is that the caller has nothing to reach for.

    **Ticket 26 added `rebuild_day`, and the list below is where that decision is
    recorded.** It is not a new thing to ask of a day: it is `recompute_day` without the
    commit, so that another module's write and the day it changed land in one transaction
    — the overtime settlement writes a record and rebuilds the day whose
    `overtime_minutes` follows from it. Every operation here still takes and returns
    *dates*, which is the property the interface exists for.
    """
    public = {name for name in vars(AttendanceService) if not name.startswith("_")}

    assert public == {"clock", "day_view", "rebuild_day", "recompute_day", "range_view"}


# --- the stream and the day, over a real database ---------------------------


async def test_a_clock_in_opens_the_day_and_writes_both_rows(platform: Platform) -> None:
    employee_id = await new_employee(platform)
    instant = datetime.now(UTC)

    written = await punch(platform, employee_id, EventType.CLOCK_IN, instant)

    rows = await platform.sql(
        "SELECT event_type, business_date, source, ip_address, created_by_employee_id "
        "FROM attendance_events WHERE employee_id = :id",
        {"id": employee_id},
    )
    assert rows == [
        ("clock_in", madrid_today(instant), "web", None, None)
    ], "the punch is stored with its Madrid business date and its source"
    assert written.business_date == madrid_today(instant)

    day = await platform.sql(
        "SELECT status, worked_minutes, first_in, last_out, expected_minutes, "
        "overtime_minutes, snapshot_schedule_id FROM attendance_daily WHERE employee_id = :id",
        {"id": employee_id},
    )
    assert day == [("working", 0, instant, None, None, None, None)]
    # Ticket 22 fills the expectation; until then the columns say so rather than
    # claiming eight hours nobody has agreed to yet.
    assert day[0][4] is None


async def test_a_clock_out_completes_the_day_without_a_second_recompute(
    platform: Platform,
) -> None:
    """The snapshot the caller reads next is the one their punch produced."""
    employee_id = await new_employee(platform)
    await punch(platform, employee_id, EventType.CLOCK_IN, at(2026, 9, 21, 8))
    await punch(platform, employee_id, EventType.CLOCK_OUT, at(2026, 9, 21, 16, 30))

    record = await read_day(platform, employee_id, NIGHT_SHIFT_MONDAY)

    assert record.status is DayStatus.OK
    assert record.worked_minutes == 510
    assert record.recomputed_at is not None, "the clock_out wrote the snapshot"
    assert await platform.scalar(
        "SELECT count(*) FROM attendance_daily WHERE employee_id = :id", {"id": employee_id}
    ) == 1


async def test_the_stored_business_date_is_madrid_and_the_utc_date_is_not(
    platform: Platform,
) -> None:
    """00:30 in Madrid is 22:30 the day before in UTC, and the row says the 22nd.

    This is the assertion the module exists for: the two dates disagree on the row
    itself, and only one of them is the one the company counts time against.
    """
    employee_id = await new_employee(platform)

    written = await punch(platform, employee_id, EventType.CLOCK_IN, at(2026, 9, 22, 0, 30))

    rows = await platform.sql(
        "SELECT business_date, (occurred_at AT TIME ZONE 'UTC')::date FROM attendance_events "
        "WHERE employee_id = :id",
        {"id": employee_id},
    )
    assert rows == [(date(2026, 9, 22), date(2026, 9, 21))]
    assert written.business_date == date(2026, 9, 22)
    assert await platform.scalar(
        "SELECT business_date FROM attendance_daily WHERE employee_id = :id",
        {"id": employee_id},
    ) == date(2026, 9, 22)


async def test_a_shift_across_midnight_belongs_to_the_day_it_started(
    platform: Platform,
) -> None:
    """22:00 to 06:00 is one eight-hour day, not two half-days and a phantom absence.

    The rule is stated once, in `clock`: a clock_out is attributed to the business
    date of the shift it closes. Nothing else in the module looks at two days at
    once, and the 22nd comes back with no events at all — which is true, and
    readable as such rather than as a missing day.
    """
    employee_id = await new_employee(platform)

    start = await punch(platform, employee_id, EventType.CLOCK_IN, at(2026, 9, 21, 22))
    end = await punch(platform, employee_id, EventType.CLOCK_OUT, at(2026, 9, 22, 6))

    assert (start.business_date, end.business_date) == (NIGHT_SHIFT_MONDAY, NIGHT_SHIFT_MONDAY)
    night = await read_day(platform, employee_id, NIGHT_SHIFT_MONDAY)
    next_day = await read_day(platform, employee_id, date(2026, 9, 22))
    assert (night.status, night.worked_minutes) == (DayStatus.OK, 480)
    assert night.last_out == at(2026, 9, 22, 6)
    assert (next_day.status, next_day.worked_minutes) == (DayStatus.ABSENT, 0)


async def test_the_spring_transition_day_is_one_day_and_one_hour_of_work(
    platform: Platform,
) -> None:
    """01:30 and 03:30 on 29 March 2026 are one hour apart, and the same day.

    Between them the clock jumped forward, and the two subtractions disagree:
    Python's own subtraction of two Madrid datetimes is *wall clock* arithmetic
    (2 hours, the same tzinfo on both sides), while the instants are 1 hour apart.
    A working-time record is the second number — when somebody was at work, not
    what the clock on the wall said.
    """
    employee_id = await new_employee(platform)
    start, end = at(2026, 3, 29, 1, 30), at(2026, 3, 29, 3, 30)
    assert (start.utcoffset(), end.utcoffset()) == (timedelta(hours=1), timedelta(hours=2))
    assert end - start == timedelta(hours=2), "the wall clock"
    assert end.astimezone(UTC) - start.astimezone(UTC) == timedelta(hours=1), "the real elapsed"

    first = await punch(
        platform, employee_id, EventType.CLOCK_IN, start, now=AFTER_BOTH_TRANSITIONS
    )
    second = await punch(
        platform, employee_id, EventType.CLOCK_OUT, end, now=AFTER_BOTH_TRANSITIONS
    )
    record = await read_day(platform, employee_id, SPRING_FORWARD, now=AFTER_BOTH_TRANSITIONS)

    assert (first.business_date, second.business_date) == (SPRING_FORWARD, SPRING_FORWARD)
    assert record.worked_minutes == 60
    assert record.status is DayStatus.OK


async def test_the_fall_back_transition_day_is_one_day_and_two_hours_of_work(
    platform: Platform,
) -> None:
    """01:30 and 02:30 on 25 October 2026 are two hours apart, and the same day.

    The clock went back, so 02:30 happened twice that night and the second one is
    the one a punch at that wall time means (`fold=1`). The wall clock reads one
    hour between the two punches and a real clock reads two — which is the number
    the record has to keep. The clock_in's instant is still 24 October in UTC; the
    Madrid business date says the 25th, the day the person was at work.
    """
    employee_id = await new_employee(platform)
    start = at(2026, 10, 25, 1, 30)
    end = datetime(2026, 10, 25, 2, 30, tzinfo=MADRID, fold=1)
    assert (start.utcoffset(), end.utcoffset()) == (timedelta(hours=2), timedelta(hours=1))
    assert end - start == timedelta(hours=1), "the wall clock"
    assert end.astimezone(UTC) - start.astimezone(UTC) == timedelta(hours=2), "the real elapsed"
    assert start.astimezone(UTC).date() == date(2026, 10, 24)

    first = await punch(
        platform, employee_id, EventType.CLOCK_IN, start, now=AFTER_BOTH_TRANSITIONS
    )
    second = await punch(
        platform, employee_id, EventType.CLOCK_OUT, end, now=AFTER_BOTH_TRANSITIONS
    )
    record = await read_day(platform, employee_id, FALL_BACK, now=AFTER_BOTH_TRANSITIONS)

    assert (first.business_date, second.business_date) == (FALL_BACK, FALL_BACK)
    assert record.worked_minutes == 120
    assert record.status is DayStatus.OK


async def test_clocking_in_twice_is_refused_and_the_day_is_untouched(
    platform: Platform,
) -> None:
    """One shift at a time, and the refusal is catalogued rather than silent.

    The alternative — recording it as an anomaly — was rejected: a second row would
    make the shift's shape a question for every later reader, and the person
    pressing the button again is better served by being told.
    """
    employee_id = await new_employee(platform)
    await punch(platform, employee_id, EventType.CLOCK_IN, at(2026, 9, 21, 8))

    with pytest.raises(DomainError) as refusal:
        await punch(platform, employee_id, EventType.CLOCK_IN, at(2026, 9, 21, 9))

    assert refusal.value.code is AttendanceErrorCode.ALREADY_CLOCKED_IN
    assert refusal.value.http_status == 409
    assert refusal.value.detail is not None and "still open" in refusal.value.detail
    assert await platform.scalar(
        "SELECT count(*) FROM attendance_events WHERE employee_id = :id", {"id": employee_id}
    ) == 1
    record = await read_day(platform, employee_id, NIGHT_SHIFT_MONDAY)
    assert record.status is DayStatus.MISSING_OUT, "the refusal changed nothing"


async def test_clocking_out_without_clocking_in_is_refused(platform: Platform) -> None:
    employee_id = await new_employee(platform)

    with pytest.raises(DomainError) as refusal:
        await punch(platform, employee_id, EventType.CLOCK_OUT, at(2026, 9, 21, 17))

    assert refusal.value.code is AttendanceErrorCode.NO_OPEN_SHIFT
    assert refusal.value.http_status == 409
    assert await platform.scalar(
        "SELECT count(*) FROM attendance_events WHERE employee_id = :id", {"id": employee_id}
    ) == 0


async def test_a_stale_open_shift_is_neither_closed_nor_allowed_to_block(
    platform: Platform,
) -> None:
    """A forgotten clock_out is not silently closed two days later.

    The clock_out that arrives after `MAX_SHIFT` cannot be the end of that shift,
    so it is refused and the earlier day keeps its `missing_out` — the anomaly
    somebody should look at. The stale shift does not block the next day either: an
    employee who forgot to clock out on Friday can still clock in on Monday.
    """
    employee_id = await new_employee(platform)
    friday = date(2026, 9, 18)
    await punch(platform, employee_id, EventType.CLOCK_IN, at(2026, 9, 18, 8))

    with pytest.raises(DomainError) as refusal:
        await punch(platform, employee_id, EventType.CLOCK_OUT, at(2026, 9, 20, 8))

    assert refusal.value.code is AttendanceErrorCode.NO_OPEN_SHIFT
    assert MAX_SHIFT == timedelta(hours=16)
    monday = await punch(platform, employee_id, EventType.CLOCK_IN, at(2026, 9, 21, 8))

    assert monday.business_date == NIGHT_SHIFT_MONDAY
    assert await platform.scalar(
        "SELECT count(*) FROM attendance_events WHERE employee_id = :id", {"id": employee_id}
    ) == 2
    stale = await read_day(platform, employee_id, friday)
    assert (stale.status, stale.worked_minutes) == (DayStatus.MISSING_OUT, 0)


async def test_a_replayed_request_returns_the_row_it_already_wrote(
    platform: Platform,
) -> None:
    """A retry is the same request, so it is the same row — not a second shift."""
    employee_id = await new_employee(platform)
    instant = at(2026, 9, 21, 8)

    first = await punch(platform, employee_id, EventType.CLOCK_IN, instant)
    again = await punch(platform, employee_id, EventType.CLOCK_IN, instant)

    assert again.id == first.id
    assert await platform.scalar(
        "SELECT count(*) FROM attendance_events WHERE employee_id = :id", {"id": employee_id}
    ) == 1


async def test_a_second_press_a_second_later_is_refused_rather_than_recorded(
    platform: Platform,
) -> None:
    """The checklist's "a duplicate within one second", in both of its shapes.

    The identical request is idempotent (the test above); a *different* instant,
    which is what a second press produces, is a second clock_in and is refused. One
    row either way, and the day stays readable.
    """
    employee_id = await new_employee(platform)
    first_click = at(2026, 9, 21, 8, 0)

    await punch(platform, employee_id, EventType.CLOCK_IN, first_click)
    with pytest.raises(DomainError) as refusal:
        await punch(platform, employee_id, EventType.CLOCK_IN, first_click + timedelta(seconds=1))

    assert refusal.value.code is AttendanceErrorCode.ALREADY_CLOCKED_IN
    assert await platform.scalar(
        "SELECT count(*) FROM attendance_events WHERE employee_id = :id", {"id": employee_id}
    ) == 1


async def test_a_punch_cannot_be_recorded_for_a_future_instant(platform: Platform) -> None:
    """Working time records what happened, with a minute of tolerance for a client
    whose clock is a few seconds fast."""
    employee_id = await new_employee(platform)
    now = utc_now()

    with pytest.raises(DomainError) as refusal:
        await punch(platform, employee_id, EventType.CLOCK_IN, now + timedelta(minutes=5))

    assert refusal.value.code is AttendanceErrorCode.EVENT_IN_FUTURE
    assert refusal.value.http_status == 422
    assert await platform.scalar(
        "SELECT count(*) FROM attendance_events WHERE employee_id = :id", {"id": employee_id}
    ) == 0

    # Inside the skew allowance, and therefore accepted.
    await punch(platform, employee_id, EventType.CLOCK_IN, now + timedelta(seconds=30))
    assert await platform.scalar(
        "SELECT count(*) FROM attendance_events WHERE employee_id = :id", {"id": employee_id}
    ) == 1


async def test_a_naive_instant_is_refused_with_a_catalogued_code(platform: Platform) -> None:
    """`business_date_of` raises `ValueError`; the service answers in the vocabulary
    the client routes on, because a 500 whose message mentions timezones is not an
    answer to "why was my punch refused"."""
    employee_id = await new_employee(platform)

    with pytest.raises(DomainError) as refusal:
        await punch(platform, employee_id, EventType.CLOCK_IN, datetime(2026, 9, 21, 8))

    assert refusal.value.code is AttendanceErrorCode.INVALID_REQUEST
    assert refusal.value.http_status == 400


async def test_a_terminated_employee_cannot_clock_in_but_can_close_their_shift(
    platform: Platform,
) -> None:
    """Ticket 18 owns termination; this module owns what it means for the stream.

    Clocking *in* is refused: the record is history. Clocking out is allowed, so a
    shift left open when the termination was applied can be closed — refusing that
    would freeze a `missing_out` nobody can repair — and everything already written
    stays readable either way.
    """
    employee_id = await new_employee(platform)
    await punch(platform, employee_id, EventType.CLOCK_IN, at(2026, 9, 21, 8))
    await platform.sql(
        "UPDATE employees SET status = 'terminated', termination_date = :day WHERE id = :id",
        {"day": date(2026, 9, 21), "id": employee_id},
    )

    with pytest.raises(DomainError) as refusal:
        await punch(platform, employee_id, EventType.CLOCK_IN, at(2026, 9, 21, 9))
    assert refusal.value.code is AttendanceErrorCode.EMPLOYEE_TERMINATED
    assert refusal.value.http_status == 409

    await punch(platform, employee_id, EventType.CLOCK_OUT, at(2026, 9, 21, 17))
    record = await read_day(platform, employee_id, NIGHT_SHIFT_MONDAY)
    assert (record.status, record.worked_minutes) == (DayStatus.OK, 540)


async def test_clock_refuses_to_append_a_correction(platform: Platform) -> None:
    """A correction has a target and a reason, and arrives after an approval; a clock
    button that could write one would be a way to rewrite a punch without either."""
    employee_id = await new_employee(platform)

    with pytest.raises(DomainError) as refusal:
        await punch(platform, employee_id, EventType.CORRECTION, at(2026, 9, 21, 8))

    assert refusal.value.code is AttendanceErrorCode.CORRECTION_NOT_A_PUNCH
    assert refusal.value.http_status == 400


async def test_an_unknown_employee_has_no_day_and_cannot_punch(platform: Platform) -> None:
    stranger = uuid4()

    with pytest.raises(DomainError) as refusal:
        await punch(platform, stranger, EventType.CLOCK_IN, at(2026, 9, 21, 8))
    assert refusal.value.code is AttendanceErrorCode.EMPLOYEE_NOT_FOUND

    with pytest.raises(DomainError) as read:
        await read_day(platform, stranger, NIGHT_SHIFT_MONDAY)
    assert read.value.code is AttendanceErrorCode.EMPLOYEE_NOT_FOUND


async def test_recompute_day_is_idempotent_and_replaces_a_hand_edited_snapshot(
    platform: Platform,
) -> None:
    """A rebuild, never an accumulation, and never a merge.

    The snapshot is edited behind the module's back first: if `recompute_day`
    accumulated, a second run would double it, and if it merged, the edit would
    survive. Neither is allowed — the events are the authority.
    """
    employee_id = await new_employee(platform)
    await worked(platform, employee_id, NIGHT_SHIFT_MONDAY, 8, 16)

    async with attendance(platform) as service:
        once = await service.recompute_day(employee_id, NIGHT_SHIFT_MONDAY)
        twice = await service.recompute_day(employee_id, NIGHT_SHIFT_MONDAY)

    assert (once.worked_minutes, twice.worked_minutes) == (480, 480)
    assert once.first_in == twice.first_in
    assert once.recomputed_at is not None and twice.recomputed_at is not None

    await platform.sql(
        "UPDATE attendance_daily SET worked_minutes = 9999, status = 'ok' WHERE employee_id = :id",
        {"id": employee_id},
    )
    async with attendance(platform) as service:
        healed = await service.recompute_day(employee_id, NIGHT_SHIFT_MONDAY)

    assert healed.worked_minutes == 480, "the snapshot is rebuilt from the events"
    assert await platform.scalar(
        "SELECT count(*) FROM attendance_daily WHERE employee_id = :id", {"id": employee_id}
    ) == 1, "a recompute replaces the row rather than adding one"


async def test_a_recompute_of_a_day_nobody_worked_stores_an_absence(
    platform: Platform,
) -> None:
    """Recomputing is what makes a decision about a day visible to a later reader."""
    employee_id = await new_employee(platform)

    async with attendance(platform) as service:
        stored = await service.recompute_day(employee_id, date(2026, 6, 1))

    assert stored.status is DayStatus.ABSENT
    assert stored.recomputed_at is not None
    assert await platform.scalar(
        "SELECT status FROM attendance_daily WHERE employee_id = :id", {"id": employee_id}
    ) == "absent"


async def test_a_day_with_no_snapshot_is_derived_and_not_written(platform: Platform) -> None:
    """A read is not a write: a day nobody has recomputed comes back derived, with
    `recomputed_at` null so a client can tell the difference."""
    employee_id = await new_employee(platform)

    record = await read_day(platform, employee_id, date(2026, 6, 1))

    assert record.status is DayStatus.ABSENT
    assert record.recomputed_at is None
    assert await platform.scalar(
        "SELECT count(*) FROM attendance_daily WHERE employee_id = :id", {"id": employee_id}
    ) == 0


# --- the correction chain ---------------------------------------------------


async def test_a_correction_appends_and_the_original_row_survives(
    platform: Platform,
) -> None:
    """D25, end to end, through the write ticket 24 will make.

    The correction is appended by the repository, because that is the row a
    correction flow writes; the service has no operation for it yet (its four are
    fixed) and inventing ticket 24's interface here would be building the wrong
    thing twice. What is proven is what matters: the original row is byte-for-byte
    what it was, the new row points at it, and the day is rebuilt to the corrected
    number.
    """
    employee_id = await new_employee(platform)
    await punch(platform, employee_id, EventType.CLOCK_IN, at(2026, 9, 21, 8))
    forgotten = await punch(platform, employee_id, EventType.CLOCK_OUT, at(2026, 9, 21, 16))
    columns = "id, event_type, occurred_at, business_date, source, reason, correction_of_event_id"
    before = await platform.sql(
        f"SELECT {columns} FROM attendance_events WHERE id = :id", {"id": forgotten.id}
    )

    async def correct(instant: datetime, reason: str) -> None:
        async with platform.factory() as session:
            repository = PostgresAttendanceRepository(session)
            await repository.append_event(
                NewEvent(
                    employee_id=employee_id,
                    event_type=EventType.CORRECTION,
                    occurred_at=instant,
                    business_date=NIGHT_SHIFT_MONDAY,
                    source=EventSource.CORRECTION,
                    correction_of_event_id=forgotten.id,
                    reason=reason,
                    created_by_employee_id=employee_id,
                )
            )
            await repository.commit()

    await correct(at(2026, 9, 21, 18), "I clocked out at 18:00, not 16:00")

    after = await platform.sql(
        f"SELECT {columns} FROM attendance_events WHERE id = :id", {"id": forgotten.id}
    )
    assert after == before, "the corrected row was rewritten"
    assert await platform.scalar(
        "SELECT count(*) FROM attendance_events WHERE employee_id = :id", {"id": employee_id}
    ) == 3, "the correction is a new row, not a replacement"

    async with attendance(platform) as service:
        corrected = await service.recompute_day(employee_id, NIGHT_SHIFT_MONDAY)

    assert corrected.worked_minutes == 600
    assert corrected.first_in == at(2026, 9, 21, 8), "the clock_in was not the corrected event"

    # A second correction of the same punch: the chain, and the newest value wins.
    await correct(at(2026, 9, 21, 19), "and it was 19:00 after all")

    async with attendance(platform) as service:
        chained = await service.recompute_day(employee_id, NIGHT_SHIFT_MONDAY)

    assert chained.worked_minutes == 660
    assert await platform.scalar(
        "SELECT count(*) FROM attendance_events WHERE correction_of_event_id = :id",
        {"id": forgotten.id},
    ) == 2, "both corrections stay readable"


async def test_a_correction_needs_a_target_and_a_reason(platform: Platform) -> None:
    """Refused by PostgreSQL, not by a convention: a row that corrects nothing, or
    that gives no reason, would be a row nobody can interpret."""
    employee_id = await new_employee(platform)
    target = await punch(platform, employee_id, EventType.CLOCK_IN, at(2026, 9, 21, 8))
    insert = (
        "INSERT INTO attendance_events (id, employee_id, event_type, occurred_at, business_date, "
        "source, correction_of_event_id, reason) VALUES (gen_random_uuid(), :id, 'correction', "
        "now(), current_date, 'correction', :target, :reason)"
    )

    for constraint, correction_target, reason in (
        ("ck_attendance_events_correction_target", None, "no target"),
        ("ck_attendance_events_correction_reason", target.id, "   "),
    ):
        with pytest.raises(Exception) as refusal:
            await platform.sql(
                insert, {"id": employee_id, "target": correction_target, "reason": reason}
            )
        assert constraint in str(refusal.value), f"{constraint} did not fire"

    with pytest.raises(Exception) as self_correction:
        await platform.sql(
            "INSERT INTO attendance_events (id, employee_id, event_type, occurred_at, "
            "business_date, source, correction_of_event_id, reason) VALUES (:new_id, :id, "
            "'correction', now(), current_date, 'correction', :new_id, 'itself')",
            {"new_id": uuid4(), "id": employee_id},
        )
    assert "ck_attendance_events_self_correction" in str(self_correction.value)


# --- the stream is append-only, in the database -----------------------------


@pytest.fixture
async def app_connection(settings: Settings) -> AsyncIterator[async_sessionmaker]:
    """Sessions bound to the restricted role, on the test database."""
    restricted = create_async_engine(settings.runtime_test_database_url)
    try:
        yield async_sessionmaker(bind=restricted, expire_on_commit=False)
    finally:
        await restricted.dispose()


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE attendance_events SET occurred_at = now()",
        "UPDATE attendance_events SET business_date = current_date",
        "DELETE FROM attendance_events",
    ],
)
async def test_the_event_stream_cannot_be_rewritten(
    platform: Platform, app_connection: async_sessionmaker, statement: str
) -> None:
    """Refused by PostgreSQL, not by a code path somebody has to remember.

    Driven over the restricted role because that is the connection requests use.
    The owner can still rewrite the table, which is exactly why the two connections
    are configured separately — and why "nobody may change a punch, HR included" is
    a statement about privileges rather than about endpoints.
    """
    employee_id = await new_employee(platform)
    await punch(platform, employee_id, EventType.CLOCK_IN, at(2026, 9, 21, 8))

    async with app_connection() as session:
        with pytest.raises(Exception) as refusal:
            await session.execute(text(statement))

    assert "permission denied" in str(refusal.value).lower()
    assert await platform.scalar(
        "SELECT count(*) FROM attendance_events WHERE employee_id = :id", {"id": employee_id}
    ) == 1


async def test_the_stream_can_still_be_appended_to_and_read_by_the_runtime_role(
    platform: Platform, app_connection: async_sessionmaker
) -> None:
    """Append-only, not read-only: the application has to be able to punch."""
    employee_id = await new_employee(platform)

    async with app_connection() as session:
        await session.execute(
            text(
                "INSERT INTO attendance_events (id, employee_id, event_type, occurred_at, "
                "business_date, source) VALUES (gen_random_uuid(), :id, 'clock_in', "
                "TIMESTAMPTZ '2026-09-21 06:00:00+00', DATE '2026-09-21', 'web')"
            ),
            {"id": employee_id},
        )
        await session.commit()
        written = await session.scalar(
            text("SELECT count(*) FROM attendance_events WHERE employee_id = :id"),
            {"id": employee_id},
        )

    assert written == 1


async def test_the_punch_index_covers_exactly_the_punches(platform: Platform) -> None:
    """The predicate is written in SQL and the set of punch types in Python.

    A type added to one and not the other would either let a replay through or
    refuse a legitimate second correction, without any code looking wrong. The
    index names the type it *excludes* and never lists the ones it covers, which is
    what makes that drift impossible to hide.
    """
    definition = await platform.scalar(
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_attendance_events_punch'"
    )

    assert "UNIQUE" in definition
    assert "occurred_at" in definition and "event_type" in definition
    assert f"'{EventType.CORRECTION.value}'" in definition, "the index is not partial"
    for kind in PUNCH_EVENT_TYPES:
        assert kind.value not in definition, "the index enumerates punch types instead of excluding"


# --- a range is complete ----------------------------------------------------


async def test_a_range_represents_the_days_without_events(platform: Platform) -> None:
    """A month with three days worked is not a month with three days.

    The gap is the answer as much as the work is: an absence nobody can see is an
    absence nobody can query.
    """
    employee_id = await new_employee(platform)
    days_worked = [date(2026, 6, 2), date(2026, 6, 10), date(2026, 6, 18)]
    for day in days_worked:
        await worked(platform, employee_id, day, 8, 16)

    async with attendance(platform) as service:
        days = await service.range_view(employee_id, date(2026, 6, 1), date(2026, 6, 30))

    assert [day.business_date for day in days] == dates_between(
        date(2026, 6, 1), date(2026, 6, 30)
    )
    assert len(days) == 30
    assert [day.worked_minutes for day in days if day.status is DayStatus.OK] == [480, 480, 480]
    assert sum(1 for day in days if day.status is DayStatus.ABSENT) == 27
    assert all(
        day.recomputed_at is None for day in days if day.business_date not in days_worked
    ), "a day nobody worked is derived for the answer and not written down"


async def test_range_view_refuses_an_inverted_or_over_long_range(platform: Platform) -> None:
    """Refused rather than answered with an empty list: "nothing there" and "you
    asked the wrong question" must not look the same."""
    employee_id = await new_employee(platform)

    async with attendance(platform) as service:
        with pytest.raises(DomainError) as inverted:
            await service.range_view(employee_id, date(2026, 6, 2), date(2026, 6, 1))
        with pytest.raises(DomainError) as too_long:
            await service.range_view(
                employee_id, date(2020, 1, 1), date(2020, 1, 1) + timedelta(days=MAX_RANGE_DAYS)
            )

    assert inverted.value.code is AttendanceErrorCode.RANGE_INVALID
    assert inverted.value.http_status == 422
    assert too_long.value.code is AttendanceErrorCode.RANGE_INVALID


# --- the endpoints ----------------------------------------------------------


async def test_the_clock_endpoint_punches_and_returns_the_business_date(
    platform: Platform,
) -> None:
    """End to end through the session, the kernel and the restricted role."""
    actor = await platform.account(roles=("employee",))

    response = await actor.post("/api/v1/attendance/clock", json={"kind": "clock_in"})

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["event_type"] == "clock_in"
    assert body["source"] == "web"
    assert body["business_date"] == madrid_today(datetime.now(UTC)).isoformat()
    # The row went in through `eam_app`, which is the role that cannot rewrite it,
    # and it carries where the punch came from and on whose behalf.
    stored = await platform.sql(
        "SELECT source, ip_address, created_by_employee_id FROM attendance_events "
        "WHERE employee_id = :id",
        {"id": actor.employee_id},
    )
    assert stored == [("web", "127.0.0.1", UUID(actor.employee_id))]


async def test_the_day_endpoint_defaults_to_today_in_madrid(platform: Platform) -> None:
    """The client does not have to know what day it is where the company is."""
    actor = await platform.account(roles=("employee",))

    before = madrid_today(datetime.now(UTC))
    response = await actor.get("/api/v1/attendance/day")
    after = madrid_today(datetime.now(UTC))

    assert response.status_code == 200, response.text
    assert response.json()["business_date"] in {before.isoformat(), after.isoformat()}
    assert response.json()["status"] == "absent"


async def test_the_range_endpoint_answers_every_day_it_was_asked_for(
    platform: Platform,
) -> None:
    actor = await platform.account(roles=("employee",))

    response = await actor.get(
        "/api/v1/attendance/range", params={"from_date": "2026-06-01", "to_date": "2026-06-30"}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["from_date"], body["to_date"]) == ("2026-06-01", "2026-06-30")
    assert len(body["days"]) == 30
    assert {day["status"] for day in body["days"]} == {"absent"}


async def test_an_employee_cannot_read_a_colleagues_day(platform: Platform) -> None:
    """The refusal the ticket asks for, and the record of it.

    Reading somebody else's attendance is a manager-of-that-person or HR question
    and belongs to ticket 24. Until it exists as its own action with its own
    resource rule, the answer here is no — for an ordinary employee, and for
    everybody else, which the kernel test below pins.
    """
    actor = await platform.account(roles=("employee",))
    colleague = await platform.employee()

    response = await actor.get("/api/v1/attendance/day", params={"employee_id": colleague})

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == ErrorCode.FORBIDDEN.value
    assert "worked_minutes" not in response.text
    refusals = await platform.sql("SELECT after FROM audit_log WHERE action = 'access.refused'")
    assert any(
        row[0]["action"] == str(Action.ATTENDANCE_READ_OWN) for row in refusals
    ), f"the refusal was not recorded: {refusals}"


async def test_an_employee_cannot_clock_for_a_colleague(platform: Platform) -> None:
    actor = await platform.account(roles=("employee",))
    colleague = await platform.employee()

    response = await actor.post(
        "/api/v1/attendance/clock", json={"kind": "clock_in", "employee_id": colleague}
    )

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == ErrorCode.FORBIDDEN.value
    assert await platform.scalar(
        "SELECT count(*) FROM attendance_events WHERE employee_id = :id", {"id": colleague}
    ) == 0, "a punch was written for the colleague"
    refusals = await platform.sql("SELECT after FROM audit_log WHERE action = 'access.refused'")
    assert any(
        row[0]["action"] == str(Action.ATTENDANCE_CLOCK_OWN) for row in refusals
    ), f"the refusal was not recorded: {refusals}"


def test_no_role_reaches_somebody_elses_attendance_through_the_self_only_actions() -> None:
    """The boundary ticket 24 has to cross with a new action.

    Every role, including HR and a manager with the subject as their report: the
    resource rule is ownership, so the answer is the same for all of them. The
    control is the second assertion — each of those principals *can* reach their
    own record — so the refusal above is the ownership rule rather than a role
    check that happens to be false for everybody.
    """
    subject = uuid4()
    for role in sorted(SYSTEM_ROLES):
        principal = Principal(
            user_id=uuid4(),
            employee_id=uuid4(),
            username="ana",
            roles=frozenset({role, "employee"}),
            clearance_level="high",
            department_ids=frozenset({uuid4()}),
            primary_department_id=uuid4(),
            is_manager=role == "manager",
            reports_employee_ids=frozenset({subject}),
        )
        for action in (Action.ATTENDANCE_READ_OWN, Action.ATTENDANCE_CLOCK_OWN):
            decision = can(principal, action, own_resource(subject))
            assert decision.denied, f"role={role} reached somebody else's attendance"
            assert decision.primary_reason is Reason.NOT_OWNER
            assert can(principal, action, own_resource(principal.employee_id)).allowed
            # A resource whose owner is unknown is refused rather than allowed:
            # "we cannot tell that it is yours" is not a permission.
            assert can(
                principal, action, Resource(ResourceKind.EMPLOYEE, owner_employee_id=None)
            ).denied


async def test_the_clock_endpoint_answers_within_the_ticket_budget(platform: Platform) -> None:
    """200 ms for a punch, measured against the real database.

    The two calls before it are the warm-up: the connection pool, the prepared
    statements and the permission snapshot cost something on a first request and
    nothing afterwards, and the budget is about a punch somebody is waiting for
    rather than about a cold process.
    """
    actor = await platform.account(roles=("employee",))
    now = utc_now()
    await actor.post(
        "/api/v1/attendance/clock",
        json={"kind": "clock_in", "at": (now - timedelta(hours=2)).isoformat()},
    )
    await actor.post(
        "/api/v1/attendance/clock",
        json={"kind": "clock_out", "at": (now - timedelta(hours=1)).isoformat()},
    )

    started = time.perf_counter()
    response = await actor.post(
        "/api/v1/attendance/clock",
        json={"kind": "clock_in", "at": (now - timedelta(minutes=30)).isoformat()},
    )
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert response.status_code == 201, response.text
    assert elapsed_ms < 200, f"a punch took {elapsed_ms:.0f} ms"
