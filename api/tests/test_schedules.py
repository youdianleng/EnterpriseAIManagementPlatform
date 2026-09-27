"""Work schedules, the holiday calendar, and the hours a month was expected to be.

No mocks and no in-memory repository, for the reason `test_attendance_events.py`
gives: half of what this module has to get right is a *query* — which override
covers a date, which department somebody was in, whether a holiday row applies to
them, whether the runtime role can rewrite a stored snapshot — and a substitute
would answer those with the test's own assumptions.

**The arithmetic is tested against a calendar somebody can check by hand.** March
2026 has twenty-two working days: five Mondays, five Tuesdays, four Wednesdays,
four Thursdays and four Fridays (the 1st is a Sunday). A week of eight hours Monday
to Thursday and six on Friday is therefore 18 × 480 + 4 × 360 = 10,080 minutes, and
every assertion below that mentions a month is one of those numbers. A test whose
expected value was itself computed by the code under test would prove nothing.

**The snapshot tests are the ticket's real subject.** A figure that changes when a
schedule is edited next year is not evidence, so those tests read the stored row
back out of PostgreSQL after the edit, and the month-end pass is run twice to show
it appends nothing the second time.
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app
from app.config import Settings
from app.core.errors import ErrorCode
from app.domain.access.kernel import Reason, Resource, ResourceKind, can
from app.domain.access.permissions import Action
from app.domain.access.principal import SYSTEM_ROLES, Principal
from app.domain.attendance.models import (
    DayStatus,
    EventSource,
    EventType,
    utc_now,
)
from app.domain.attendance.service import AttendanceService
from app.domain.errors import DomainError
from app.domain.schedule.calculation import (
    department_on,
    month_dates,
    most_specific,
    validate_day,
    weekly_hours_of,
)
from app.domain.schedule.errors import ScheduleErrorCode
from app.domain.schedule.importer import parse_holidays_csv, read_holidays_csv
from app.domain.schedule.models import (
    AssignmentSpan,
    Holiday,
    HolidayInput,
    HolidayScope,
    OverrideInput,
    ScheduleDayInput,
    ScheduleInput,
    SchedulePatch,
    ScheduleSource,
    WorkSchedule,
)
from app.domain.schedule.service import ScheduleService
from app.jobs.import_holidays import import_file
from app.repositories.attendance import PostgresAttendanceRepository
from app.repositories.schedule import PostgresScheduleRepository
from tests.support.platform import Platform

#: The worked example the module ships, which is also the import's format test.
SAMPLE_CSV = Path(app.__file__).resolve().parent / "data" / "holidays_madrid_2026.csv"

MARCH = (2026, 3)

#: March 2026, worked out from the calendar: the 1st is a Sunday, so there are five
#: Mondays and five Tuesdays and four of everything else.
MARCH_WORKING_DAYS = 22
MARCH_FRIDAYS = 4
MARCH_MONDAY_TO_THURSDAY = 18

#: The two patterns the ticket names. "Monday to Thursday eight hours, Friday six"
#: is the intensivo week; the plain one is five equal days.
FULL_DAY = 480
SHORT_FRIDAY = 360

FULL_TIME_MARCH = MARCH_WORKING_DAYS * FULL_DAY  # 10,560
INTENSIVO_MARCH = MARCH_MONDAY_TO_THURSDAY * FULL_DAY + MARCH_FRIDAYS * SHORT_FRIDAY  # 10,080
PART_TIME_MARCH = MARCH_WORKING_DAYS * (FULL_DAY // 2)  # 5,280
INTENSIVO_PART_TIME_MARCH = 18 * 240 + 4 * 180  # 5,040, exactly half of 10,080

#: A Thursday in the middle of the month, and a Friday before it. Chosen because a
#: holiday on one of them moves exactly one day's minutes and nothing else.
MARCH_THURSDAY = date(2026, 3, 19)
MARCH_FRIDAY = date(2026, 3, 13)
MARCH_SATURDAY = date(2026, 3, 21)

MADRID_REGION = "ES-MD"
VALENCIA_REGION = "ES-VC"


def day(
    weekday: int,
    minutes: int,
    start: int | None = 8,
    end: int | None = 16,
    break_minutes: int = 0,
) -> ScheduleDayInput:
    def moment(hour: int | None):  # noqa: ANN202 - time | None
        return None if hour is None else time(hour, 0)

    return ScheduleDayInput(
        weekday=weekday,
        expected_minutes=minutes,
        start_time=moment(start),
        end_time=moment(end),
        break_minutes=break_minutes,
    )


def full_week(minutes: int = FULL_DAY) -> tuple[ScheduleDayInput, ...]:
    """Monday to Friday, the same every day, ending when the minutes say."""
    end = 8 + minutes // 60
    return tuple(day(weekday, minutes, 8, end) for weekday in range(5))


def intensivo_week() -> tuple[ScheduleDayInput, ...]:
    """The Spanish short Friday: eight hours Monday to Thursday, six on Friday."""
    return (*[day(weekday, FULL_DAY) for weekday in range(4)], day(4, SHORT_FRIDAY, 8, 14))


def intensivo_part_time_week() -> tuple[ScheduleDayInput, ...]:
    """The same shape at half the hours, which is what a 兼职 override looks like."""
    return (*[day(weekday, 240, 8, 12) for weekday in range(4)], day(4, 180, 8, 11))


@asynccontextmanager
async def schedule_service(
    platform: Platform, *, now: datetime | None = None
) -> AsyncIterator[ScheduleService]:
    """The service on its own session, the way one request uses it."""
    async with platform.factory() as session:
        yield ScheduleService(
            PostgresScheduleRepository(session), session, now=(lambda: now) if now else None
        )


@asynccontextmanager
async def attendance_service(
    platform: Platform, *, now: datetime | None = None
) -> AsyncIterator[AttendanceService]:
    """The attendance module, wired to the scheduling one exactly as the API does."""
    clock = utc_now if now is None else (lambda: now)
    async with platform.factory() as session:
        yield AttendanceService(
            PostgresAttendanceRepository(session),
            now=clock,
            expectations=ScheduleService(PostgresScheduleRepository(session), session),
        )


async def add_schedule(
    platform: Platform,
    *,
    code: str,
    days: tuple[ScheduleDayInput, ...],
    department_id: UUID | str | None = None,
    is_default: bool = False,
) -> WorkSchedule:
    async with schedule_service(platform) as service:
        return await service.create_schedule(
            ScheduleInput(
                code=code,
                name_es=code,
                name_en=code,
                days=days,
                department_id=UUID(str(department_id)) if department_id else None,
                is_default=is_default,
            )
        )


async def change_schedule(
    platform: Platform, schedule_id: UUID | str, days: tuple[ScheduleDayInput, ...]
) -> WorkSchedule:
    async with schedule_service(platform) as service:
        return await service.update_schedule(
            UUID(str(schedule_id)), SchedulePatch(days=days)
        )


async def add_holiday(
    platform: Platform,
    on_date: date,
    *,
    scope: HolidayScope = HolidayScope.NATIONAL,
    region_code: str | None = None,
    name: str = "Festivo",
) -> Holiday:
    async with schedule_service(platform) as service:
        return await service.add_holiday(
            HolidayInput(
                date=on_date,
                name_es=name,
                name_en=name,
                scope=scope,
                region_code=region_code,
            )
        )


async def month_minutes(platform: Platform, employee_id: UUID, year: int, month: int) -> int:
    async with schedule_service(platform) as service:
        return (await service.expected_minutes(employee_id, year, month)).expected_minutes


async def region_employee(
    platform: Platform, *, code: str, region: str | None
) -> tuple[UUID, str]:
    """An employee in a department that works in one region.

    The region is set through the departments endpoint, which is where ticket 22
    reads it from — a per-person field was rejected, so there is deliberately no
    other way to state it.
    """
    department = await platform.department(code, region_code=region)
    position = await platform.position(department, f"{code}p")
    employee_id = UUID(await platform.employee())
    await platform.assign(employee_id, department, position)
    return employee_id, department


async def attendance_employee(
    platform: Platform, *, code: str, region: str | None = MADRID_REGION
) -> UUID:
    employee_id, _ = await region_employee(platform, code=code, region=region)
    return employee_id


async def worked(platform: Platform, employee_id: UUID, on_date: date, start: int, end: int):
    """A complete shift on one day, through the attendance write path."""
    zone = datetime(on_date.year, on_date.month, on_date.day, tzinfo=UTC)
    async with attendance_service(platform) as service:
        await service.clock(
            employee_id,
            EventType.CLOCK_IN,
            zone.replace(hour=start),
            EventSource.WEB,
        )
        await service.clock(
            employee_id,
            EventType.CLOCK_OUT,
            zone.replace(hour=end),
            EventSource.WEB,
        )


async def day_status(
    platform: Platform, employee_id: UUID, on_date: date
) -> tuple[str, int | None]:
    async with attendance_service(platform) as service:
        record = await service.recompute_day(employee_id, on_date)
    return str(record.status), record.expected_minutes


def own_resource(employee_id: UUID) -> Resource:
    return Resource(ResourceKind.EMPLOYEE, owner_employee_id=employee_id)


# --- the pattern, purely ----------------------------------------------------


def test_a_day_must_state_a_window_its_own_minutes_agree_with() -> None:
    """The rule the CHECK constraint states, in the caller's vocabulary.

    Validated rather than derived: a window that does not add up is a mistake in the
    data, and a service that silently derived the minutes from the window would
    store a day nobody agreed to.
    """
    validate_day(day(0, FULL_DAY, 8, 16))
    validate_day(day(0, 420, 9, 17, break_minutes=60))
    validate_day(day(5, 0, None, None))

    with pytest.raises(DomainError) as mismatch:
        validate_day(day(0, FULL_DAY, 9, 14))
    assert mismatch.value.code is ScheduleErrorCode.SCHEDULE_INVALID_DAY
    assert "not 480" in (mismatch.value.detail or "")

    with pytest.raises(DomainError):
        # A rest day with a window is two answers to one question.
        validate_day(day(5, 0, 9, 14))

    with pytest.raises(DomainError):
        # Seconds would make the window and the minutes agree only by rounding.
        validate_day(
            ScheduleDayInput(
                weekday=0,
                expected_minutes=480,
                start_time=time(8, 0, 30),
                end_time=time(16, 0, 30),
            )
        )

    with pytest.raises(DomainError):
        # A shift that ends before it starts is a night shift, and this system
        # counts those as two days.
        validate_day(day(0, 480, 22, 6))


def test_a_week_may_differ_from_day_to_day_and_its_hours_are_derived() -> None:
    """"Monday to Thursday 8 hours, Friday 6" — the intensivo case, in one place."""
    days = (*[day(weekday, FULL_DAY) for weekday in range(4)], day(4, SHORT_FRIDAY, 8, 14))

    assert weekly_hours_of(days) == Decimal("38.00")
    assert weekly_hours_of(full_week()) == Decimal("40.00")
    # A week that is not a whole number of hours does not get rounded away.
    assert weekly_hours_of((day(0, 450, 8, 15, 30),)) == Decimal("7.50")


def test_a_month_is_every_date_in_it() -> None:
    assert len(month_dates(2026, 3)) == 31
    assert month_dates(2026, 3)[0] == date(2026, 3, 1)
    assert month_dates(2026, 3)[-1] == date(2026, 3, 31)
    assert len(month_dates(2028, 2)) == 29, "a leap year is not special-cased anywhere"
    with pytest.raises(DomainError):
        month_dates(2026, 13)


def test_a_holiday_applies_by_region_and_the_scope_only_labels_it() -> None:
    """The whole matching rule: no region means everybody, a region means theirs."""
    national = Holiday(
        id=uuid.uuid4(),
        date=MARCH_FRIDAY,
        name_es="Nacional",
        name_en="National",
        scope=HolidayScope.NATIONAL,
        year=2026,
    )
    regional = Holiday(
        id=uuid.uuid4(),
        date=MARCH_FRIDAY,
        name_es="Autonómico",
        name_en="Regional",
        scope=HolidayScope.REGIONAL,
        year=2026,
        region_code=MADRID_REGION,
    )

    assert national.applies_to(None) and national.applies_to(VALENCIA_REGION)
    assert regional.applies_to(MADRID_REGION)
    assert not regional.applies_to(VALENCIA_REGION)
    # Nobody's region: only the national calendar, which is the honest answer
    # rather than a guess at where somebody works.
    assert not regional.applies_to(None)
    assert most_specific([national, regional]) is regional, "the more specific one is named"


def test_the_fallback_chain_is_override_then_department_then_default() -> None:
    """The resolution order, as the signature of one function."""
    from app.domain.schedule.calculation import resolve_schedule

    def schedule(code: str) -> WorkSchedule:
        return WorkSchedule(
            id=uuid.uuid4(),
            code=code,
            name_es=code,
            name_en=code,
            weekly_hours=Decimal("40.00"),
            is_default=False,
            is_active=True,
            days=tuple(day(weekday, FULL_DAY) for weekday in range(5)),
        )

    override, department, company = schedule("o"), schedule("d"), schedule("c")
    override_id = uuid.uuid4()

    assert resolve_schedule(
        override_schedule=override,
        department_schedule=department,
        default_schedule=company,
        override_id=override_id,
    ) == resolve_schedule(
        override_schedule=override,
        department_schedule=department,
        default_schedule=company,
        override_id=override_id,
    )
    resolved = resolve_schedule(
        override_schedule=override,
        department_schedule=department,
        default_schedule=company,
        override_id=override_id,
    )
    assert resolved is not None
    assert (resolved.source, resolved.override_id) == (ScheduleSource.OVERRIDE, override_id)

    fallback = resolve_schedule(
        override_schedule=None, department_schedule=department, default_schedule=company
    )
    assert fallback is not None and fallback.source is ScheduleSource.DEPARTMENT

    default = resolve_schedule(
        override_schedule=None, department_schedule=None, default_schedule=company
    )
    assert default is not None and default.source is ScheduleSource.DEFAULT

    # Nobody has configured anything: not the same fact as a schedule of zeroes.
    assert (
        resolve_schedule(
            override_schedule=None, department_schedule=None, default_schedule=None
        )
        is None
    )


def test_where_somebody_worked_is_the_primary_assignment_covering_the_date() -> None:
    first, second = uuid.uuid4(), uuid.uuid4()
    spans = [
        AssignmentSpan(
            department_id=first,
            start_date=date(2024, 1, 15),
            end_date=date(2026, 3, 15),
            is_primary=True,
        ),
        AssignmentSpan(
            department_id=second,
            start_date=date(2026, 3, 16),
            is_primary=False,
        ),
    ]

    assert department_on(spans, date(2026, 3, 13)) == first
    assert department_on(spans, date(2026, 3, 16)) == second, "the later span is the only one"
    assert department_on([], date(2026, 3, 13)) is None


# --- the import file --------------------------------------------------------


def test_the_shipped_madrid_sample_is_a_calendar_hr_can_start_from() -> None:
    rows = read_holidays_csv(SAMPLE_CSV)
    by_scope: dict[str, int] = {}
    for row in rows:
        by_scope[row.scope.value] = by_scope.get(row.scope.value, 0) + 1

    assert by_scope == {"national": 8, "regional": 2, "local": 2}
    assert all(row.date.year == 2026 for row in rows)
    assert {row.region_code for row in rows if row.scope is HolidayScope.NATIONAL} == {None}


def test_a_calendar_file_with_a_bad_row_is_refused_whole_and_says_which() -> None:
    """Nothing half-loaded: a partly imported calendar is a partly wrong figure."""
    header = "date,name_es,name_en,scope,region_code"
    bad_scope = parse_holidays_csv  # the name is long; the call is below

    with pytest.raises(DomainError) as no_header:
        bad_scope("2026-01-01,Año Nuevo,New Year,national,\n")
    assert no_header.value.code is ScheduleErrorCode.SCHEDULE_INVALID_HOLIDAY_FILE
    assert "header" in (no_header.value.detail or "")

    with pytest.raises(DomainError) as wrong:
        bad_scope(
            f"{header}\n"
            "2026-01-01,Año Nuevo,New Year,bank,\n"
            "2026-04-02,Jueves Santo,Maundy Thursday,regional,\n"
            "2026-05-15,San Isidro,San Isidro,local,ES-MD\n"
        )
    detail = wrong.value.detail or ""
    assert "line 2" in detail and "line 3" in detail, detail
    assert "region" in detail

    with pytest.raises(DomainError) as duplicated:
        bad_scope(
            f"{header}\n"
            "2026-01-01,Año Nuevo,New Year,national,\n"
            "2026-01-01,Año Nuevo (otra vez),New Year,national,\n"
        )
    assert "already on line 2" in (duplicated.value.detail or "")


# --- resolution over a real database ----------------------------------------


async def test_the_company_default_applies_where_no_department_has_said_otherwise(
    platform: Platform,
) -> None:
    employee_id = await attendance_employee(platform, code="default")
    await add_schedule(platform, code="estandar", days=full_week(), is_default=True)

    async with schedule_service(platform) as service:
        expectation = await service.day_expectation(employee_id, date(2026, 3, 18))

    assert expectation.source is ScheduleSource.DEFAULT
    assert expectation.expected_minutes == FULL_DAY
    assert expectation.is_working_day


async def test_a_department_schedule_wins_over_the_company_default(platform: Platform) -> None:
    employee_id, department = await region_employee(platform, code="tienda", region=MADRID_REGION)
    await add_schedule(platform, code="estandar", days=full_week(), is_default=True)
    await add_schedule(
        platform, code="intensivo", days=intensivo_week(), department_id=department
    )

    async with schedule_service(platform) as service:
        monday = await service.day_expectation(employee_id, date(2026, 3, 16))
        friday = await service.day_expectation(employee_id, date(2026, 3, 20))

    assert monday.source is ScheduleSource.DEPARTMENT
    assert (monday.expected_minutes, friday.expected_minutes) == (FULL_DAY, SHORT_FRIDAY)
    assert await month_minutes(platform, employee_id, *MARCH) == INTENSIVO_MARCH


async def test_an_override_inside_its_window_wins_and_outside_it_does_not(
    platform: Platform,
) -> None:
    """The window is the whole point: the same person, two answers, one month."""
    employee_id, department = await region_employee(
        platform, code="logistica", region=MADRID_REGION
    )
    await add_schedule(
        platform, code="intensivo", days=intensivo_week(), department_id=department
    )
    part_time = await add_schedule(platform, code="parcial", days=full_week(FULL_DAY // 2))

    async with schedule_service(platform) as service:
        await service.set_override(
            OverrideInput(
                employee_id=employee_id,
                schedule_id=part_time.id,
                effective_from=date(2026, 3, 16),
                effective_to=date(2026, 3, 20),
                reason="Jornada parcial hasta final de mes",
            )
        )

    async with schedule_service(platform) as service:
        before = await service.day_expectation(employee_id, date(2026, 3, 13))
        inside = await service.day_expectation(employee_id, date(2026, 3, 18))
        after = await service.day_expectation(employee_id, date(2026, 3, 25))

    assert (before.source, before.expected_minutes) == (ScheduleSource.DEPARTMENT, SHORT_FRIDAY)
    assert (inside.source, inside.expected_minutes) == (ScheduleSource.OVERRIDE, FULL_DAY // 2)
    assert (after.source, after.expected_minutes) == (
        ScheduleSource.DEPARTMENT,
        FULL_DAY,
    ), "the override stopped on the 20th"

    # A week of the department's pattern, three days of the part-time one, and the
    # rest of the month back on the department's.
    expected = (
        INTENSIVO_MARCH
        - FULL_DAY  # Monday 16th
        - SHORT_FRIDAY  # Friday 20th
        - 2 * FULL_DAY  # Wednesday 18th and Thursday 19th
        + 3 * (FULL_DAY // 2)
    )
    assert await month_minutes(platform, employee_id, *MARCH) == expected


async def test_a_part_time_override_halves_the_month(platform: Platform) -> None:
    """The 兼职 case the ticket names, at exactly half."""
    employee_id, department = await region_employee(platform, code="obrador", region=MADRID_REGION)
    await add_schedule(
        platform, code="intensivo", days=intensivo_week(), department_id=department
    )
    half = await add_schedule(
        platform, code="parcial", days=intensivo_part_time_week()
    )

    full_time = await month_minutes(platform, employee_id, *MARCH)

    async with schedule_service(platform) as service:
        await service.set_override(
            OverrideInput(
                employee_id=employee_id,
                schedule_id=half.id,
                effective_from=date(2026, 1, 1),
                reason="Contrato a jornada parcial",
            )
        )

    part_time = await month_minutes(platform, employee_id, *MARCH)

    assert full_time == INTENSIVO_MARCH
    assert part_time == INTENSIVO_PART_TIME_MARCH
    assert part_time * 2 == full_time


async def test_the_region_follows_the_department_somebody_worked_in_that_day(
    platform: Platform,
) -> None:
    """A transfer mid-month changes which holidays apply, from the day it happens.

    Read from the assignment in force on the date rather than from a field on the
    person: the region is a fact about a workplace, and a per-person copy would
    eventually contradict the building somebody sits in.
    """
    madrid = await platform.department("madrid", region_code=MADRID_REGION)
    valencia = await platform.department("valencia", region_code=VALENCIA_REGION)
    madrid_position = await platform.position(madrid, "md")
    valencia_position = await platform.position(valencia, "vc")
    employee_id = UUID(await platform.employee())
    await platform.assign(
        employee_id, madrid, madrid_position, start_date="2024-01-15", end_date="2026-03-15"
    )
    await platform.assign(employee_id, valencia, valencia_position, start_date="2026-03-16")

    await add_schedule(platform, code="estandar", days=full_week(), is_default=True)
    await add_holiday(
        platform,
        MARCH_FRIDAY,
        scope=HolidayScope.REGIONAL,
        region_code=MADRID_REGION,
        name="Fiesta de Madrid",
    )
    await add_holiday(
        platform,
        date(2026, 3, 17),
        scope=HolidayScope.REGIONAL,
        region_code=VALENCIA_REGION,
        name="Fiesta de Valencia",
    )

    async with schedule_service(platform) as service:
        before = await service.day_expectation(employee_id, MARCH_FRIDAY)
        after = await service.day_expectation(employee_id, date(2026, 3, 17))

    assert before.region_code == MADRID_REGION
    assert before.is_holiday and before.expected_minutes == 0
    assert after.region_code == VALENCIA_REGION
    assert after.is_holiday and after.expected_minutes == 0
    # Each region's holiday is invisible to the other half of the month.
    assert await month_minutes(platform, employee_id, *MARCH) == FULL_TIME_MARCH - 2 * FULL_DAY


async def test_somebody_whose_department_names_no_region_gets_the_national_calendar(
    platform: Platform,
) -> None:
    employee_id, _ = await region_employee(platform, code="sede", region=None)
    await add_schedule(platform, code="estandar", days=full_week(), is_default=True)
    await add_holiday(
        platform,
        MARCH_FRIDAY,
        scope=HolidayScope.LOCAL,
        region_code=MADRID_REGION,
        name="San Isidro",
    )

    async with schedule_service(platform) as service:
        expectation = await service.day_expectation(employee_id, MARCH_FRIDAY)

    assert expectation.region_code is None
    assert not expectation.is_holiday
    assert expectation.expected_minutes == FULL_DAY


async def test_overlapping_overrides_are_refused_by_the_service_and_by_the_database(
    platform: Platform,
) -> None:
    """Two windows covering one day would make "the rules in March" undecidable."""
    employee_id = await attendance_employee(platform, code="duplicado")
    first = await add_schedule(platform, code="uno", days=full_week())
    second = await add_schedule(platform, code="dos", days=intensivo_week())
    override_id = uuid.uuid4()

    await platform.sql(
        "INSERT INTO employee_schedule_overrides "
        "(id, employee_id, schedule_id, effective_from, effective_to, reason) "
        "VALUES (:id, :employee, :schedule, DATE '2026-03-01', DATE '2026-03-15', 'primera')",
        {"id": override_id, "employee": employee_id, "schedule": first.id},
    )

    async with schedule_service(platform) as service:
        with pytest.raises(DomainError) as refused:
            await service.set_override(
                OverrideInput(
                    employee_id=employee_id,
                    schedule_id=second.id,
                    effective_from=date(2026, 3, 15),
                    reason="se solapa el último día",
                )
            )
    assert refused.value.code is ScheduleErrorCode.SCHEDULE_OVERRIDE_OVERLAPS

    # And the database refuses it too, which is what holds when two administrators
    # press the button at the same moment.
    with pytest.raises(Exception) as constraint:
        await platform.sql(
            "INSERT INTO employee_schedule_overrides "
            "(id, employee_id, schedule_id, effective_from, effective_to, reason) "
            "VALUES (gen_random_uuid(), :employee, :schedule, DATE '2026-03-10', NULL, 'solape')",
            {"employee": employee_id, "schedule": second.id},
        )
    assert "ex_employee_schedule_overrides_window" in str(constraint.value)


async def test_one_active_schedule_per_department_and_one_default(platform: Platform) -> None:
    """Resolution has exactly one answer, and this is where a second is refused."""
    department = await platform.department("unico", region_code=MADRID_REGION)
    await add_schedule(platform, code="primero", days=full_week(), department_id=department)
    await add_schedule(platform, code="empresa", days=full_week(), is_default=True)

    async with schedule_service(platform) as service:
        with pytest.raises(DomainError) as second_department:
            await service.create_schedule(
                ScheduleInput(
                    code="segundo",
                    name_es="Segundo",
                    name_en="Second",
                    days=full_week(),
                    department_id=UUID(department),
                )
            )
        with pytest.raises(DomainError) as second_default:
            await service.create_schedule(
                ScheduleInput(
                    code="empresa2",
                    name_es="Empresa",
                    name_en="Company",
                    days=full_week(),
                    is_default=True,
                )
            )
        with pytest.raises(DomainError) as taken_code:
            await service.create_schedule(
                ScheduleInput(code="primero", name_es="x", name_en="x", days=full_week())
            )

    assert second_department.value.code is ScheduleErrorCode.SCHEDULE_ALREADY_SET
    assert second_default.value.code is ScheduleErrorCode.SCHEDULE_ALREADY_SET
    assert taken_code.value.code is ScheduleErrorCode.SCHEDULE_CODE_TAKEN

    # A deactivated pattern frees the scope, which is how a season change is made.
    async with schedule_service(platform) as service:
        first = await service.list_schedules(include_inactive=True)
        current = next(item for item in first if item.code == "primero")
        await service.update_schedule(current.id, SchedulePatch(is_active=False))
        replacement = await service.create_schedule(
            ScheduleInput(
                code="verano",
                name_es="Verano",
                name_en="Summer",
                days=intensivo_week(),
                department_id=UUID(department),
            )
        )
    assert replacement.weekly_hours == Decimal("38.00"), "derived from the days"


async def test_resolve_names_the_row_that_won(platform: Platform) -> None:
    """The ticket's "resolve a person's schedule for a date", with the *why*.

    `source` and `override_id` are the answer to the question a reader actually
    asks — "why is Friday six hours" — and they are the reason resolution returns a
    value object rather than a schedule.
    """
    employee_id, department = await region_employee(platform, code="cual", region=MADRID_REGION)
    await add_schedule(
        platform, code="intensivo", days=intensivo_week(), department_id=department
    )
    part_time = await add_schedule(platform, code="parcial", days=full_week(240))

    async with schedule_service(platform) as service:
        before = await service.resolve(employee_id, date(2026, 3, 13))
        override = await service.set_override(
            OverrideInput(
                employee_id=employee_id,
                schedule_id=part_time.id,
                effective_from=date(2026, 3, 16),
                reason="Jornada parcial",
            )
        )
        inside = await service.resolve(employee_id, date(2026, 3, 18))
        nobody = await service.resolve(UUID(await platform.employee()), date(2026, 3, 18))

    assert before is not None
    assert (before.source, before.override_id) == (ScheduleSource.DEPARTMENT, None)
    assert before.schedule.code == "intensivo"
    assert before.minutes_on(4) == SHORT_FRIDAY

    assert inside is not None
    assert (inside.source, inside.override_id) == (ScheduleSource.OVERRIDE, override.id)
    assert inside.schedule.code == "parcial"
    assert inside.minutes_on(0) == 240

    assert nobody is None, "no schedule reaches somebody with no assignment and no default"


# --- the month's expected hours ---------------------------------------------


async def test_a_month_matches_the_calendar_day_by_day(platform: Platform) -> None:
    """March 2026, added up by hand: 18 long days and 4 short Fridays."""
    employee_id, department = await region_employee(platform, code="planta", region=MADRID_REGION)
    await add_schedule(
        platform, code="intensivo", days=intensivo_week(), department_id=department
    )

    async with schedule_service(platform) as service:
        month = await service.expected_minutes(employee_id, *MARCH)

    assert month.expected_minutes == INTENSIVO_MARCH
    assert not month.is_snapshot, "nothing has frozen it yet"
    assert len(month.days) == 31
    assert sum(1 for item in month.days if item.is_working_day) == MARCH_WORKING_DAYS
    assert [item.expected_minutes for item in month.days if item.weekday == 4] == [
        SHORT_FRIDAY
    ] * MARCH_FRIDAYS
    assert month.inputs["total_minutes"] == INTENSIVO_MARCH
    # Weekends and the two days after the 30th are in the answer as zeroes rather
    # than missing: a calendar with holes in it is not a calendar.
    assert len(month.inputs["days"]) == 31


async def test_a_holiday_added_through_the_api_changes_march(platform: Platform) -> None:
    """The checklist's own test: no date in Python, and the figure moves."""
    employee_id, department = await region_employee(platform, code="taller", region=MADRID_REGION)
    await add_schedule(
        platform, code="intensivo", days=intensivo_week(), department_id=department
    )

    before = await month_minutes(platform, employee_id, *MARCH)

    admin = await platform.account(roles=("admin",))
    response = await admin.post(
        "/api/v1/holidays",
        json={
            "date": MARCH_THURSDAY.isoformat(),
            "name_es": "Fiesta local",
            "name_en": "Local holiday",
            "scope": "national",
        },
    )
    assert response.status_code == 201, response.text

    after = await month_minutes(platform, employee_id, *MARCH)

    assert before == INTENSIVO_MARCH
    assert after == INTENSIVO_MARCH - FULL_DAY, "a Thursday of eight hours came out"
    assert before - after == FULL_DAY

    # And the same read again, with the year's rows already cached, still sees it:
    # the cache key is derived from the table, so the edit reaches the next read
    # rather than the next TTL.
    assert await month_minutes(platform, employee_id, *MARCH) == after


