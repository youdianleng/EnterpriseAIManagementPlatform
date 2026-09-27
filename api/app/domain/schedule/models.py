"""Schedule value objects: the pattern, the day's answer, and the frozen month.

The vocabulary is small and each member exists because a later question needs it:

* `WorkSchedule` is a weekly pattern — seven possible days, a start, an end and a
  break — plus the department it belongs to (null for the company default).
* `ResolvedSchedule` is *which* pattern governs one person on one date, and *why*:
  an override beats the department's schedule, which beats the company default.
  The `source` is carried rather than inferred because "why is Friday six hours"
  is the question a reader has four years later, and the answer must be in the
  record rather than in somebody's memory of the org chart.
* `DayExpectation` is the answer for one date: how many minutes the schedule
  expects, which schedule said so, and whether a holiday zeroed it.
* `MonthExpectation` is a month of those, either derived now or read back from a
  stored snapshot. `snapshot` being non-null is what tells them apart.

**Everything is keyed by date, like every other attendance question.** A person's
schedule changes over time, so "what does Ana work" has no answer; "what did Ana
work on 12 March 2026" has exactly one (`docs/architecture/codebase-design.md`
§2.4, and the same reasoning as `attendance/business_day.py`).

**Weekday is 0 for Monday** throughout, which is Python's `date.weekday()`.
"""

from dataclasses import dataclass
from datetime import date, time
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

#: The seven weekdays, as `date.weekday()` numbers them.
WEEKDAYS: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)

#: What a full day can hold. A schedule that expected more than this would be
#: expecting somebody to work a longer day than the clock has in it.
MINUTES_PER_DAY = 1440

#: Minutes in an hour, named because `weekly_hours` is the one place the codebase
#: converts between the two units and the conversion is where a factor of sixty
#: goes missing.
MINUTES_PER_HOUR = 60


class HolidayScope(StrEnum):
    """Which administration declared a holiday.

    Descriptive, and the matching rule deliberately does not branch on it: a
    holiday applies to a person when its `region_code` is theirs, and a national
    one has no region at all. See `models.Holiday.applies_to`.
    """

    NATIONAL = "national"
    REGIONAL = "regional"
    LOCAL = "local"


class ScheduleSource(StrEnum):
    """Where a day's schedule came from. The fallback chain, named."""

    #: An `employee_schedule_overrides` row covers the date.
    OVERRIDE = "override"
    #: The schedule configured for the employee's department.
    DEPARTMENT = "department"
    #: The company default schedule.
    DEFAULT = "default"
    #: Nobody has configured anything, so nothing is expected. Not the same as
    #: zero minutes: zero is a decision, and this is the absence of one.
    NONE = "none"


@dataclass(slots=True, frozen=True)
class ScheduleDay:
    """One weekday of a pattern. Days with no row are days nobody works."""

    weekday: int
    expected_minutes: int
    start_time: time | None = None
    end_time: time | None = None
    break_minutes: int = 0


@dataclass(slots=True, frozen=True)
class WorkSchedule:
    """A weekly pattern, with its days."""

    id: UUID
    code: str
    name_es: str
    name_en: str
    weekly_hours: Decimal
    is_default: bool
    is_active: bool
    department_id: UUID | None = None
    days: tuple[ScheduleDay, ...] = ()

    def day(self, weekday: int) -> ScheduleDay | None:
        return next((day for day in self.days if day.weekday == weekday), None)

    def minutes_on(self, weekday: int) -> int:
        """Minutes expected on that weekday — zero where there is no row."""
        day = self.day(weekday)
        return day.expected_minutes if day is not None else 0


@dataclass(slots=True, frozen=True)
class AssignmentSpan:
    """One position assignment, as a span of dates.

    Read rather than modelled: the employee module owns assignments, and this is
    the four fields the scheduling module needs — where, from when, to when, and
    whether it was the primary one. It is what decides which department's schedule
    and which region a day belongs to.
    """

    department_id: UUID
    start_date: date
    is_primary: bool
    end_date: date | None = None

    def covers(self, on_date: date) -> bool:
        if on_date < self.start_date:
            return False
        return self.end_date is None or on_date <= self.end_date


@dataclass(slots=True, frozen=True)
class ScheduleOverride:
    """One person's pattern for a window of dates, and the reason for it."""

    id: UUID
    employee_id: UUID
    schedule_id: UUID
    effective_from: date
    reason: str
    effective_to: date | None = None
    created_by_employee_id: UUID | None = None

    def covers(self, on_date: date) -> bool:
        """Whether this override is in force on a date, both ends included.

        Inclusive at both ends because both ends are days somebody worked: an
        override from the 1st to the 15th is in force on the 15th, and a
        half-open window would move somebody's hours on the last day of it.
        """
        if on_date < self.effective_from:
            return False
        return self.effective_to is None or on_date <= self.effective_to


@dataclass(slots=True, frozen=True)
class Holiday:
    """A day nobody works, as stored."""

    id: UUID
    date: date
    name_es: str
    name_en: str
    scope: HolidayScope
    year: int
    region_code: str | None = None

    def applies_to(self, region_code: str | None) -> bool:
        """Whether this holiday is observed by somebody working in `region_code`.

        One rule, and the scope does not enter into it: a holiday applies when it
        names no region (a national one, which everybody observes) or names the
        region the person works in. A person whose department has no region
        configured gets the national calendar and nothing else, which is the
        honest answer rather than a guess at a region.
        """
        return self.region_code is None or self.region_code == region_code


