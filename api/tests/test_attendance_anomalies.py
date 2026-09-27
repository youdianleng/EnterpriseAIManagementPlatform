"""Ticket 23: the clock-out notification, the nightly anomaly scan, the reminder.

No mocks and no in-memory repository, for the reason `test_attendance_events.py`
gives: what this module has to get right is largely a *query* — which assignment's
manager somebody's punches belong to, whether a day was expected of them at all,
whether a second pass over the same day writes a second row, whether the runtime
role can delete an anomaly — and a substitute would answer those with the test's own
assumptions.

**Both halves of the ticket's "any date and any punches".** The detection rules are
pure and are tested with hand-built events at the top of this file; the pass over a
real database drives a fixed Monday and Tuesday through the whole stack, so nothing
here depends on the day the suite happens to run on.

**The leave seam is filled with a test double and nothing else.** Ticket 25 owns
what leave is; this file owns proving that the scan asks, and that a `True` answer
suppresses the day. `LeaveOnDates` below is deliberately the smallest thing that can
answer the protocol.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, time
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.domain.attendance.anomalies import (
    PUNCH_TOLERANCE,
    AnomalyType,
    detect,
)
from app.domain.attendance.anomaly_repository import LeaveLookup
from app.domain.attendance.anomaly_service import AnomalyService
from app.domain.attendance.business_day import MADRID
from app.domain.attendance.models import (
    AttendanceEvent,
    EventSource,
    EventType,
    NewEvent,
    utc_now,
)
from app.domain.attendance.notify import AnomalyReminder, AttendanceNotifier
from app.domain.attendance.service import AttendanceService
from app.domain.notification.models import (
    DIGEST_CANDIDATE_TYPES,
    NotificationType,
)
from app.domain.notification.service import NotificationService
from app.domain.schedule.models import (
    DayExpectation,
    Holiday,
    HolidayScope,
    ScheduleDay,
    ScheduleDayInput,
    ScheduleInput,
    ScheduleSource,
)
from app.domain.schedule.service import ScheduleService
from app.jobs.scan_attendance_anomalies import previous_day
from app.repositories.attendance import (
    PostgresAnomalyRepository,
    PostgresAttendanceRepository,
)
from app.repositories.notification import PostgresNotificationRepository
from app.repositories.schedule import PostgresScheduleRepository
from tests.support.platform import Platform

#: A Monday and the Tuesday after it. Fixed rather than "today", because a test
#: whose result depends on the day it runs on is not evidence — and a Monday because
#: the schedule below works Monday to Friday, so nothing here leans on a weekend it
#: did not set up.
MONDAY = date(2026, 9, 21)
TUESDAY = date(2026, 9, 22)
SATURDAY = date(2026, 9, 19)

#: The window the department works. 09:00–17:00 is 480 minutes with no break, which
#: is what `work_schedule_days` requires the two to agree on.
OPENS = time(9, 0)
CLOSES = time(17, 0)
FULL_DAY = 480

#: The pass's own clock, pinned: `detected_at` is evidence of when somebody could
#: have known, and a test that cannot pin it cannot assert it.
FIXED_NOW = datetime(2026, 10, 1, 6, 30, tzinfo=UTC)


def at(day: date, hour: int, minute: int = 0) -> datetime:
    """An instant as somebody in Madrid would say it, through `ZoneInfo`."""
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=MADRID)


def punch(
    employee_id: UUID,
    kind: EventType,
    instant: datetime,
    *,
    day: date = MONDAY,
    created_at: datetime | None = None,
) -> AttendanceEvent:
    """A hand-built row, for the pure tests. The database tests write real ones."""
    return AttendanceEvent(
        id=uuid4(),
        employee_id=employee_id,
        event_type=kind,
        occurred_at=instant,
        business_date=day,
        source=EventSource.WEB,
        created_at=created_at or instant,
    )


def correction(target: AttendanceEvent, instant: datetime) -> AttendanceEvent:
    """A correction of `target`, restating it at another instant (DESIGN D25)."""
    return AttendanceEvent(
        id=uuid4(),
        employee_id=target.employee_id,
        event_type=EventType.CORRECTION,
        occurred_at=instant,
        business_date=target.business_date,
        source=EventSource.CORRECTION,
        created_at=instant,
        correction_of_event_id=target.id,
        reason="the punch clock was wrong",
    )


def expectation(
    *,
    employee_id: UUID | None = None,
    business_date: date = MONDAY,
    minutes: int = FULL_DAY,
    start: time = OPENS,
    end: time = CLOSES,
    holiday: Holiday | None = None,
    source: ScheduleSource = ScheduleSource.DEPARTMENT,
) -> DayExpectation:
    """What the scheduling module would have answered for one day, as a value."""
    return DayExpectation(
        employee_id=employee_id or uuid4(),
        business_date=business_date,
        expected_minutes=0 if holiday is not None else minutes,
        source=source,
        weekday=business_date.weekday(),
        schedule_id=uuid4() if source is not ScheduleSource.NONE else None,
        schedule_day=ScheduleDay(
            weekday=business_date.weekday(),
            expected_minutes=minutes,
            start_time=start,
            end_time=end,
        ),
        holiday=holiday,
    )


def madrid_holiday(business_date: date = MONDAY) -> Holiday:
    return Holiday(
        id=uuid4(),
        date=business_date,
        name_es="Fiesta",
        name_en="Holiday",
        scope=HolidayScope.NATIONAL,
        year=business_date.year,
    )


class LeaveOnDates:
    """The seam ticket 25 fills, with one person's date in it.

    The smallest thing that satisfies `LeaveLookup` and records what it was asked,
    so the test proves the scan *asks* — per person, per date — rather than that
    some leave implementation works. Keyed by the pair, because leave is a fact
    about a person and a date and the scan's whole job is to ask about both.
    """

    def __init__(self, *entries: tuple[UUID, date]) -> None:
        self.entries = set(entries)
        self.asked: list[tuple[UUID, date]] = []

    async def is_on_leave(self, employee_id: UUID, business_date: date) -> bool:
        self.asked.append((employee_id, business_date))
        return (employee_id, business_date) in self.entries


# --- the module on its own session, the way a request or a job uses it --------


@asynccontextmanager
async def attendance_service(
    platform: Platform, *, now: datetime | None = None
) -> AsyncIterator[AttendanceService]:
    async with platform.factory() as session:
        yield AttendanceService(
            PostgresAttendanceRepository(session),
            now=utc_now if now is None else (lambda: now),
            expectations=ScheduleService(PostgresScheduleRepository(session), session),
        )


@asynccontextmanager
async def notifier(platform: Platform) -> AsyncIterator[AttendanceNotifier]:
    """The service as the attendance endpoints build it: wrapped, so it notifies."""
    async with platform.factory() as session:
        repository = PostgresAttendanceRepository(session)
        yield AttendanceNotifier(
            AttendanceService(
                repository,
                expectations=ScheduleService(PostgresScheduleRepository(session), session),
            ),
            NotificationService(PostgresNotificationRepository(session), session),
            repository,
        )


@asynccontextmanager
async def anomalies(
    platform: Platform, *, leave: LeaveLookup | None = None, now: datetime | None = None
) -> AsyncIterator[AnomalyService]:
    async with platform.factory() as session:
        yield _anomalies(session, leave=leave, now=now)


@asynccontextmanager
async def reminder_pass(
    platform: Platform, *, leave: LeaveLookup | None = None, now: datetime | None = None
) -> AsyncIterator[tuple[AnomalyService, AnomalyReminder]]:
    """The scan and the reminder, wired as the job wires them."""
    async with platform.factory() as session:
        service = _anomalies(session, leave=leave, now=now)
        yield service, AnomalyReminder(
            service,
            NotificationService(PostgresNotificationRepository(session), session),
        )


def _anomalies(session, *, leave: LeaveLookup | None, now: datetime | None) -> AnomalyService:
    return AnomalyService(
        PostgresAnomalyRepository(session),
        expectations=ScheduleService(PostgresScheduleRepository(session), session),
        leave=leave,
        now=utc_now if now is None else (lambda: now),
    )


@asynccontextmanager
async def notifications(platform: Platform) -> AsyncIterator[NotificationService]:
    async with platform.factory() as session:
        yield NotificationService(PostgresNotificationRepository(session), session)


# --- the organisation a scan has something to say about -----------------------


async def works_here(
    platform: Platform,
    *,
    code: str = "OPS",
    employee_id: str | None = None,
    manager_employee_id: str | None = None,
    notification_override_employee_id: str | None = None,
    department_manager: str | None = None,
    schedule: bool = True,
) -> UUID:
    """A department, a position, a schedule and somebody assigned to it.

    The schedule is the department's rather than the company default on purpose:
    the scan examines everybody a schedule reaches, and a default would also reach
    the accounts these tests create for other reasons.

    `department_manager` is appointed through the endpoint and *after* the position
    exists: the organisation module only accepts somebody who holds an active
    position in the department, which is a rule worth exercising rather than going
    around.
    """
    department = await platform.department(code)
    position = await platform.position(department, f"{code}-P")
    subject = employee_id or await platform.employee()
    await platform.assign(
        subject,
        department,
        position,
        manager_employee_id=manager_employee_id,
        notification_override_employee_id=notification_override_employee_id,
    )
    if department_manager is not None:
        if UUID(department_manager) != UUID(subject):
            await platform.assign(department_manager, department, position)
        appointer = await platform.account(roles=("admin",))
        response = await appointer.call(
            "PUT",
            f"/api/v1/departments/{department}/manager",
            json={"employee_id": department_manager},
        )
        assert response.status_code == 200, response.text
    if schedule:
        async with platform.factory() as session:
            await ScheduleService(PostgresScheduleRepository(session), session).create_schedule(
                ScheduleInput(
                    code=f"{code}-WEEK",
                    name_es="Semana",
                    name_en="Week",
                    days=tuple(
                        ScheduleDayInput(
                            weekday=weekday,
                            expected_minutes=FULL_DAY,
                            start_time=OPENS,
                            end_time=CLOSES,
                        )
                        for weekday in range(5)
                    ),
                    department_id=UUID(department),
                )
            )
    return UUID(subject)


async def clocked(
    platform: Platform, employee_id: UUID, day: date, *, start: int = 9, end: int = 17
) -> None:
    """A complete shift, through the notifier the endpoints use."""
    async with notifier(platform) as service:
        await service.clock(employee_id, EventType.CLOCK_IN, at(day, start), EventSource.WEB)
        await service.clock(employee_id, EventType.CLOCK_OUT, at(day, end), EventSource.WEB)


async def rows(platform: Platform, day: date) -> list[tuple]:
    """The stored anomalies of one day: employee, type, detected, notified, resolved."""
    return await platform.sql(
        "SELECT employee_id, type, detected_at, notified_at, resolved_by_event_id "
        "FROM attendance_anomalies WHERE business_date = :day ORDER BY employee_id, type",
        {"day": day},
    )


# --- the rules, purely --------------------------------------------------------


def test_a_day_nobody_punched_is_one_anomaly_and_not_two() -> None:
    """No clock_in and no clock_out is one thing that happened to somebody.

    Reporting it as two would double every absence in every count that reads this
    table, and which punch is missing is unknowable when neither exists.
    """
    employee_id = uuid4()
    found = detect(
        employee_id=employee_id,
        business_date=MONDAY,
        events=[],
        expected=expectation(employee_id=employee_id),
    )

    assert [anomaly.type for anomaly in found] == [AnomalyType.NO_PUNCHES]


def test_an_open_shift_is_a_missing_clock_out() -> None:
    employee_id = uuid4()
    found = detect(
        employee_id=employee_id,
        business_date=MONDAY,
        events=[punch(employee_id, EventType.CLOCK_IN, at(MONDAY, 9))],
        expected=expectation(employee_id=employee_id),
    )

    assert [anomaly.type for anomaly in found] == [AnomalyType.MISSING_CLOCK_OUT]


def test_a_clock_out_with_no_shift_to_close_is_a_missing_clock_in() -> None:
    """The write path refuses one, so a day in this state came from a correction or
    a hand-written row — and it is exactly the day somebody has to look at."""
    employee_id = uuid4()
    found = detect(
        employee_id=employee_id,
        business_date=MONDAY,
        events=[punch(employee_id, EventType.CLOCK_OUT, at(MONDAY, 17))],
        expected=expectation(employee_id=employee_id),
    )

    assert [anomaly.type for anomaly in found] == [AnomalyType.MISSING_CLOCK_IN]


def test_late_and_early_are_measured_against_the_window_with_the_tolerance() -> None:
    """The ticket's real decision: a minute or two either side is not a fact.

    Both edges are asserted at the tolerance and one minute past it, because an
    off-by-one in either direction is invisible in a test that only checks the
    obvious case.
    """
    employee_id = uuid4()
    window = expectation(employee_id=employee_id)
    tolerance = int(PUNCH_TOLERANCE.total_seconds() // 60)

    def kinds(start: int, start_minutes: int, end: int, end_minutes: int) -> list[AnomalyType]:
        return [
            anomaly.type
            for anomaly in detect(
                employee_id=employee_id,
                business_date=MONDAY,
                events=[
                    punch(employee_id, EventType.CLOCK_IN, at(MONDAY, start, start_minutes)),
                    punch(employee_id, EventType.CLOCK_OUT, at(MONDAY, end, end_minutes)),
                ],
                expected=window,
            )
        ]

    assert kinds(9, tolerance, 17, 0) == []
    assert kinds(9, tolerance + 1, 17, 0) == [AnomalyType.LATE]
    assert kinds(9, 0, 16, 60 - tolerance) == []
    assert kinds(9, 0, 16, 60 - tolerance - 1) == [AnomalyType.EARLY_LEAVE]


def test_a_correction_moves_the_instant_the_scan_judges() -> None:
    """A correction restates a punch's moment, and the scan reads the restatement.

    Otherwise a correction that fixed a late arrival would leave the lateness
    standing for ever, and the day's own record would say something the anomaly
    beside it contradicts.
    """
    employee_id = uuid4()
    late_in = punch(employee_id, EventType.CLOCK_IN, at(MONDAY, 9, 30))
    out = punch(employee_id, EventType.CLOCK_OUT, at(MONDAY, 17))
    fixed = correction(late_in, at(MONDAY, 9))

    assert [
        anomaly.type
        for anomaly in detect(
            employee_id=employee_id,
            business_date=MONDAY,
            events=[late_in, out],
            expected=expectation(employee_id=employee_id),
        )
    ] == [AnomalyType.LATE]
    assert (
        detect(
            employee_id=employee_id,
            business_date=MONDAY,
            events=[late_in, out, fixed],
            expected=expectation(employee_id=employee_id),
        )
        == []
    )


def test_a_holiday_a_rest_day_and_leave_all_produce_nothing() -> None:
    """Three different reasons nobody was due, and none of them is an absence.

    A labour record that called a public holiday an absence would be wrong in the
    direction people complain about, and it is the direction this table is read in.
    """
    employee_id = uuid4()
    cases = {
        "holiday": expectation(
            employee_id=employee_id, holiday=madrid_holiday(), minutes=FULL_DAY
        ),
        "rest day": expectation(employee_id=employee_id, business_date=SATURDAY, minutes=0),
        "no schedule": expectation(
            employee_id=employee_id, source=ScheduleSource.NONE, minutes=0
        ),
    }

    for reason, window in cases.items():
        assert (
            detect(
                employee_id=employee_id,
                business_date=window.business_date,
                events=[],
                expected=window,
            )
            == []
        ), reason

    assert (
        detect(
            employee_id=employee_id,
            business_date=MONDAY,
            events=[],
            expected=expectation(employee_id=employee_id),
            on_leave=True,
        )
        == []
    ), "leave"


def test_a_punch_that_belongs_to_another_madrid_day_is_not_this_days_window() -> None:
    """A shift that crossed midnight is judged by the day it began.

    Its clock_out reads 06:00, which against a 09:00–17:00 window would be an early
    leave invented out of the cross-midnight rule — that punch is not in this day,
    so this day's window has nothing to say about it.
    """
    employee_id = uuid4()
    found = detect(
        employee_id=employee_id,
        business_date=MONDAY,
        events=[
            punch(employee_id, EventType.CLOCK_IN, at(MONDAY, 22)),
            punch(employee_id, EventType.CLOCK_OUT, at(TUESDAY, 6)),
        ],
        expected=expectation(employee_id=employee_id, start=time(22, 0), end=time(23, 0)),
    )

    assert found == []


# --- the scan over a real database -------------------------------------------


async def test_the_nightly_scan_records_what_each_day_is_missing(platform: Platform) -> None:
    """One pass over a Monday, asserting the whole answer rather than one row.

    What goes wrong with a scan is not a row: it is the population it examined and
    the rows it did not write. A punctual day is part of the answer too.
    """
    forgetful = await works_here(platform, code="OPS")
    latecomer = await works_here(platform, code="FIN")
    away = await works_here(platform, code="IT")
    punctual = await works_here(platform, code="HR")

    async with attendance_service(platform) as service:
        await service.clock(forgetful, EventType.CLOCK_IN, at(MONDAY, 9), EventSource.WEB)
        await service.clock(latecomer, EventType.CLOCK_IN, at(MONDAY, 9, 30), EventSource.WEB)
        await service.clock(latecomer, EventType.CLOCK_OUT, at(MONDAY, 16, 0), EventSource.WEB)
        await service.clock(punctual, EventType.CLOCK_IN, at(MONDAY, 9), EventSource.WEB)
        await service.clock(punctual, EventType.CLOCK_OUT, at(MONDAY, 17), EventSource.WEB)

    async with anomalies(platform, now=FIXED_NOW) as service:
        report = await service.scan(MONDAY)

    assert {(row[0], row[1]) for row in await rows(platform, MONDAY)} == {
        (forgetful, AnomalyType.MISSING_CLOCK_OUT.value),
        (latecomer, AnomalyType.LATE.value),
        (latecomer, AnomalyType.EARLY_LEAVE.value),
        (away, AnomalyType.NO_PUNCHES.value),
    }
    assert report.examined == 4
    assert report.created_count == 4
    assert report.existing == 0
    assert report.failed == ()
    # The pass's own clock, not the database's: it is evidence of when somebody
    # could have known.
    assert {row[2] for row in await rows(platform, MONDAY)} == {FIXED_NOW}


async def test_running_the_scan_twice_creates_anomalies_once(platform: Platform) -> None:
    """Both halves of the ticket's idempotency line.

    The first pass creates; the second creates nothing and says so — `existing`
    rather than an empty report, so "nothing was wrong" and "nothing was written"
    are different answers.
    """
    employee_id = await works_here(platform)
    async with attendance_service(platform) as service:
        await service.clock(employee_id, EventType.CLOCK_IN, at(MONDAY, 9), EventSource.WEB)

    async with anomalies(platform) as service:
        first = await service.scan(MONDAY)
        second = await service.scan(MONDAY)

    assert first.created_count == 1 and first.existing == 0
    assert second.created_count == 0 and second.existing == 1
    assert len(await rows(platform, MONDAY)) == 1
    assert first.created[0].type is AnomalyType.MISSING_CLOCK_OUT


async def test_a_holiday_and_a_rest_day_produce_no_anomalies(platform: Platform) -> None:
    """A holiday is a fact about the date and reaches everybody; a Saturday is a
    fact about the week. Neither is a day somebody was due, so neither is examined."""
    await works_here(platform, code="OPS")
    await platform.sql(
        "INSERT INTO holidays (id, date, name_es, name_en, scope, year) "
        "VALUES (gen_random_uuid(), :day, 'Fiesta', 'Holiday', 'national', :year)",
        {"day": MONDAY, "year": MONDAY.year},
    )

    async with anomalies(platform) as service:
        holiday = await service.scan(MONDAY)
        weekend = await service.scan(SATURDAY)

    assert holiday.examined == 0 and holiday.created_count == 0
    assert weekend.examined == 0 and weekend.created_count == 0
    assert await rows(platform, MONDAY) == []


async def test_leave_suppresses_a_day_for_one_person_and_not_their_colleague(
    platform: Platform,
) -> None:
    """The scan asks the leave seam per person, and a `True` suppresses the day.

    Ticket 25 puts the real lookup behind this; until then `AssumeNoLeave` is the
    answer, and this is what proves the question is asked at all.
    """
    away = await works_here(platform, code="OPS")
    present = await works_here(platform, code="FIN")
    leave = LeaveOnDates((away, MONDAY))

    async with anomalies(platform, leave=leave) as service:
        report = await service.scan(MONDAY)

    assert report.examined == 2
    assert report.created_count == 1
    assert [row[0] for row in await rows(platform, MONDAY)] == [present]
    assert (away, MONDAY) in leave.asked and (present, MONDAY) in leave.asked


async def test_the_scan_answers_for_whichever_date_it_is_given(platform: Platform) -> None:
    """Any date, any punches: nothing here waits for a night to pass.

    The same employee is clean on Monday and late on Tuesday, and the two passes
    are independent — which is what makes re-examining a corrected day possible.
    """
    employee_id = await works_here(platform)
    await clocked(platform, employee_id, MONDAY)

    async with attendance_service(platform) as service:
        await service.clock(employee_id, EventType.CLOCK_IN, at(TUESDAY, 9, 45), EventSource.WEB)
        await service.clock(employee_id, EventType.CLOCK_OUT, at(TUESDAY, 17), EventSource.WEB)

    async with anomalies(platform) as service:
        monday = await service.scan(MONDAY)
        tuesday = await service.scan(TUESDAY)

    assert monday.examined == 1 and monday.created_count == 0
    assert tuesday.examined == 1 and tuesday.created_count == 1
    assert await rows(platform, MONDAY) == []
    assert [row[1] for row in await rows(platform, TUESDAY)] == [AnomalyType.LATE.value]


async def test_a_day_nobody_was_due_leaves_the_scan_nothing_to_examine(
    platform: Platform,
) -> None:
    """`examined` counts the people the schedule expected work from.

    An employee with no assignment and a department with no schedule are both
    outside that population, which is why "nothing was created" needs the count
    beside it to mean anything.
    """
    await platform.employee()
    await works_here(platform, code="OPS", schedule=False)

    async with anomalies(platform) as service:
        report = await service.scan(MONDAY)

    assert report.examined == 0
    assert report.created_count == 0
    assert await rows(platform, MONDAY) == []


# --- the clock-out notification ----------------------------------------------


async def test_clocking_out_notifies_the_manager_of_the_primary_position(
    platform: Platform,
) -> None:
    manager = UUID(await platform.employee())
    employee_id = await works_here(
        platform,
        employee_id=await platform.employee(),
        manager_employee_id=str(manager),
    )
    async with notifier(platform) as service:
        await service.clock(employee_id, EventType.CLOCK_IN, at(MONDAY, 9), EventSource.WEB)
        event = await service.clock(
            employee_id, EventType.CLOCK_OUT, at(MONDAY, 17), EventSource.WEB
        )

    async with notifications(platform) as centre:
        page = await centre.list_for(manager)
        own = await centre.list_for(employee_id)

    assert page.total == 1
    raised = page.items[0]
    assert raised.type is NotificationType.ATTENDANCE_CLOCK_OUT
    assert raised.title_key == "notifications.attendance.clock_out"
    assert raised.entity_type == "attendance_event"
    assert raised.payload == {
        "employee_id": str(employee_id),
        "event_id": str(event.id),
        "business_date": MONDAY.isoformat(),
        "occurred_at": at(MONDAY, 17).astimezone(UTC).isoformat(),
    }
    # The punch does not notify the person who punched: this is about their day,
    # addressed to somebody who has to know about it.
    assert own.total == 0


async def test_the_assignments_notification_override_wins_over_the_manager(
    platform: Platform,
) -> None:
    """The field exists for this, and it wins because somebody typed it for this
    person rather than inheriting it from the org chart."""
    manager = await platform.employee()
    covering = UUID(await platform.employee())
    employee_id = await works_here(
        platform,
        employee_id=await platform.employee(),
        manager_employee_id=manager,
        notification_override_employee_id=str(covering),
    )
    await clocked(platform, employee_id, MONDAY)

    async with notifications(platform) as centre:
        assert (await centre.list_for(manager)).total == 0
        page = await centre.list_for(covering)

    assert page.total == 1
    assert page.items[0].type is NotificationType.ATTENDANCE_CLOCK_OUT


async def test_a_department_with_no_manager_notifies_nobody_and_still_punches(
    platform: Platform,
) -> None:
    """Nobody to tell is not an error and not a reason to invent a recipient.

    The punch is the record; the notification is a courtesy on top of it, and a
    clock-out that failed because an org chart was incomplete would be the record
    losing to the courtesy.
    """
    employee_id = await works_here(platform)
    await clocked(platform, employee_id, MONDAY)

    assert await platform.scalar(
        "SELECT count(*) FROM attendance_events WHERE employee_id = :id", {"id": employee_id}
    ) == 2
    assert await platform.scalar("SELECT count(*) FROM notifications") == 0


async def test_a_position_with_no_manager_falls_back_to_the_department_manager(
    platform: Platform,
) -> None:
    """The same fallback the approval route uses: a position that names nobody
    still belongs to a department somebody is accountable for."""
    head = UUID(await platform.employee())
    employee_id = await works_here(
        platform,
        employee_id=await platform.employee(),
        department_manager=str(head),
    )
    await clocked(platform, employee_id, MONDAY)

    async with notifications(platform) as centre:
        page = await centre.list_for(head)

    assert page.total == 1
    assert page.items[0].type is NotificationType.ATTENDANCE_CLOCK_OUT


async def test_a_department_manager_clocking_out_is_not_notified_about_themselves(
    platform: Platform,
) -> None:
    """A one-person department is a real configuration — its head is the manager of
    their own department — and being told what they just did is not a notification.

    The position's own manager field cannot express this (the employee module
    refuses "an employee cannot be their own approver"), which is exactly why the
    fallback needs the rule too.
    """
    employee_id = await platform.employee()
    themselves = await works_here(
        platform, employee_id=employee_id, department_manager=employee_id
    )
    await clocked(platform, themselves, MONDAY)

    assert await platform.scalar("SELECT count(*) FROM notifications") == 0


async def test_a_replayed_clock_out_notifies_the_manager_once(platform: Platform) -> None:
    """A retried request is the same event: the dedupe key is the event's, so the
    second raise is suppressed rather than repeated."""
    manager = UUID(await platform.employee())
    employee_id = await works_here(
        platform, employee_id=await platform.employee(), manager_employee_id=str(manager)
    )
    async with notifier(platform) as service:
        await service.clock(employee_id, EventType.CLOCK_IN, at(MONDAY, 9), EventSource.WEB)
        await service.clock(employee_id, EventType.CLOCK_OUT, at(MONDAY, 17), EventSource.WEB)
        replay = await service.clock(
            employee_id, EventType.CLOCK_OUT, at(MONDAY, 17), EventSource.WEB
        )

    async with notifications(platform) as centre:
        page = await centre.list_for(manager)

    assert page.total == 1
    assert page.items[0].entity_id == replay.id


async def test_the_clock_out_notification_is_a_candidate_for_the_digest(
    platform: Platform,
) -> None:
    """In the centre, and in the set ticket 20's morning mail selects from.

    Both halves are asserted through the rows the notification wrote: the in-app
    delivery is already `sent` (the row *is* the delivery), and the email one waits
    at `pending` with the reason nothing has sent it — the queue the digest drains.
    """
    manager = UUID(await platform.employee())
    employee_id = await works_here(
        platform, employee_id=await platform.employee(), manager_employee_id=str(manager)
    )
    await clocked(platform, employee_id, MONDAY)

    assert NotificationType.ATTENDANCE_CLOCK_OUT in DIGEST_CANDIDATE_TYPES
    deliveries = await platform.sql(
        "SELECT d.channel, d.status, d.error FROM notification_deliveries d "
        "JOIN notifications n ON n.id = d.notification_id "
        "WHERE n.recipient_employee_id = :id ORDER BY d.channel",
        {"id": manager},
    )
    assert [(row[0], row[1]) for row in deliveries] == [("email", "pending"), ("inapp", "sent")]
    assert deliveries[0][2] is not None, "a pending row says why nothing has sent it"


async def test_the_clock_endpoint_notifies_the_manager(platform: Platform) -> None:
    """The wiring, through the real endpoint and the restricted role.

    A test of the notifier alone would pass while the endpoint built a bare
    service, which is precisely the mistake the decorator exists to make
    impossible.
    """
    employee = await platform.account(roles=("employee",))
    manager = await platform.account(roles=("employee",))
    await works_here(
        platform,
        code="OPS",
        employee_id=employee.employee_id,
        manager_employee_id=manager.employee_id,
    )

    for kind, instant in (("clock_in", at(MONDAY, 9)), ("clock_out", at(MONDAY, 17))):
        response = await employee.post(
            "/api/v1/attendance/clock", json={"kind": kind, "at": instant.isoformat()}
        )
        assert response.status_code == 201, response.text

    posted = await manager.get("/api/v1/notifications")
    assert posted.status_code == 200, posted.text
    items = posted.json()["items"]
    assert [item["type"] for item in items] == [NotificationType.ATTENDANCE_CLOCK_OUT.value]
    assert items[0]["payload"]["employee_id"] == employee.employee_id


# --- the morning reminder -----------------------------------------------------


async def test_the_morning_reminder_tells_the_employee_and_stamps_the_row(
    platform: Platform,
) -> None:
    """In the employee's own centre, a digest candidate, and stamped so a second
    run — or a restarted container — reminds nobody twice."""
    employee_id = await works_here(platform)
    async with attendance_service(platform) as service:
        await service.clock(employee_id, EventType.CLOCK_IN, at(MONDAY, 9), EventSource.WEB)

    async with reminder_pass(platform, now=FIXED_NOW) as (service, reminder):
        await service.scan(MONDAY)
        first = await reminder.remind(MONDAY)
        second = await reminder.remind(MONDAY)

    assert first.reminded_count == 1
    assert second.reminded_count == 0
    stamped = await rows(platform, MONDAY)
    assert stamped[0][3] == FIXED_NOW, "the row the reminder was raised for carries the stamp"

    async with notifications(platform) as centre:
        page = await centre.list_for(employee_id)

    assert page.total == 1
    raised = page.items[0]
    assert raised.type is NotificationType.ATTENDANCE_ANOMALY_REMINDER
    assert raised.title_key == "notifications.attendance.anomaly_reminder"
    assert raised.entity_type == "attendance_anomaly"
    assert raised.payload["anomaly_type"] == AnomalyType.MISSING_CLOCK_OUT.value
    assert raised.payload["business_date"] == MONDAY.isoformat()
    assert NotificationType.ATTENDANCE_ANOMALY_REMINDER in DIGEST_CANDIDATE_TYPES


async def test_a_day_with_no_anomalies_is_not_reminded(platform: Platform) -> None:
    """The ticket's last line, as a count over the notification table.

    A clean day has nothing to remind anybody about, and the pass must not send one
    "everything is fine" message per employee per day.
    """
    employee_id = await works_here(platform)
    await clocked(platform, employee_id, MONDAY)

    async with reminder_pass(platform) as (service, reminder):
        scanned = await service.scan(MONDAY)
        reminded = await reminder.remind(MONDAY)

    assert scanned.created_count == 0
    assert reminded.reminded_count == 0
    assert await platform.scalar("SELECT count(*) FROM notifications") == 0


async def test_a_resolved_anomaly_is_not_reminded(platform: Platform) -> None:
    """The one message the reminder must not send: make up a punch you have already
    made up. It is the same rule as the resolution itself, read from the queue."""
    employee_id = await works_here(platform)
    async with attendance_service(platform) as service:
        await service.clock(employee_id, EventType.CLOCK_IN, at(MONDAY, 9), EventSource.WEB)

    async with platform.factory() as session:
        repository = PostgresAttendanceRepository(session)
        makeup = await repository.append_event(
            NewEvent(
                employee_id=employee_id,
                event_type=EventType.CLOCK_OUT,
                occurred_at=at(MONDAY, 17),
                business_date=MONDAY,
                source=EventSource.CORRECTION,
            )
        )
        await repository.commit()

    async with reminder_pass(platform) as (service, reminder):
        await service.scan(MONDAY)
        await service.resolve_for_correction(employee_id, MONDAY, makeup.id)
        reminded = await reminder.remind(MONDAY)

    assert reminded.reminded_count == 0
    assert await platform.scalar("SELECT count(*) FROM notifications") == 0


# --- what a correction clears (ticket 24's caller) ---------------------------


async def test_a_correction_resolves_the_anomaly_the_day_no_longer_shows(
    platform: Platform,
) -> None:
    """Ticket 24 calls `resolve_for_correction` once its approval has appended the
    make-up event and recomputed the day.

    Nothing here builds that flow. The rule under test is that an anomaly the day no
    longer shows is resolved *by that event* — and, in the same transaction, that
    the anomalies the correction created are on the record: a make-up clock_in at
    11:00 answers the no-punches anomaly and creates a lateness, and a pass that
    only closed rows would lose it until somebody re-scanned the date.
    """
    employee_id = await works_here(platform)
    async with anomalies(platform) as service:
        await service.scan(MONDAY)
    assert [row[1] for row in await rows(platform, MONDAY)] == [
        AnomalyType.NO_PUNCHES.value
    ]

    # Ticket 24's make-up punch: an event with source=correction and no target,
    # because the punch it stands for never happened.
    async with platform.factory() as session:
        repository = PostgresAttendanceRepository(session)
        makeup = await repository.append_event(
            NewEvent(
                employee_id=employee_id,
                event_type=EventType.CLOCK_IN,
                occurred_at=at(MONDAY, 11),
                business_date=MONDAY,
                source=EventSource.CORRECTION,
            )
        )
        await repository.commit()

    async with anomalies(platform) as service:
        cleared = await service.resolve_for_correction(employee_id, MONDAY, makeup.id)
        stored = await service.day_anomalies(employee_id, MONDAY)

    assert [row.type for row in cleared] == [AnomalyType.NO_PUNCHES]
    assert cleared[0].resolved_by_event_id == makeup.id
    # The resolved row stays on the record — "this was flagged and here is what
    # cleared it" — and the two the correction created are open beside it.
    assert [row.type for row in stored] == [
        AnomalyType.LATE,
        AnomalyType.MISSING_CLOCK_OUT,
        AnomalyType.NO_PUNCHES,
    ]
    assert [row.is_resolved for row in stored] == [False, False, True]
    assert stored[2].resolved_by_event_id == makeup.id

    async with anomalies(platform) as service:
        again = await service.scan(MONDAY)
    assert again.created_count == 0 and again.existing == 2
    resolved = [row for row in await rows(platform, MONDAY) if row[4] is not None]
    assert len(resolved) == 1 and resolved[0][1] == AnomalyType.NO_PUNCHES.value
    assert resolved[0][4] == makeup.id, "a later scan leaves it resolved"


async def test_a_correction_that_leaves_the_day_late_resolves_nothing(
    platform: Platform,
) -> None:
    """The other half of the rule, and the half that would be tempting to get wrong.

    A correction that moves a clock_in from 09:30 to 09:45 does not clear the
    lateness — it is a lateness either way — and marking it resolved would put a
    false "somebody dealt with this" on the record the manager reads.
    """
    employee_id = await works_here(platform)
    async with attendance_service(platform) as service:
        await service.clock(employee_id, EventType.CLOCK_IN, at(TUESDAY, 9, 30), EventSource.WEB)
        await service.clock(employee_id, EventType.CLOCK_OUT, at(TUESDAY, 17), EventSource.WEB)

    async with anomalies(platform) as service:
        await service.scan(TUESDAY)
        assert [row.type for row in await service.day_anomalies(employee_id, TUESDAY)] == [
            AnomalyType.LATE
        ]

    async with platform.factory() as session:
        repository = PostgresAttendanceRepository(session)
        found = await repository.find_punch(employee_id, EventType.CLOCK_IN, at(TUESDAY, 9, 30))
        assert found is not None
        restated = await repository.append_event(
            NewEvent(
                employee_id=employee_id,
                event_type=EventType.CORRECTION,
                occurred_at=at(TUESDAY, 9, 45),
                business_date=TUESDAY,
                source=EventSource.CORRECTION,
                correction_of_event_id=found.id,
                reason="the door reader was fifteen minutes behind",
            )
        )
        await repository.commit()

    async with anomalies(platform) as service:
        cleared = await service.resolve_for_correction(employee_id, TUESDAY, restated.id)
        stored = await service.day_anomalies(employee_id, TUESDAY)

    assert cleared == ()
    assert [row.type for row in stored] == [AnomalyType.LATE]
    assert not stored[0].is_resolved


# --- the database's own rules -------------------------------------------------


@pytest.fixture
async def app_connection(settings: Settings) -> AsyncIterator[async_sessionmaker]:
    """Sessions bound to the restricted role, on the test database."""
    restricted = create_async_engine(settings.runtime_test_database_url)
    try:
        yield async_sessionmaker(bind=restricted, expire_on_commit=False)
    finally:
        await restricted.dispose()


async def test_the_anomaly_table_refuses_a_second_row_for_the_same_day_and_kind(
    platform: Platform,
) -> None:
    """The scan's idempotency is a constraint, not a convention.

    Written straight to the table, because the service's insert already handles the
    conflict: what is under test is that PostgreSQL refuses the row at all.
    """
    employee_id = await works_here(platform)
    async with anomalies(platform) as service:
        await service.scan(MONDAY)

    with pytest.raises(Exception) as refusal:
        await platform.sql(
            "INSERT INTO attendance_anomalies (id, employee_id, business_date, type) "
            "VALUES (gen_random_uuid(), :id, :day, 'no_punches')",
            {"id": employee_id, "day": MONDAY},
        )

    assert "uq_attendance_anomalies_day_type" in str(refusal.value)


async def test_an_anomaly_cannot_be_deleted_by_the_runtime_role(
    platform: Platform, app_connection: async_sessionmaker
) -> None:
    """A record of what was owed to whom, and the way to stop it being true is to
    correct the day rather than to remove the row."""
    await works_here(platform)
    async with anomalies(platform) as service:
        await service.scan(MONDAY)

    async with app_connection() as session:
        with pytest.raises(Exception) as refusal:
            await session.execute(text("DELETE FROM attendance_anomalies"))
        await session.rollback()

    assert "permission denied" in str(refusal.value).lower()
    assert len(await rows(platform, MONDAY)) == 1


# --- the job ------------------------------------------------------------------


def test_the_job_defaults_to_yesterday_in_madrid() -> None:
    """The pass runs after midnight, so the day it is about has just ended."""
    assert previous_day(TUESDAY) == MONDAY
    assert previous_day(date(2026, 1, 1)) == date(2025, 12, 31)


async def test_the_job_runs_both_phases_and_says_what_it_did(
    platform: Platform, capsys: pytest.CaptureFixture
) -> None:
    """Scan then remind, in one pass over a real database.

    The scan's half is asserted through the table and the reminder's through the
    stamp; the printed line is the operator's summary, so it is asserted too.
    """
    from app.jobs.scan_attendance_anomalies import main

    employee_id = await works_here(platform)
    async with attendance_service(platform) as service:
        await service.clock(employee_id, EventType.CLOCK_IN, at(MONDAY, 9), EventSource.WEB)

    assert await main([MONDAY.isoformat()]) == 0

    printed = capsys.readouterr().out
    assert MONDAY.isoformat() in printed
    assert "1 anomalies" in printed and "1 reminded" in printed
    stored = await rows(platform, MONDAY)
    assert len(stored) == 1 and stored[0][3] is not None

    # A second run of the same day is a no-op in both phases, which is the whole
    # reason one command runs them together.
    assert await main([MONDAY.isoformat()]) == 0
    assert "0 anomalies" in capsys.readouterr().out
    assert len(await rows(platform, MONDAY)) == 1