async def test_editing_a_holiday_out_of_band_reaches_the_next_read(platform: Platform) -> None:
    """The cache cannot go stale, because nothing has to remember to invalidate it.

    The write here bypasses the API entirely — straight into PostgreSQL, the way the
    import command in another process does — and the next read still sees it. That
    is the property `schedule/cache.py` argues for: a derived stamp rather than a
    counter somebody has to bump.

    The pattern is the intensivo one, so moving the holiday from a Thursday to a
    Friday changes the month's total: the figure is evidence that the second read
    used the new row rather than a cached copy of the old one.
    """
    employee_id, department = await region_employee(platform, code="cache", region=MADRID_REGION)
    await add_schedule(
        platform, code="intensivo", days=intensivo_week(), department_id=department
    )
    holiday = await add_holiday(platform, MARCH_THURSDAY)

    assert await month_minutes(platform, employee_id, *MARCH) == INTENSIVO_MARCH - FULL_DAY

    await platform.sql(
        "UPDATE holidays SET name_es = 'Fiesta movida', date = DATE '2026-03-20' WHERE id = :id",
        {"id": holiday.id},
    )

    assert (
        await month_minutes(platform, employee_id, *MARCH)
        == INTENSIVO_MARCH - SHORT_FRIDAY
    ), "the 19th is a working day again and the short Friday is not"

    async with schedule_service(platform) as service:
        thursday = await service.day_expectation(employee_id, MARCH_THURSDAY)
        friday = await service.day_expectation(employee_id, date(2026, 3, 20))

    assert not thursday.is_holiday and thursday.expected_minutes == FULL_DAY
    assert friday.is_holiday and friday.holiday is not None
    assert friday.holiday.name_es == "Fiesta movida"