@dataclass(slots=True, frozen=True)
class ResolvedSchedule:
    """Which pattern governs one person on one date, and why."""

    schedule: WorkSchedule
    source: ScheduleSource
    override_id: UUID | None = None

    def day(self, weekday: int) -> ScheduleDay | None:
        return self.schedule.day(weekday)

    def minutes_on(self, weekday: int) -> int:
        return self.schedule.minutes_on(weekday)


@dataclass(slots=True, frozen=True)
class DayExpectation:
    """What the rules say about one person's one day.

    `expected_minutes` is zero for a rest day and for a holiday alike, and
    `holiday` is what tells them apart — which matters, because a day nobody was
    expected to work is not an absence, and a labour record that calls it one is
    wrong in the direction people complain about.

    `region_code` is carried for the same reason `source` is: the figure is only
    reproducible if the inputs that produced it are readable beside it.
    """

    employee_id: UUID
    business_date: date
    expected_minutes: int
    source: ScheduleSource
    weekday: int
    schedule_id: UUID | None = None
    schedule_day: ScheduleDay | None = None
    holiday: Holiday | None = None
    region_code: str | None = None

    @property
    def is_working_day(self) -> bool:
        """Whether anybody was expected. False for weekends and holidays."""
        return self.expected_minutes > 0

    @property
    def is_holiday(self) -> bool:
        return self.holiday is not None


@dataclass(slots=True, frozen=True)
class ExpectedHoursSnapshot:
    """A stored month, with the inputs it was computed from. Never rewritten."""

    id: UUID
    employee_id: UUID
    year: int
    month: int
    revision: int
    expected_minutes: int
    inputs: dict
    computed_at: object | None = None
    computed_by_employee_id: UUID | None = None


@dataclass(slots=True, frozen=True)
class MonthExpectation:
    """One employee's month: the figure, how it was reached, and what said so.

    `snapshot` is null for a month computed to answer a read and never written
    down, and `days` is empty for one read back from a snapshot — the days are in
    `inputs` in that case, exactly as they were when the figure was frozen.
    """

    employee_id: UUID
    year: int
    month: int
    expected_minutes: int
    inputs: dict
    days: tuple[DayExpectation, ...] = ()
    holidays: tuple[Holiday, ...] = ()
    snapshot: ExpectedHoursSnapshot | None = None

    @property
    def is_snapshot(self) -> bool:
        return self.snapshot is not None


@dataclass(slots=True, frozen=True)
class SnapshotFailure:
    """One employee whose month could not be frozen, and why."""

    employee_id: UUID
    code: str
    detail: str


@dataclass(slots=True, frozen=True)
class SnapshotReport:
    """What the month-end pass did, employee by employee.

    A failure is carried rather than raised, for the reason the personnel applier
    gives: one person with data nobody can compute must not stop the other
    ninety-nine from being frozen, and a pass that raised would be a scheduler
    reporting failure for ever.
    """

    snapshotted: tuple[MonthExpectation, ...] = ()
    failed: tuple[SnapshotFailure, ...] = ()

    @property
    def written(self) -> int:
        return len(self.snapshotted)


@dataclass(slots=True, frozen=True)
class HolidayImportReport:
    """What an import did, per row rather than in total.

    "Imported" and "was already there" have to be distinguishable: a second run of
    the same file that reports twelve insertions is a file that was applied
    without the uniqueness rule working.
    """

    created: int = 0
    updated: int = 0
    unchanged: int = 0
    total: int = 0


@dataclass(slots=True)
class ScheduleDayInput:
    """One weekday as a caller states it."""

    weekday: int
    expected_minutes: int
    start_time: time | None = None
    end_time: time | None = None
    break_minutes: int = 0


@dataclass(slots=True)
class ScheduleInput:
    """A schedule as a caller states it."""

    code: str
    name_es: str
    name_en: str
    days: tuple[ScheduleDayInput, ...]
    department_id: UUID | None = None
    is_default: bool = False
    is_active: bool = True


@dataclass(slots=True)
class SchedulePatch:
    """A change to a schedule. Absent means "leave it as it was".

    `code` is deliberately absent: it is what a report and a later import name a
    schedule by, so changing it would change what an old report meant. `days`
    replaces the whole week rather than merging into it, because a week is read as
    a whole and a merge could not express removing a day.
    """

    name_es: str | None = None
    name_en: str | None = None
    days: tuple[ScheduleDayInput, ...] | None = None
    is_default: bool | None = None
    is_active: bool | None = None


@dataclass(slots=True)
class OverrideInput:
    """One person's deviation from their department's week."""

    employee_id: UUID
    schedule_id: UUID
    effective_from: date
    reason: str
    effective_to: date | None = None
    created_by_employee_id: UUID | None = None


@dataclass(slots=True)
class HolidayInput:
    """A holiday as a caller states it, or as a file carries it.

    `year` is derived from `date` by the service and is not a field here: a year
    that a caller can state is a year that can disagree with the date it belongs
    to, and the database refuses the disagreement anyway.
    """

    date: date
    name_es: str
    name_en: str
    scope: HolidayScope
    region_code: str | None = None


__all__ = [
    "MINUTES_PER_DAY",
    "MINUTES_PER_HOUR",
    "WEEKDAYS",
    "AssignmentSpan",
    "DayExpectation",
    "ExpectedHoursSnapshot",
    "Holiday",
    "HolidayImportReport",
    "HolidayInput",
    "HolidayScope",
    "MonthExpectation",
    "OverrideInput",
    "ResolvedSchedule",
    "ScheduleDay",
    "ScheduleDayInput",
    "ScheduleInput",
    "ScheduleOverride",
    "SchedulePatch",
    "ScheduleSource",
    "SnapshotFailure",
    "SnapshotReport",
    "WorkSchedule",
]
