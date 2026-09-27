"""What a month's expected hours are, and what produced them.

Pure: values in, a value out, no session and no clock — the same split as
`attendance/derivation.py`, and for the same reason. `codebase-design` §5 puts the
arithmetic of a business calendar in the "in-process" column so it can be tested
with hand-built days, and the service's job is only to fetch the right rows and
write the result back.

Three decisions live here rather than in SQL, and they are the module's answers to
the ticket's checklist:

* **A month is the sum of its days, and a day is zero when a holiday covers it.**
  Not "the month's weekdays minus the holidays in it": the regions of a month can
  change with a transfer, an override can start mid-month, and a day is the only
  unit all three of those agree on.
* **A holiday applies by region, not by scope.** `Holiday.applies_to` is the whole
  rule; `scope` says who declared it, which is a fact about the calendar rather
  than about who observes it.
* **The inputs are a document, not a column per fact.** `month_inputs` writes down
  every day's schedule, window, minutes and holiday as they were, because the
  snapshot's job is to answer "how was this number reached" without the schedule
  table — which by then may say something else.
"""

from datetime import date, time
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from app.domain.errors import DomainError
from app.domain.schedule.errors import ScheduleErrorCode
from app.domain.schedule.models import (
    MINUTES_PER_DAY,
    MINUTES_PER_HOUR,
    WEEKDAYS,
    AssignmentSpan,
    DayExpectation,
    Holiday,
    HolidayScope,
    ResolvedSchedule,
    ScheduleDay,
    ScheduleDayInput,
    ScheduleSource,
    WorkSchedule,
)

#: How specific a holiday is, for choosing which one to name on a day that several
#: cover. A local holiday is the more informative answer when Madrid's Almudena and
#: something national land together; the month's inputs carry all of them either
#: way, so this is about the day's own label rather than about the arithmetic.
_SPECIFICITY: dict[HolidayScope, int] = {
    HolidayScope.NATIONAL: 0,
    HolidayScope.REGIONAL: 1,
    HolidayScope.LOCAL: 2,
}


def month_dates(year: int, month: int) -> tuple[date, ...]:
    """Every date in a calendar month, in order.

    Written here rather than borrowed from `attendance.business_day.dates_between`
    so that the scheduling module does not have to import the attendance module to
    know how long March is.
    """
    if not 1 <= month <= 12:
        raise DomainError(
            ScheduleErrorCode.INVALID_REQUEST, detail=f"month {month} is not a month"
        )
    if not 2000 <= year <= 2200:
        raise DomainError(
            ScheduleErrorCode.INVALID_REQUEST, detail=f"year {year} is outside the calendar"
        )
    first = date(year, month, 1)
    last = date(year + (month == 12), (month % 12) + 1, 1)
    return tuple(
        date.fromordinal(day) for day in range(first.toordinal(), last.toordinal())
    )


def window_minutes(start: time, end: time) -> int:
    """How long a shift window is, in whole minutes, break excluded.

    The same arithmetic the database's CHECK constraint performs, and deliberately
    the same: a service that accepted a day the table refuses would fail at the
    insert with a message about a constraint rather than about the window.

    Seconds are refused rather than truncated (see `validate_day`), which is what
    makes the two agree exactly — `CAST(numeric AS integer)` rounds, so a window of
    480.5 minutes would be 481 here and 481 there only by luck.
    """
    return ((end.hour * 60 + end.minute) - (start.hour * 60 + start.minute))


def validate_day(day: ScheduleDayInput) -> None:
    """Refuse a day that would not survive the table's own constraint.

    Stated as one rule in two places on purpose: the CHECK constraint is what makes
    a hand-written row impossible, and this is what turns the same rule into a
    catalogued 400 that says which of the two halves is wrong.
    """
    if day.weekday not in WEEKDAYS:
        raise _invalid(f"weekday {day.weekday} is not one of {WEEKDAYS}, Monday being 0")
    if not 0 <= day.expected_minutes <= MINUTES_PER_DAY:
        raise _invalid(f"{day.expected_minutes} minutes is not a day")
    if day.break_minutes < 0:
        raise _invalid("a break cannot be negative")
    for label, moment in (("start_time", day.start_time), ("end_time", day.end_time)):
        if moment is not None and (moment.second or moment.microsecond):
            raise _invalid(f"{label} {moment.isoformat()} is not a whole minute")

    if day.expected_minutes == 0:
        if day.start_time is not None or day.end_time is not None or day.break_minutes:
            raise _invalid(
                "a day with no expected minutes has no window and no break; "
                "either give it minutes or leave it out of the schedule"
            )
        return

    if day.start_time is None or day.end_time is None:
        raise _invalid(
            f"a day of {day.expected_minutes} minutes states when it starts and ends"
        )
    window = window_minutes(day.start_time, day.end_time)
    if window <= 0:
        raise _invalid(
            f"the window {day.start_time}-{day.end_time} does not end after it starts; "
            "a shift that crosses midnight is two days in this system"
        )
    expected = window - day.break_minutes
    if expected != day.expected_minutes:
        raise _invalid(
            f"{day.start_time}-{day.end_time} less a {day.break_minutes}-minute break is "
            f"{expected} minutes, not {day.expected_minutes}"
        )