async def test_a_holiday_that_is_deleted_stops_being_one(platform: Platform) -> None:
    employee_id, _ = await region_employee(platform, code="borrado", region=MADRID_REGION)
    await add_schedule(platform, code="estandar", days=full_week(), is_default=True)
    holiday = await add_holiday(platform, MARCH_THURSDAY)

    assert await month_minutes(platform, employee_id, *MARCH) == FULL_TIME_MARCH - FULL_DAY

    async with schedule_service(platform) as service:
        await service.delete_holiday(holiday.id)

    assert await month_minutes(platform, employee_id, *MARCH) == FULL_TIME_MARCH


async def test_importing_the_shipped_sample_writes_the_year(platform: Platform) -> None:
    """Through the command's own function, against a real database."""
    employee_id, _ = await region_employee(platform, code="importado", region=MADRID_REGION)
    await add_schedule(platform, code="estandar", days=full_week(), is_default=True)

    first = await import_file(SAMPLE_CSV)
    again = await import_file(SAMPLE_CSV)

    assert first == {"created": 12, "updated": 0, "unchanged": 0, "total": 12}
    assert again == {"created": 0, "updated": 0, "unchanged": 12, "total": 12}, (
        "re-importing a corrected calendar must not duplicate it"
    )

    # March has no Madrid holiday in the sample, so the month is untouched; the two
    # April ones and the two May ones are where the file's effect shows. April 2026
    # has twenty-two working days and two holidays in Madrid (Jueves and Viernes
    # Santo); May has twenty-one and two that land on one (the 1st and San Isidro,
    # the 2nd being a Saturday).
    async with schedule_service(platform) as service:
        may = await service.expected_minutes(employee_id, 2026, 5)
        april = await service.expected_minutes(employee_id, 2026, 4)

    assert april.expected_minutes == 20 * FULL_DAY
    assert may.expected_minutes == 19 * FULL_DAY


