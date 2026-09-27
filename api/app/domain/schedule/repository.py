"""Persistence contract for the scheduling module.

Three things about this interface are load-bearing:

* **Everything that resolves a schedule takes dates.** `overrides_covering` takes a
  range, `assignments_between` takes a range, and `holidays_in_year` takes a year.
  Nothing here accepts an instant, for the reason `attendance/repository.py`
  records: a caller cannot confuse the business calendar with a timestamp if no
  method offers it one.
* **The employee directory is read, never written.** `assignments_between`,
  `employee_status` and `region_of` read tables the employee and organisation
  modules own. The scheduling module needs to know *where* somebody worked and
  *whether* they still do; it does not need to change either, and a write method
  here would be a second way to move somebody between departments.
* **Nothing commits.** The service commits once, so a holiday write and the cache
  stamp it moves land together, and a schedule and its days are one act.

`holiday_stamp` is the one method whose shape needs explaining: it returns a number
that changes whenever the year's rows do, and the cache key carries it. See
`app/domain/schedule/cache.py` for why the stamp is derived from the table rather
than kept as a counter.
"""

from datetime import date, datetime
from typing import Protocol
from uuid import UUID

from app.domain.schedule.models import (
    AssignmentSpan,
    ExpectedHoursSnapshot,
    Holiday,
    HolidayInput,
    OverrideInput,
    ScheduleInput,
    ScheduleOverride,
    WorkSchedule,
)


class ScheduleRepository(Protocol):
    # --- the employee side, read-only --------------------------------------

    async def employee_status(self, employee_id: UUID) -> str | None:
        """`employees.status`, or None when there is no such employee."""
        ...

    async def assignments_between(
        self, employee_id: UUID, from_date: date, to_date: date
    ) -> list[AssignmentSpan]:
        """Every assignment overlapping an inclusive range, oldest first.

        Overlapping rather than contained: somebody who moved departments in the
        middle of a month has two spans and the month needs both.
        """
        ...

    async def region_of(self, department_ids: list[UUID]) -> dict[UUID, str | None]:
        """`departments.region_code` for each id given, missing ids omitted."""
        ...

    async def active_employee_ids(self) -> list[UUID]:
        """Everybody whose record is not closed — the month-end pass's list."""
        ...

    # --- schedules ---------------------------------------------------------

    async def list_schedules(self, *, include_inactive: bool = False) -> list[WorkSchedule]:
        """The catalogue, by code."""
        ...

    async def get_schedule(self, schedule_id: UUID) -> WorkSchedule | None:
        """One schedule with its days."""
        ...

    async def schedule_for_department(self, department_id: UUID) -> WorkSchedule | None:
        """The active schedule configured for a department, if there is one."""
        ...

    async def default_schedule(self) -> WorkSchedule | None:
        """The active company default, if there is one."""
        ...

    async def code_taken(self, code: str, *, excluding: UUID | None = None) -> bool:
        """Whether another schedule already carries this code."""
        ...

    async def scope_taken(
        self,
        *,
        department_id: UUID | None,
        is_default: bool,
        excluding: UUID | None = None,
    ) -> bool:
        """Whether an active schedule already governs the scope this one claims.

        The scope is what the candidate *is*: a company default (asked about through
        `is_default`), a department's pattern, or neither — a catalogue pattern that
        only an employee override points at, which competes with nothing and is why
        the third answer is `False` rather than "the default scope".
        """
        ...

    async def save_schedule(
        self, data: ScheduleInput, *, schedule_id: UUID | None = None
    ) -> WorkSchedule:
        """Insert a schedule, or replace one and its days wholesale."""
        ...

    # --- overrides ---------------------------------------------------------

    async def overrides_covering(
        self, employee_id: UUID, from_date: date, to_date: date
    ) -> list[ScheduleOverride]:
        """Overrides in force on any day of an inclusive range, oldest first."""
        ...

    async def get_override(self, override_id: UUID) -> ScheduleOverride | None:
        ...

    async def overlapping_override(
        self,
        employee_id: UUID,
        effective_from: date,
        effective_to: date | None,
        *,
        excluding: UUID | None = None,
    ) -> ScheduleOverride | None:
        """An override for this employee whose window meets the one proposed."""
        ...

    async def save_override(
        self, data: OverrideInput, *, override_id: UUID | None = None
    ) -> ScheduleOverride:
        ...

    async def delete_override(self, override_id: UUID) -> None:
        """Remove one. An override is a decision, not evidence: ending it is
        ordinary administration, and the snapshots it produced keep their inputs."""
        ...

    # --- holidays ----------------------------------------------------------

    async def holidays_in_year(self, year: int) -> list[Holiday]:
        """Every holiday of one calendar year, in date order."""
        ...

    async def holiday_stamp(self, year: int) -> str | None:
        """A digest of this year's rows, or None when the year has none.

        A digest of the rows rather than a counter or a timestamp: the holiday table
        has a second writer (the import command, in its own process), and a
        correction typed into `psql` moves no column a counter would notice. See the
        implementation for what it covers and why the cache key carries it.
        """
        ...

    async def get_holiday(self, holiday_id: UUID) -> Holiday | None:
        ...

    async def find_holiday(
        self, on_date: date, scope: str, region_code: str | None
    ) -> Holiday | None:
        """The row an import would update — the uniqueness rule, as a read."""
        ...

    async def save_holiday(
        self, data: HolidayInput, *, holiday_id: UUID | None = None
    ) -> Holiday:
        """Insert a holiday, or update one to what the caller states."""
        ...

    async def delete_holiday(self, holiday_id: UUID) -> None:
        ...

    # --- snapshots ---------------------------------------------------------

    async def latest_snapshot(
        self, employee_id: UUID, year: int, month: int
    ) -> ExpectedHoursSnapshot | None:
        """The highest revision stored for that employee and month."""
        ...

    async def append_snapshot(
        self,
        *,
        employee_id: UUID,
        year: int,
        month: int,
        expected_minutes: int,
        inputs: dict,
        computed_by_employee_id: UUID | None,
        computed_at: datetime,
    ) -> ExpectedHoursSnapshot:
        """Write the next revision. The table refuses to update or delete one."""
        ...

    async def commit(self) -> None: ...


__all__ = ["ScheduleRepository"]