def validate_schedule(days: tuple[ScheduleDayInput, ...]) -> None:
    """Refuse a pattern with a weekday twice, or with no work in it at all."""
    for day in days:
        validate_day(day)
    weekdays = [day.weekday for day in days]
    if len(set(weekdays)) != len(weekdays):
        raise _invalid(f"a weekday may appear once: {sorted(weekdays)}")
    if not days:
        raise _invalid("a schedule with no days expects nothing on any day of the week")


def weekly_hours_of(days: tuple[ScheduleDay, ...] | tuple[ScheduleDayInput, ...]) -> Decimal:
    """A week's minutes as hours, which is the unit a contract is agreed in.

    Derived, never accepted from a caller: `weekly_hours` is a sum of the days, and
    a second statement of a sum is a statement that eventually disagrees with it.
    """
    total = sum(day.expected_minutes for day in days)
    return (Decimal(total) / MINUTES_PER_HOUR).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def resolve_schedule(
    *,
    override_schedule: WorkSchedule | None,
    department_schedule: WorkSchedule | None,
    default_schedule: WorkSchedule | None,
    override_id: UUID | None = None,
) -> ResolvedSchedule | None:
    """The fallback chain, in one place: override, then department, then default.

    Written as three named arguments rather than as a list to be searched, so the
    order is a property of this function's signature. `None` means nobody has
    configured a schedule that reaches this person, which is not the same fact as
    a schedule that expects nothing — and the caller distinguishes them by whether
    it gets a `ResolvedSchedule` back at all.
    """
    if override_schedule is not None:
        return ResolvedSchedule(
            schedule=override_schedule,
            source=ScheduleSource.OVERRIDE,
            override_id=override_id,
        )
    if department_schedule is not None:
        return ResolvedSchedule(schedule=department_schedule, source=ScheduleSource.DEPARTMENT)
    if default_schedule is not None:
        return ResolvedSchedule(schedule=default_schedule, source=ScheduleSource.DEFAULT)
    return None


def department_on(spans: list[AssignmentSpan], on_date: date) -> UUID | None:
    """Where somebody worked on a date, from their assignment spans.

    The primary assignment wins, and a non-primary one is the fallback for the days
    between two primary ones — a transfer ends the old assignment the day before the
    new one starts, so on the day itself there is exactly one candidate. Ties are
    broken by start date and then by id, because a hand-edited pair of primary rows
    must still produce one answer rather than whichever the query returned first.
    """
    covering = [span for span in spans if span.covers(on_date)]
    if not covering:
        return None
    primary = [span for span in covering if span.is_primary]
    chosen = primary or covering
    return min(chosen, key=lambda span: (span.start_date, str(span.department_id))).department_id


def holidays_on(
    holidays: tuple[Holiday, ...], on_date: date, region_code: str | None
) -> list[Holiday]:
    """Every holiday of a year's table that falls on a date and applies to a region."""
    return [
        holiday
        for holiday in holidays
        if holiday.date == on_date and holiday.applies_to(region_code)
    ]


def most_specific(holidays: list[Holiday]) -> Holiday | None:
    """The one to name on the day. The order is total, so the answer is stable."""
    if not holidays:
        return None
    return max(
        holidays,
        key=lambda holiday: (
            _SPECIFICITY[holiday.scope],
            holiday.name_es,
            str(holiday.id),
        ),
    )