# --- the snapshot -----------------------------------------------------------


async def test_the_month_is_snapshotted_with_the_rules_that_produced_it(
    platform: Platform,
) -> None:
    employee_id, department = await region_employee(platform, code="archivo", region=MADRID_REGION)
    schedule = await add_schedule(
        platform, code="intensivo", days=intensivo_week(), department_id=department
    )
    holiday = await add_holiday(platform, MARCH_THURSDAY)

    async with schedule_service(platform) as service:
        month = await service.snapshot_month(employee_id, *MARCH)

    assert month.is_snapshot and month.snapshot is not None
    assert month.snapshot.revision == 1
    assert month.expected_minutes == INTENSIVO_MARCH - FULL_DAY

    stored = await platform.sql(
        "SELECT expected_minutes, inputs FROM expected_hours_snapshots WHERE id = :id",
        {"id": month.snapshot.id},
    )
    assert stored[0][0] == month.expected_minutes
    inputs = stored[0][1]
    assert inputs["total_minutes"] == month.expected_minutes
    assert [item["id"] for item in inputs["holidays"]] == [str(holiday.id)]
    assert inputs["region_codes"] == [MADRID_REGION]
    # The schedule, the window and the source per day: a reader four years from now
    # can see what each day was measured against without the schedule table.
    thursday = next(item for item in inputs["days"] if item["date"] == MARCH_THURSDAY.isoformat())
    assert thursday["holiday"]["name_es"] == holiday.name_es
    assert thursday["expected_minutes"] == 0
    wednesday = next(item for item in inputs["days"] if item["date"] == "2026-03-18")
    assert wednesday["schedule_id"] == str(schedule.id)
    assert wednesday["source"] == "department"
    assert (wednesday["start_time"], wednesday["end_time"]) == ("08:00", "16:00")


async def test_changing_the_schedule_leaves_a_stored_snapshot_untouched(
    platform: Platform,
) -> None:
    """The ticket's evidence rule, and the reason the table is append-only.

    A month that has been written down answers with what was written, whatever the
    schedule says now. The second half of the test is the control: recomputing
    deliberately *does* see the new pattern, as a new revision, with the old one
    still readable beside it — so the first assertion is the snapshot working rather
    than the schedule edit failing to happen.
    """
    employee_id, department = await region_employee(
        platform, code="historial", region=MADRID_REGION
    )
    schedule = await add_schedule(
        platform, code="intensivo", days=intensivo_week(), department_id=department
    )

    async with schedule_service(platform) as service:
        frozen = await service.snapshot_month(employee_id, *MARCH)
    assert frozen.expected_minutes == INTENSIVO_MARCH
    assert frozen.snapshot is not None
    snapshot_id = frozen.snapshot.id
    columns = "expected_minutes, inputs, revision"
    before = await platform.sql(
        f"SELECT {columns} FROM expected_hours_snapshots WHERE id = :id", {"id": snapshot_id}
    )

    # Next year, HR shortens the week.
    await change_schedule(platform, schedule.id, full_week())

    after = await platform.sql(
        f"SELECT {columns} FROM expected_hours_snapshots WHERE id = :id", {"id": snapshot_id}
    )
    assert after == before, "the stored snapshot was rewritten by a schedule change"
    assert await month_minutes(platform, employee_id, *MARCH) == INTENSIVO_MARCH, (
        "the stored figure is the answer, not a recomputation"
    )

    async with schedule_service(platform) as service:
        recomputed = await service.snapshot_month(employee_id, *MARCH)

    assert recomputed.expected_minutes == FULL_TIME_MARCH
    assert recomputed.snapshot is not None and recomputed.snapshot.revision == 2
    assert await platform.scalar(
        "SELECT count(*) FROM expected_hours_snapshots WHERE employee_id = :id",
        {"id": employee_id},
    ) == 2, "the first revision is still there"
    assert await platform.sql(
        f"SELECT {columns} FROM expected_hours_snapshots WHERE id = :id", {"id": snapshot_id}
    ) == before, "the revision the first run wrote is byte-for-byte what it was"


async def test_running_the_month_end_pass_twice_appends_nothing(platform: Platform) -> None:
    employee_id, department = await region_employee(platform, code="pasada", region=MADRID_REGION)
    await add_schedule(platform, code="intensivo", days=intensivo_week(), department_id=department)

    async with schedule_service(platform) as service:
        first = await service.snapshot_month(employee_id, *MARCH)
        second = await service.snapshot_month(employee_id, *MARCH)

    assert first.snapshot is not None and second.snapshot is not None
    assert second.snapshot.id == first.snapshot.id, "an identical run wrote a second revision"
    assert await platform.scalar(
        "SELECT count(*) FROM expected_hours_snapshots WHERE employee_id = :id",
        {"id": employee_id},
    ) == 1


async def test_the_snapshot_table_cannot_be_rewritten_by_the_runtime_role(
    platform: Platform, app_connection: async_sessionmaker
) -> None:
    """`REVOKE UPDATE, DELETE`, which is what makes the evidence a property of the
    database rather than of the code paths that exist today."""
    employee_id, _ = await region_employee(platform, code="evidencia", region=MADRID_REGION)
    await add_schedule(platform, code="estandar", days=full_week(), is_default=True)
    async with schedule_service(platform) as service:
        await service.snapshot_month(employee_id, *MARCH)

    for statement in (
        "UPDATE expected_hours_snapshots SET expected_minutes = 1",
        "DELETE FROM expected_hours_snapshots",
    ):
        async with app_connection() as session:
            with pytest.raises(Exception) as refusal:
                await session.execute(text(statement))
            assert "permission denied" in str(refusal.value).lower()
        assert await platform.scalar(
            "SELECT count(*) FROM expected_hours_snapshots WHERE employee_id = :id",
            {"id": employee_id},
        ) == 1


# --- ticket 21's daily record ----------------------------------------------


async def test_a_working_day_records_what_the_schedule_expected(platform: Platform) -> None:
    employee_id, department = await region_employee(platform, code="diario", region=MADRID_REGION)
    schedule = await add_schedule(
        platform, code="intensivo", days=intensivo_week(), department_id=department
    )

    status, expected = await day_status(platform, employee_id, date(2026, 3, 18))
    friday_status, friday_expected = await day_status(platform, employee_id, date(2026, 3, 20))

    assert (status, expected) == ("absent", FULL_DAY)
    assert (friday_status, friday_expected) == ("absent", SHORT_FRIDAY)

    stored = await platform.sql(
        "SELECT expected_minutes, snapshot_schedule_id FROM attendance_daily "
        "WHERE employee_id = :id AND business_date = DATE '2026-03-18'",
        {"id": employee_id},
    )
    assert stored == [(FULL_DAY, schedule.id)]


async def test_a_holiday_reads_as_expected_zero_and_not_as_an_absence(
    platform: Platform,
) -> None:
    """No punches on a day nobody was expected is not an absence."""
    employee_id, _ = await region_employee(platform, code="festivo", region=MADRID_REGION)
    await add_schedule(platform, code="estandar", days=full_week(), is_default=True)
    await add_holiday(platform, MARCH_THURSDAY)

    holiday_status, holiday_expected = await day_status(platform, employee_id, MARCH_THURSDAY)
    saturday_status, saturday_expected = await day_status(platform, employee_id, MARCH_SATURDAY)
    ordinary_status, ordinary_expected = await day_status(platform, employee_id, date(2026, 3, 18))

    assert (holiday_status, holiday_expected) == (DayStatus.HOLIDAY.value, 0)
    assert (saturday_status, saturday_expected) == (DayStatus.NON_WORKING.value, 0)
    assert (ordinary_status, ordinary_expected) == (DayStatus.ABSENT.value, FULL_DAY)

    async with attendance_service(platform) as service:
        record = await service.day_view(employee_id, MARCH_THURSDAY)
    assert not record.was_expected


async def test_working_on_a_holiday_is_recorded_as_the_day_it_was(platform: Platform) -> None:
    """Nobody was expected, and somebody came in: the punches decide the status.

    The expectation stays zero, which is what makes the day's overtime legible
    later; the status stays the event-derived one, because calling a day somebody
    worked a holiday in the sense of "nobody worked" would be false.
    """
    employee_id, _ = await region_employee(platform, code="voluntario", region=MADRID_REGION)
    await add_schedule(platform, code="estandar", days=full_week(), is_default=True)
    await add_holiday(platform, MARCH_THURSDAY)

    await worked(platform, employee_id, MARCH_THURSDAY, 8, 12)
    status, expected = await day_status(platform, employee_id, MARCH_THURSDAY)

    assert (status, expected) == (DayStatus.OK.value, 0)