def expectation_for(
    *,
    employee_id: UUID,
    business_date: date,
    resolved: ResolvedSchedule | None,
    holidays: tuple[Holiday, ...],
    region_code: str | None,
) -> DayExpectation:
    """What one person's one day was expected to be.

    A holiday zeroes the day whatever the schedule says. That is the whole reason
    the figure is computed day by day: a holiday is a fact about a date, and a
    schedule is a fact about a weekday, and only the date knows which of the two
    wins on the 2nd of April.
    """
    weekday = business_date.weekday()
    applicable = holidays_on(holidays, business_date, region_code)
    holiday = most_specific(applicable)
    day = resolved.day(weekday) if resolved is not None else None
    expected = 0 if holiday is not None or resolved is None else resolved.minutes_on(weekday)

    return DayExpectation(
        employee_id=employee_id,
        business_date=business_date,
        expected_minutes=expected,
        source=resolved.source if resolved is not None else ScheduleSource.NONE,
        weekday=weekday,
        schedule_id=resolved.schedule.id if resolved is not None else None,
        schedule_day=day,
        holiday=holiday,
        region_code=region_code,
    )


def month_total(days: tuple[DayExpectation, ...]) -> int:
    """The figure the ticket is about: the month's days, added up."""
    return sum(day.expected_minutes for day in days)


def applied_holidays(days: tuple[DayExpectation, ...]) -> tuple[Holiday, ...]:
    """The holiday rows that actually moved a figure, once each, in date order.

    A holiday that covered several days — or that several days' entries name —
    appears once: this is the calendar as it was, not a copy of the days.
    """
    seen: dict[UUID, Holiday] = {}
    for day in days:
        if day.holiday is not None:
            seen.setdefault(day.holiday.id, day.holiday)
    return tuple(sorted(seen.values(), key=lambda item: (item.date, str(item.id))))


def month_inputs(
    *,
    employee_id: UUID,
    year: int,
    month: int,
    days: tuple[DayExpectation, ...],
) -> dict:
    """The rules a month's figure was computed from, as they were.

    Frozen with the number because the number is worthless without them: four years
    from now the schedule will say something else, the department may have moved,
    and a holiday row may have been corrected — and the question will be "why was
    March 10,080 minutes", which only this document can answer.

    The per-day entries carry the window as well as the minutes. The minutes are
    what the obligation is about, and the window is what a reader asks about next;
    a document that carried only the total would be an assertion rather than
    evidence.
    """
    return {
        "employee_id": str(employee_id),
        "year": year,
        "month": month,
        "total_minutes": month_total(days),
        "region_codes": sorted({day.region_code for day in days if day.region_code}),
        "days": [_day_inputs(day) for day in days],
        "holidays": [_holiday_inputs(holiday) for holiday in applied_holidays(days)],
    }


def _day_inputs(day: DayExpectation) -> dict:
    window = day.schedule_day
    return {
        "date": day.business_date.isoformat(),
        "weekday": day.weekday,
        "region_code": day.region_code,
        "schedule_id": str(day.schedule_id) if day.schedule_id else None,
        "source": day.source.value,
        "expected_minutes": day.expected_minutes,
        "start_time": _clock(window.start_time if window else None),
        "end_time": _clock(window.end_time if window else None),
        "break_minutes": window.break_minutes if window else 0,
        "holiday": _holiday_inputs(day.holiday) if day.holiday is not None else None,
    }


def _clock(moment) -> str | None:  # noqa: ANN001 - datetime.time | None
    """A window's edge, to the minute.

    Seconds are refused by validation, so this loses nothing — and a frozen document
    that reads `08:00` is what somebody comparing it to a contract expects to see.
    """
    return moment.isoformat(timespec="minutes") if moment is not None else None


def _holiday_inputs(holiday: Holiday) -> dict:
    return {
        "id": str(holiday.id),
        "date": holiday.date.isoformat(),
        "name_es": holiday.name_es,
        "name_en": holiday.name_en,
        "scope": holiday.scope.value,
        "region_code": holiday.region_code,
    }


def _invalid(detail: str) -> DomainError:
    return DomainError(ScheduleErrorCode.SCHEDULE_INVALID_DAY, detail=detail)


__all__ = [
    "applied_holidays",
    "department_on",
    "expectation_for",
    "holidays_on",
    "month_dates",
    "month_inputs",
    "month_total",
    "most_specific",
    "resolve_schedule",
    "validate_day",
    "validate_schedule",
    "weekly_hours_of",
    "window_minutes",
]