async def test_a_day_with_no_schedule_still_records_no_expectation(platform: Platform) -> None:
    """Ticket 21's behaviour, kept: no schedule means no figure to claim.

    Zero and null are different answers, and this is the one that says "nobody has
    configured this person's week yet".
    """
    employee_id = await attendance_employee(platform, code="sinhorario")
    await add_holiday(platform, MARCH_THURSDAY)

    await worked(platform, employee_id, date(2026, 3, 18), 8, 16)
    async with attendance_service(platform) as service:
        record = await service.recompute_day(employee_id, date(2026, 3, 18))
        empty = await service.recompute_day(employee_id, MARCH_SATURDAY)

    assert record.expected_minutes is None
    assert record.snapshot_schedule_id is None
    assert empty.status is DayStatus.ABSENT, (
        "without a schedule, a Saturday is still 'nobody worked' rather than a rest day"
    )
    assert await platform.scalar(
        "SELECT expected_minutes FROM attendance_daily WHERE employee_id = :id "
        "AND business_date = DATE '2026-03-18'",
        {"id": employee_id},
    ) is None


async def test_a_range_reads_its_expectations_without_writing_them(platform: Platform) -> None:
    employee_id, _ = await region_employee(platform, code="rango", region=MADRID_REGION)
    await add_schedule(platform, code="estandar", days=full_week(), is_default=True)
    await add_holiday(platform, MARCH_THURSDAY)

    async with attendance_service(platform) as service:
        days = await service.range_view(employee_id, date(2026, 3, 16), date(2026, 3, 22))

    by_date = {item.business_date: item for item in days}
    assert by_date[date(2026, 3, 18)].expected_minutes == FULL_DAY
    assert by_date[MARCH_THURSDAY].status is DayStatus.HOLIDAY
    assert by_date[MARCH_SATURDAY].status is DayStatus.NON_WORKING
    assert await platform.scalar(
        "SELECT count(*) FROM attendance_daily WHERE employee_id = :id", {"id": employee_id}
    ) == 0, "a read is not a write"


# --- permissions ------------------------------------------------------------


def test_no_role_widens_the_self_only_schedule_action() -> None:
    """Reading somebody else's week is not a permission any role holds.

    HR reads a month through its own action; a manager reads a report's through
    ticket 24's. Neither arrives as a quiet widening of "I may see my own".
    """
    subject = uuid.uuid4()
    for role in sorted(SYSTEM_ROLES):
        principal = Principal(
            user_id=uuid.uuid4(),
            employee_id=uuid.uuid4(),
            username="ana",
            roles=frozenset({role, "employee"}),
            clearance_level="high",
            department_ids=frozenset({uuid.uuid4()}),
            primary_department_id=uuid.uuid4(),
            is_manager=role == "manager",
            reports_employee_ids=frozenset({subject}),
        )
        decision = can(principal, Action.SCHEDULE_READ_OWN, own_resource(subject))
        assert decision.denied, f"role={role} read somebody else's schedule"
        assert decision.primary_reason is Reason.NOT_OWNER
        assert can(principal, Action.SCHEDULE_READ_OWN, own_resource(principal.employee_id)).allowed

    assert can(
        Principal(
            user_id=uuid.uuid4(),
            employee_id=uuid.uuid4(),
            username="ana",
            roles=frozenset({"hr", "employee"}),
            clearance_level="high",
        ),
        Action.SCHEDULE_MANAGE,
    ).allowed
    assert can(
        Principal(
            user_id=uuid.uuid4(),
            employee_id=uuid.uuid4(),
            username="ana",
            roles=frozenset({"finance", "employee"}),
            clearance_level="high",
        ),
        Action.SCHEDULE_MANAGE,
    ).denied


async def test_only_hr_and_administration_manage_schedules_and_holidays(
    platform: Platform,
) -> None:
    """End to end: the refusals carry a catalogued code, and they are recorded."""
    body = {
        "code": "ajeno",
        "name_es": "Ajeno",
        "name_en": "Somebody else's",
        "days": [
            {
                "weekday": 0,
                "expected_minutes": 480,
                "start_time": "08:00",
                "end_time": "16:00",
            }
        ],
    }
    holiday = {
        "date": MARCH_THURSDAY.isoformat(),
        "name_es": "Fiesta",
        "name_en": "Holiday",
        "scope": "national",
    }

    for role in ("employee", "finance", "it", "compliance", "manager"):
        actor = await platform.account(roles=(role,))

        created = await actor.post("/api/v1/schedules", json=body)
        added = await actor.post("/api/v1/holidays", json=holiday)
        listed = await actor.get("/api/v1/schedules")

        for response in (created, added, listed):
            assert response.status_code == 403, f"role={role}: {response.text}"
            assert response.json()["error"]["code"] == ErrorCode.FORBIDDEN.value

        calendar = await actor.get("/api/v1/holidays")
        assert calendar.status_code == 200, (
            "the calendar is published: it is a fact about the country, not personnel data"
        )
        await actor.close()

    assert await platform.scalar("SELECT count(*) FROM work_schedules") == 0
    assert await platform.scalar("SELECT count(*) FROM holidays") == 0
    refusals = await platform.sql("SELECT after FROM audit_log WHERE action = 'access.refused'")
    assert any(row[0]["action"] == str(Action.SCHEDULE_MANAGE) for row in refusals)
    assert any(row[0]["action"] == str(Action.HOLIDAY_MANAGE) for row in refusals)


async def test_an_employee_reads_their_own_week_and_not_a_colleagues(
    platform: Platform,
) -> None:
    actor = await platform.account(roles=("employee",))
    colleague = await platform.employee()
    await add_schedule(platform, code="empresa", days=full_week(), is_default=True)

    mine = await actor.get("/api/v1/schedules/mine", params={"on_date": "2026-03-18"})
    month = await actor.get(
        "/api/v1/schedules/expected-hours", params={"year": 2026, "month": 3}
    )
    theirs = await actor.get(
        "/api/v1/schedules/expected-hours",
        params={"year": 2026, "month": 3, "employee_id": colleague},
    )

    assert mine.status_code == 200, mine.text
    assert mine.json()["expected_minutes"] == FULL_DAY
    assert mine.json()["source"] == "default"
    assert mine.json()["schedule"]["code"] == "empresa"

    assert month.status_code == 200, month.text
    assert month.json()["expected_minutes"] == FULL_TIME_MARCH
    assert month.json()["is_snapshot"] is False

    assert theirs.status_code == 403, theirs.text
    assert theirs.json()["error"]["code"] == ErrorCode.FORBIDDEN.value
    assert "expected_minutes" not in theirs.text
    refusals = await platform.sql("SELECT after FROM audit_log WHERE action = 'access.refused'")
    assert any(row[0]["action"] == str(Action.SCHEDULE_READ_OWN) for row in refusals)


async def test_the_api_round_trip_for_a_pattern_and_a_month(platform: Platform) -> None:
    """Create, correct, freeze, read back — through the real surface.

    The reading is done by the employee, not by HR: `schedule.read_own` is
    self-only, and HR holding it means HR may read *their own* month. HR's half of
    the round trip is the writing and the freezing.
    """
    department = await platform.department("api", region_code=MADRID_REGION)
    employee = await platform.account(roles=("employee",))
    employee_id = UUID(employee.employee_id)
    position = await platform.position(department, "apip")
    await platform.assign(employee.employee_id, department, position)
    hr = await platform.account(roles=("hr",))

    def payload(code: str, minutes: int) -> dict:
        return {
            "code": code,
            "name_es": code,
            "name_en": code,
            "department_id": department,
            "days": [
                {
                    "weekday": weekday,
                    "expected_minutes": minutes,
                    "start_time": "08:00",
                    "end_time": "16:00",
                }
                for weekday in range(5)
            ],
        }

    created = await hr.post("/api/v1/schedules", json=payload("api-estandar", FULL_DAY))
    assert created.status_code == 201, created.text
    assert created.json()["weekly_hours"] == "40.00"
    schedule_id = created.json()["id"]

    # A day whose window does not match its minutes is a catalogued 422, not a 500
    # from a constraint nobody can read.
    broken = await hr.post(
        "/api/v1/schedules",
        json={
            "code": "api-roto",
            "name_es": "Roto",
            "name_en": "Broken",
            "department_id": department,
            "days": [
                {
                    "weekday": 0,
                    "expected_minutes": SHORT_FRIDAY,
                    "start_time": "08:00",
                    "end_time": "16:00",
                }
            ],
        },
    )
    assert broken.status_code == 422, broken.text
    assert broken.json()["error"]["code"] == ErrorCode.SCHEDULE_INVALID_DAY.value

    corrected = await hr.patch(
        f"/api/v1/schedules/{schedule_id}",
        json={
            "days": [
                {
                    "weekday": weekday,
                    "expected_minutes": FULL_DAY,
                    "start_time": "08:00",
                    "end_time": "16:00",
                }
                for weekday in range(4)
            ]
            + [
                {
                    "weekday": 4,
                    "expected_minutes": SHORT_FRIDAY,
                    "start_time": "08:00",
                    "end_time": "14:00",
                }
            ]
        },
    )
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["weekly_hours"] == "38.00"

    frozen = await hr.post(
        "/api/v1/schedules/expected-hours/snapshots",
        json={"year": 2026, "month": 3, "employee_id": str(employee_id)},
    )
    assert frozen.status_code == 201, frozen.text
    assert frozen.json()[0]["expected_minutes"] == INTENSIVO_MARCH
    assert frozen.json()[0]["revision"] == 1

    read_back = await employee.get(
        "/api/v1/schedules/expected-hours", params={"year": 2026, "month": 3}
    )
    assert read_back.status_code == 200, read_back.text
    assert read_back.json()["is_snapshot"] is True
    assert read_back.json()["expected_minutes"] == INTENSIVO_MARCH

    # And HR, asking for somebody else's month through the self-only action, is
    # refused: freezing a figure and reading somebody's record are different acts.
    refused = await hr.get(
        "/api/v1/schedules/expected-hours",
        params={"year": 2026, "month": 3, "employee_id": str(employee_id)},
    )
    assert refused.status_code == 403, refused.text

    overrides = await hr.post(
        "/api/v1/schedules/overrides",
        json={
            "employee_id": str(employee_id),
            "schedule_id": schedule_id,
            "effective_from": "2026-03-01",
            "effective_to": "2026-03-31",
            "reason": "Jornada intensiva de verano",
        },
    )
    assert overrides.status_code == 201, overrides.text
    override_id = overrides.json()["id"]

    listed = await hr.get("/api/v1/schedules")
    assert listed.status_code == 200
    assert [item["code"] for item in listed.json()] == ["api-estandar"]

    ended = await hr.delete(f"/api/v1/schedules/overrides/{override_id}")
    assert ended.status_code == 204, ended.text
    assert await platform.scalar(
        "SELECT count(*) FROM employee_schedule_overrides"
    ) == 0


async def test_an_employee_cannot_read_a_colleagues_schedule_or_freeze_a_month(
    platform: Platform,
) -> None:
    """The two halves of "self-service": you read yours, and you freeze nothing."""
    actor = await platform.account(roles=("employee",))
    colleague = await platform.employee()

    mine = await actor.get("/api/v1/schedules/mine", params={"employee_id": colleague})
    snapshot = await actor.post(
        "/api/v1/schedules/expected-hours/snapshots",
        json={"year": 2026, "month": 3, "employee_id": colleague},
    )

    assert mine.status_code == 403, mine.text
    assert snapshot.status_code == 403, snapshot.text
    assert await platform.scalar("SELECT count(*) FROM expected_hours_snapshots") == 0


# --- the database's own rules ----------------------------------------------


@pytest.fixture
async def app_connection(settings: Settings) -> AsyncIterator[async_sessionmaker]:
    """Sessions bound to the restricted role, on the test database."""
    restricted = create_async_engine(settings.runtime_test_database_url)
    try:
        yield async_sessionmaker(bind=restricted, expire_on_commit=False)
    finally:
        await restricted.dispose()


async def test_the_pattern_rules_are_enforced_by_postgresql(platform: Platform) -> None:
    """The service refuses these first; the constraint is what holds when somebody
    writes the row by hand, which is the case a check in Python cannot cover."""
    schedule = await add_schedule(platform, code="constricciones", days=full_week())

    with pytest.raises(Exception) as inconsistent:
        await platform.sql(
            "INSERT INTO work_schedule_days "
            "(id, schedule_id, weekday, expected_minutes, start_time, end_time, break_minutes) "
            "VALUES (gen_random_uuid(), :schedule, 5, 480, TIME '09:00', TIME '14:00', 0)",
            {"schedule": schedule.id},
        )
    assert "ck_work_schedule_days_consistent" in str(inconsistent.value)

    with pytest.raises(Exception) as duplicated:
        await platform.sql(
            "INSERT INTO work_schedule_days "
            "(id, schedule_id, weekday, expected_minutes, start_time, end_time, break_minutes) "
            "VALUES (gen_random_uuid(), :schedule, 0, 480, TIME '08:00', TIME '16:00', 0)",
            {"schedule": schedule.id},
        )
    assert "uq_work_schedule_days_schedule_weekday" in str(duplicated.value)

    # A national holiday that names a region applies everywhere anyway, so the
    # column would be a lie about who observes it.
    with pytest.raises(Exception) as national_region:
        await platform.sql(
            "INSERT INTO holidays (id, date, name_es, name_en, scope, region_code, year) "
            "VALUES (gen_random_uuid(), DATE '2026-01-01', 'x', 'x', 'national', 'ES-MD', 2026)"
        )
    assert "ck_holidays_region_unexpected" in str(national_region.value)

    # And a regional one without a region could never match anybody.
    with pytest.raises(Exception) as regional_region:
        await platform.sql(
            "INSERT INTO holidays (id, date, name_es, name_en, scope, region_code, year) "
            "VALUES (gen_random_uuid(), DATE '2026-01-02', 'x', 'x', 'regional', NULL, 2026)"
        )
    assert "ck_holidays_region_required" in str(regional_region.value)

    # The year is the date's, always.
    with pytest.raises(Exception) as wrong_year:
        await platform.sql(
            "INSERT INTO holidays (id, date, name_es, name_en, scope, region_code, year) "
            "VALUES (gen_random_uuid(), DATE '2026-01-03', 'x', 'x', 'national', NULL, 2025)"
        )
    assert "ck_holidays_year_matches_date" in str(wrong_year.value)

    # One national holiday per date, and `NULL <> NULL` must not let a second in.
    await add_holiday(platform, date(2026, 1, 4))
    with pytest.raises(Exception) as duplicate:
        await platform.sql(
            "INSERT INTO holidays (id, date, name_es, name_en, scope, region_code, year) "
            "VALUES (gen_random_uuid(), DATE '2026-01-04', 'otra', 'other', 'national', NULL, 2026)"
        )
    assert "uq_holidays_date_scope_region" in str(duplicate.value)


async def test_a_property_is_only_ever_concluded_from_a_real_row(platform: Platform) -> None:
    """A guard against this file passing for the wrong reason.

    Every figure asserted above is read back out of PostgreSQL or computed from a
    calendar written down in this file; nothing here is compared against a value the
    code under test produced. This test exists so that a future edit that started
    doing so would have something to fail against.
    """
    employee_id = await attendance_employee(platform, code="control")
    await add_schedule(platform, code="estandar", days=full_week(), is_default=True)

    async with schedule_service(platform) as service:
        month = await service.expected_minutes(employee_id, *MARCH)

    assert month.expected_minutes == 22 * 480
    assert len([item for item in month.days if item.expected_minutes]) == 22
    weekday_minutes = {item.business_date.weekday() for item in month.days}
    assert weekday_minutes == {0, 1, 2, 3, 4, 5, 6}
    assert month.days[0].business_date == date(2026, 3, 1)
    assert month.days[0].business_date.weekday() == 6, "the 1st of March 2026 is a Sunday"


async def test_the_month_end_pass_covers_everybody_and_reports_what_it_did(
    platform: Platform,
) -> None:
    """The pass is the job's operation, and it is idempotent in storage too.

    The fixture's own administrators are employees as well, so the assertions are
    about the two people this test made rather than about a total: a pass that
    counted employees would be asserting on the fixture.
    """
    first, _ = await region_employee(platform, code="loteuno", region=MADRID_REGION)
    second = await attendance_employee(platform, code="lotedos")
    await add_schedule(platform, code="estandar", days=full_week(), is_default=True)

    async with schedule_service(platform) as service:
        report = await service.snapshot_all(*MARCH)

    written = {item.employee_id for item in report.snapshotted}
    assert {first, second} <= written
    assert report.failed == ()
    stored = await platform.scalar("SELECT count(*) FROM expected_hours_snapshots")
    assert stored == report.written

    async with schedule_service(platform) as service:
        again = await service.snapshot_all(*MARCH)

    assert again.written == report.written
    assert await platform.scalar("SELECT count(*) FROM expected_hours_snapshots") == stored, (
        "a second pass that computed the same inputs appended revisions"
    )


async def test_a_snapshot_made_in_march_does_not_move_when_the_year_turns(
    platform: Platform,
) -> None:
    """The obligation in one sentence: four years later, the figure still explains
    itself, even though the schedule, the holiday table and the department have all
    been edited since."""
    employee_id, department = await region_employee(platform, code="cuatro", region=MADRID_REGION)
    schedule = await add_schedule(
        platform, code="intensivo", days=intensivo_week(), department_id=department
    )
    holiday = await add_holiday(platform, MARCH_THURSDAY, scope=HolidayScope.NATIONAL)

    async with schedule_service(platform) as service:
        frozen = await service.snapshot_month(employee_id, *MARCH)
    assert frozen.snapshot is not None
    original = await platform.sql(
        "SELECT expected_minutes, inputs FROM expected_hours_snapshots WHERE id = :id",
        {"id": frozen.snapshot.id},
    )

    # Everything that fed the figure changes: the pattern, the calendar and the
    # department's region.
    await change_schedule(platform, schedule.id, full_week())
    await platform.sql("DELETE FROM holidays WHERE id = :id", {"id": holiday.id})
    await platform.sql(
        "UPDATE departments SET region_code = :region WHERE id = :id",
        {"region": VALENCIA_REGION, "id": department},
    )

    after = await platform.sql(
        "SELECT expected_minutes, inputs FROM expected_hours_snapshots WHERE id = :id",
        {"id": frozen.snapshot.id},
    )
    assert after == original
    assert await month_minutes(platform, employee_id, *MARCH) == INTENSIVO_MARCH - FULL_DAY
    assert after[0][1]["holidays"][0]["name_es"] == holiday.name_es, (
        "the holiday row the figure used is in the snapshot, not looked up again"
    )


async def test_an_override_that_ended_still_explains_the_month_it_covered(
    platform: Platform,
) -> None:
    """Ending an override changes the future, never a figure already written down."""
    employee_id, department = await region_employee(platform, code="vigencia", region=MADRID_REGION)
    await add_schedule(platform, code="estandar", days=full_week(), is_default=True)
    part_time = await add_schedule(platform, code="parcial", days=full_week(240))

    async with schedule_service(platform) as service:
        override = await service.set_override(
            OverrideInput(
                employee_id=employee_id,
                schedule_id=part_time.id,
                effective_from=date(2026, 3, 1),
                effective_to=date(2026, 3, 31),
                reason="Jornada parcial en marzo",
            )
        )
        frozen = await service.snapshot_month(employee_id, *MARCH)
        await service.delete_override(override.id)

    assert frozen.expected_minutes == PART_TIME_MARCH
    assert frozen.inputs["days"][0]["source"] == "override"
    assert await month_minutes(platform, employee_id, *MARCH) == PART_TIME_MARCH
    # April 2026 has twenty-two working days, and no override and no holiday
    # touches it.
    assert await month_minutes(platform, employee_id, 2026, 4) == 22 * FULL_DAY


def test_the_public_surface_is_the_three_groups_the_module_documents() -> None:
    """The interface, pinned. Adding a fourth group is a decision somebody makes."""
    public = {
        name
        for name in vars(ScheduleService)
        if not name.startswith("_") and callable(getattr(ScheduleService, name))
    }

    assert public >= {
        "resolve",
        "day_expectation",
        "day_expectations",
        "expected_minutes",
        "snapshot_month",
        "snapshot_all",
    }
    # The arithmetic is not public: it is `calculation`'s, and a caller that wants
    # a number asks for a day or a month rather than for a weekday.
    assert not any(name.startswith("minutes_for") for name in public)


async def test_a_bad_import_path_is_reported_in_the_files_own_vocabulary(
    tmp_path: Path,
) -> None:
    """A missing path and a malformed one are the same answer to whoever typed it."""
    with pytest.raises(DomainError) as missing:
        read_holidays_csv(tmp_path / "no-existe.csv")

    assert missing.value.code is ScheduleErrorCode.SCHEDULE_INVALID_HOLIDAY_FILE
    assert "no-existe.csv" in (missing.value.detail or "")
    assert missing.value.http_status == 422


async def test_a_month_with_no_days_and_a_holiday_with_no_name_are_refused(
    platform: Platform,
) -> None:
    async with schedule_service(platform) as service:
        with pytest.raises(DomainError) as empty:
            await service.create_schedule(
                ScheduleInput(code="vacio", name_es="Vacío", name_en="Empty", days=())
            )
        with pytest.raises(DomainError) as twice:
            await service.create_schedule(
                ScheduleInput(
                    code="repetido",
                    name_es="Repetido",
                    name_en="Repeated",
                    days=(day(0, FULL_DAY), day(0, FULL_DAY)),
                )
            )
        with pytest.raises(DomainError) as unnamed:
            await service.add_holiday(
                HolidayInput(
                    date=MARCH_THURSDAY,
                    name_es="   ",
                    name_en="Holiday",
                    scope=HolidayScope.NATIONAL,
                )
            )

    assert empty.value.code is ScheduleErrorCode.SCHEDULE_INVALID_DAY
    assert twice.value.code is ScheduleErrorCode.SCHEDULE_INVALID_DAY
    assert unnamed.value.code is ScheduleErrorCode.SCHEDULE_INVALID_HOLIDAY


def test_the_month_end_pass_defaults_to_the_month_that_has_just_ended() -> None:
    """Freezing the month in progress would freeze a figure still being decided.

    The argument handling is tested because it is the difference between "the month
    that ended" and "the month we are in", and the second is wrong by definition.
    """
    from app.jobs.snapshot_expected_hours import parse_month, previous_month

    assert previous_month(date(2026, 3, 15)) == (2026, 2)
    assert previous_month(date(2026, 1, 1)) == (2025, 12), "January looks back a year"
    assert parse_month("2026-03") == (2026, 3)


async def test_every_refusal_this_module_raises_carries_a_catalogued_code(
    platform: Platform,
) -> None:
    """The branches a client routes on, each with its own code and status.

    A typo'd alias in `ScheduleErrorCode` stays invisible until the branch that
    raises it runs, which is why they are all exercised here rather than trusted.
    """
    employee_id, department = await region_employee(platform, code="codigos", region=MADRID_REGION)
    await add_schedule(platform, code="activo", days=full_week(), department_id=department)
    retired = await add_schedule(platform, code="retirado", days=full_week())
    await add_holiday(platform, MARCH_THURSDAY)

    async with schedule_service(platform) as service:
        await service.update_schedule(retired.id, SchedulePatch(is_active=False))

        with pytest.raises(DomainError) as inactive:
            await service.set_override(
                OverrideInput(
                    employee_id=employee_id,
                    schedule_id=retired.id,
                    effective_from=MARCH_THURSDAY,
                    reason="un horario retirado",
                )
            )
        with pytest.raises(DomainError) as unknown_schedule:
            await service.get_schedule(uuid.uuid4())
        with pytest.raises(DomainError) as unknown_override:
            await service.delete_override(uuid.uuid4())
        with pytest.raises(DomainError) as unknown_holiday:
            await service.delete_holiday(uuid.uuid4())
        with pytest.raises(DomainError) as already_a_holiday:
            await service.add_holiday(
                HolidayInput(
                    date=MARCH_THURSDAY,
                    name_es="Otra vez",
                    name_en="Again",
                    scope=HolidayScope.NATIONAL,
                )
            )
        with pytest.raises(DomainError) as unknown_employee:
            await service.expected_minutes(uuid.uuid4(), *MARCH)

    assert inactive.value.code is ScheduleErrorCode.SCHEDULE_INACTIVE
    assert unknown_schedule.value.code is ScheduleErrorCode.SCHEDULE_NOT_FOUND
    assert unknown_override.value.code is ScheduleErrorCode.SCHEDULE_OVERRIDE_NOT_FOUND
    assert unknown_holiday.value.code is ScheduleErrorCode.SCHEDULE_HOLIDAY_NOT_FOUND
    assert already_a_holiday.value.code is ScheduleErrorCode.SCHEDULE_HOLIDAY_EXISTS
    assert unknown_employee.value.code is ScheduleErrorCode.EMPLOYEE_NOT_FOUND

    # The statuses the client distinguishes: a closed catalogue entry is the
    # caller's to fix, a name that is already taken is a conflict, and a missing
    # person is a 404 rather than a refusal.
    assert [
        refusal.value.http_status
        for refusal in (
            inactive,
            unknown_schedule,
            unknown_override,
            unknown_holiday,
            already_a_holiday,
            unknown_employee,
        )
    ] == [422, 404, 404, 404, 409, 404]


__all__ = ["MARCH", "MARCH_THURSDAY"]
