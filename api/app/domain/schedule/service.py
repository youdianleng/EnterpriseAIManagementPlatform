"""The scheduling module: which pattern governs a day, and what a month was worth.

Three groups of operations, and the module docstring is the place the ticket's two
open questions are answered.

**On `expected_minutes` and the window: it is validated, not derived.** A day
states three things — a window, a break and the minutes expected — and the database
refuses a row where the third is not the first less the second (migration 0013's
`ck_work_schedule_days_consistent`, mirrored by `calculation.validate_day` so the
refusal arrives as a catalogued 422 rather than as a constraint name). Deriving the
minutes instead was the alternative, and it was rejected for two reasons. A reader
four years from now must be able to read the number rather than recompute it from a
window that may by then have been edited; and a window that does not add up — 09:00
to 14:00 described as a full eight-hour day — is a *mistake in the data*, which a
derivation would silently paper over. `weekly_hours` goes the other way and is
derived, because it is a sum of the days: a sum stated twice is a sum that
eventually disagrees with its parts.

**On a person's region: it is the department's, and the department gains the
field.** `departments.region_code` (added by migration 0013) is the region a
department works in, and a day's holidays are the national ones plus the regional
and local ones whose `region_code` matches it. The alternative — a `region_code` on
the employee — was rejected because a region is a fact about a *workplace*: two
people in one office observe the same local holidays, somebody who transfers to
another community observes that one's from the day they move, and a per-person
field would have to be maintained by hand and would eventually contradict the
building they sit in. `calculation.department_on` reads it from the primary
assignment in force on the date, so a transfer mid-year changes the calendar on the
day it happens, and an employee with no department (or a department with no region
configured) gets the national calendar and nothing else.

The rest of the module follows the shape the attendance module established: the
service is the only thing that fetches rows, `calculation` is pure arithmetic over
values, and nothing commits twice.
"""

from collections.abc import Sequence
from dataclasses import replace
from datetime import date
from re import compile as compile_pattern
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditAction, record
from app.core.errors import ErrorCode
from app.domain.attendance.business_day import madrid_today
from app.domain.attendance.models import utc_now
from app.domain.errors import DomainError
from app.domain.schedule.cache import HolidayCache
from app.domain.schedule.calculation import (
    applied_holidays,
    department_on,
    expectation_for,
    month_dates,
    month_inputs,
    month_total,
    resolve_schedule,
    validate_schedule,
)
from app.domain.schedule.errors import ScheduleErrorCode
from app.domain.schedule.models import (
    DayExpectation,
    Holiday,
    HolidayImportReport,
    HolidayInput,
    HolidayScope,
    MonthExpectation,
    OverrideInput,
    ResolvedSchedule,
    ScheduleDay,
    ScheduleDayInput,
    ScheduleInput,
    ScheduleOverride,
    SchedulePatch,
    ScheduleSource,
    SnapshotFailure,
    SnapshotReport,
    WorkSchedule,
)
from app.domain.schedule.repository import ScheduleRepository

#: The audit entity types. One string per table, so a filter on the trail reads
#: "everything that happened to the holiday calendar" as a prefix.
SCHEDULE_ENTITY = "work_schedule"
OVERRIDE_ENTITY = "employee_schedule_override"
HOLIDAY_ENTITY = "holiday"
SNAPSHOT_ENTITY = "expected_hours_snapshot"


class ScheduleService:
    """Resolution, maintenance and snapshots, in that order below.

    `session` is required because every write is audited in the same transaction as
    the change — a schedule edit and the record of who made it land together or not
    at all. `cache` is injectable so a test can drive the holiday lookup without
    Redis; the default is the Redis-backed one the design names.

    `now` is injectable for the same reason the attendance module's is: "which year
    is it" decides what a default query answers, and a test that depends on the day
    it runs on is not evidence.
    """

    def __init__(
        self,
        repository: ScheduleRepository,
        session: AsyncSession,
        *,
        cache: HolidayCache | None = None,
        now=None,  # noqa: ANN001 - a TimeSource, as attendance/models defines it
    ) -> None:
        self._repository = repository
        self._session = session
        self._cache = cache if cache is not None else HolidayCache()
        self._now = now or utc_now

    # --- resolution --------------------------------------------------------

    async def resolve(self, employee_id: UUID, on_date: date) -> ResolvedSchedule | None:
        """Which pattern governs this person on this date, and why.

        `None` means nobody has configured a schedule that reaches them, which is
        not the same fact as a schedule that expects nothing.

        The override is looked up again to name it, rather than being carried
        through `DayExpectation`: the day's answer needs the *effect* of an override
        (which schedule, which minutes) and a reader asking this question wants the
        row itself — so the second query happens only on the days one applies.
        """
        await self._require_employee(employee_id)
        expectations = await self._expectations(employee_id, (on_date,))
        expectation = expectations[on_date]
        if expectation.schedule_id is None:
            return None
        schedule = await self._repository.get_schedule(expectation.schedule_id)
        if schedule is None:  # pragma: no cover - the foreign key forbids it
            return None

        override_id: UUID | None = None
        if expectation.source is ScheduleSource.OVERRIDE:
            covering = await self._repository.overrides_covering(
                employee_id, on_date, on_date
            )
            override_id = next(
                (item.id for item in covering if item.covers(on_date)), None
            )
        return ResolvedSchedule(
            schedule=schedule, source=expectation.source, override_id=override_id
        )

    async def day_expectation(self, employee_id: UUID, business_date: date) -> DayExpectation:
        """One day, in the vocabulary the attendance module consumes."""
        await self._require_employee(employee_id)
        return (await self._expectations(employee_id, (business_date,)))[business_date]

    async def day_expectations(
        self, employee_id: UUID, from_date: date, to_date: date
    ) -> dict[date, DayExpectation]:
        """Every day of a range, in one pass.

        One query set for the range rather than one per day: a month's calendar is
        thirty days, and thirty round trips per read is how a calendar view becomes
        the slowest page in the system.
        """
        await self._require_employee(employee_id)
        return await self._expectations(employee_id, _dates_between(from_date, to_date))

    # --- expected hours ----------------------------------------------------

    async def expected_minutes(
        self, employee_id: UUID, year: int, month: int
    ) -> MonthExpectation:
        """A month's expected hours: the stored snapshot if there is one.

        The stored figure wins, which is the whole point of storing it. A month that
        has been snapshotted answers with what was agreed at the time, and a month
        that has not is computed from today's rules — so a schedule edited in
        December cannot reach back into March without somebody deliberately
        snapshotting March again.
        """
        await self._require_employee(employee_id)
        stored = await self._repository.latest_snapshot(employee_id, year, month)
        if stored is not None:
            return MonthExpectation(
                employee_id=employee_id,
                year=year,
                month=month,
                expected_minutes=stored.expected_minutes,
                inputs=stored.inputs,
                snapshot=stored,
            )
        return await self._derive_month(employee_id, year, month)

    async def snapshot_month(
        self,
        employee_id: UUID,
        year: int,
        month: int,
        *,
        computed_by_employee_id: UUID | None = None,
    ) -> MonthExpectation:
        """Freeze a month, with the rules that produced it, as a new revision.

        Idempotent in storage as well as in effect: a second run that computes the
        same inputs writes nothing, because a month-end pass that ran twice must not
        leave two identical revisions behind for a reader to choose between. A run
        whose inputs differ appends — nothing is rewritten, and the revision under
        it stays readable with the figure it produced.
        """
        await self._require_employee(employee_id)
        derived = await self._derive_month(employee_id, year, month)
        stored = await self._repository.latest_snapshot(employee_id, year, month)
        if (
            stored is not None
            and stored.expected_minutes == derived.expected_minutes
            and stored.inputs == derived.inputs
        ):
            return replace(derived, snapshot=stored)

        written = await self._repository.append_snapshot(
            employee_id=employee_id,
            year=year,
            month=month,
            expected_minutes=derived.expected_minutes,
            inputs=derived.inputs,
            computed_by_employee_id=computed_by_employee_id,
            computed_at=self._now(),
        )
        await record(
            self._session,
            action=AuditAction.EXPECTED_HOURS_SNAPSHOTTED,
            entity_type=SNAPSHOT_ENTITY,
            entity_id=written.id,
            before=(
                {"revision": stored.revision, "expected_minutes": stored.expected_minutes}
                if stored is not None
                else None
            ),
            after={
                "employee_id": str(employee_id),
                "year": year,
                "month": month,
                "revision": written.revision,
                "expected_minutes": written.expected_minutes,
            },
            initiated_by="user" if computed_by_employee_id else "system",
        )
        await self._repository.commit()
        return replace(derived, snapshot=written)

    async def snapshot_all(
        self, year: int, month: int, *, computed_by_employee_id: UUID | None = None
    ) -> SnapshotReport:
        """The month-end pass: every employee who still works here.

        One employee per transaction, because the repository commits inside
        `snapshot_month`: a pass that failed half way through the company would
        otherwise leave the month unsnapshotted for everybody rather than for the
        one person whose data was wrong. The failures are reported rather than
        raised, and the caller decides what to do with them.
        """
        written: list[MonthExpectation] = []
        failed: list[SnapshotFailure] = []
        for employee_id in await self._repository.active_employee_ids():
            try:
                written.append(
                    await self.snapshot_month(
                        employee_id,
                        year,
                        month,
                        computed_by_employee_id=computed_by_employee_id,
                    )
                )
            except Exception as error:  # noqa: BLE001 - reported, never fatal
                code = (
                    error.code.value
                    if isinstance(error, DomainError)
                    else ErrorCode.INTERNAL_ERROR.value
                )
                failed.append(
                    SnapshotFailure(
                        employee_id=employee_id, code=code, detail=str(error)
                    )
                )
        return SnapshotReport(snapshotted=tuple(written), failed=tuple(failed))

    # --- schedules, maintenance -------------------------------------------

    async def list_schedules(self, *, include_inactive: bool = False) -> list[WorkSchedule]:
        return await self._repository.list_schedules(include_inactive=include_inactive)

    async def get_schedule(self, schedule_id: UUID) -> WorkSchedule:
        return await self._require_schedule(schedule_id)

    async def get_override(self, override_id: UUID) -> ScheduleOverride:
        override = await self._repository.get_override(override_id)
        if override is None:
            raise DomainError(
                ScheduleErrorCode.SCHEDULE_OVERRIDE_NOT_FOUND,
                detail=f"unknown override {override_id}",
            )
        return override

    async def create_schedule(
        self,
        data: ScheduleInput,
        *,
        actor_user_id: UUID | None = None,
        actor_roles: frozenset[str] | None = None,
    ) -> WorkSchedule:
        """Write a weekly pattern, having refused one that could never apply."""
        validate_schedule(data.days)
        if await self._repository.code_taken(data.code):
            raise DomainError(
                ScheduleErrorCode.SCHEDULE_CODE_TAKEN, detail=f"code {data.code} is in use"
            )
        await self._require_scope_free(data.department_id, data.is_default, data.is_active)

        schedule = await self._repository.save_schedule(data)
        await record(
            self._session,
            action=AuditAction.SCHEDULE_CREATED,
            entity_type=SCHEDULE_ENTITY,
            entity_id=schedule.id,
            after=_schedule_audit(schedule),
            actor_user_id=actor_user_id,
            actor_roles=actor_roles,
        )
        await self._repository.commit()
        return schedule

    async def update_schedule(
        self,
        schedule_id: UUID,
        patch: SchedulePatch,
        *,
        actor_user_id: UUID | None = None,
        actor_roles: frozenset[str] | None = None,
    ) -> WorkSchedule:
        """Replace what the caller states, and leave the rest as it was.

        The days are replaced wholesale rather than merged per weekday: a week is
        read as a whole ("Monday to Thursday eight hours, Friday six"), and a merge
        would make removing a day impossible to express. The `code` is deliberately
        not patchable — it is what a report and a later import name a schedule by,
        and changing it would rewrite what an old report meant.
        """
        current = await self._require_schedule(schedule_id)
        data = ScheduleInput(
            code=current.code,
            name_es=patch.name_es or current.name_es,
            name_en=patch.name_en or current.name_en,
            days=patch.days if patch.days is not None else _as_inputs(current.days),
            department_id=current.department_id,
            is_default=patch.is_default if patch.is_default is not None else current.is_default,
            is_active=patch.is_active if patch.is_active is not None else current.is_active,
        )
        validate_schedule(data.days)
        await self._require_scope_free(
            data.department_id, data.is_default, data.is_active, excluding=schedule_id
        )

        schedule = await self._repository.save_schedule(data, schedule_id=schedule_id)
        await record(
            self._session,
            action=AuditAction.SCHEDULE_UPDATED,
            entity_type=SCHEDULE_ENTITY,
            entity_id=schedule.id,
            before=_schedule_audit(current),
            after=_schedule_audit(schedule),
            actor_user_id=actor_user_id,
            actor_roles=actor_roles,
        )
        await self._repository.commit()
        return schedule

    async def set_override(
        self,
        data: OverrideInput,
        *,
        override_id: UUID | None = None,
        actor_user_id: UUID | None = None,
        actor_roles: frozenset[str] | None = None,
    ) -> ScheduleOverride:
        """Give one person a different week, for a window of dates.

        The windows must not overlap, and the database refuses it as well as this
        method (an exclusion constraint): which of two overrides wins on a shared
        day is not a question the resolver should ever have to answer.
        """
        await self._require_employee(data.employee_id)
        schedule = await self._require_schedule(data.schedule_id)
        if not schedule.is_active:
            raise DomainError(
                ScheduleErrorCode.SCHEDULE_INACTIVE,
                detail=f"schedule {schedule.code} is deactivated",
            )
        if not data.reason or not data.reason.strip():
            raise DomainError(
                ScheduleErrorCode.INVALID_REQUEST,
                detail="an override states why this person's week differs",
            )
        overlapping = await self._repository.overlapping_override(
            data.employee_id,
            data.effective_from,
            data.effective_to,
            excluding=override_id,
        )
        if overlapping is not None:
            raise DomainError(
                ScheduleErrorCode.SCHEDULE_OVERRIDE_OVERLAPS,
                detail=(
                    f"override {overlapping.id} covers "
                    f"{overlapping.effective_from}..{overlapping.effective_to or 'open'}"
                ),
            )

        saved = await self._repository.save_override(data, override_id=override_id)
        await record(
            self._session,
            action=AuditAction.SCHEDULE_OVERRIDE_SET,
            entity_type=OVERRIDE_ENTITY,
            entity_id=saved.id,
            after=_override_audit(saved),
            reason=data.reason.strip(),
            actor_user_id=actor_user_id,
            actor_roles=actor_roles,
        )
        await self._repository.commit()
        return saved

    async def delete_override(
        self,
        override_id: UUID,
        *,
        actor_user_id: UUID | None = None,
        actor_roles: frozenset[str] | None = None,
    ) -> None:
        """Remove one, for a window that was written down wrongly.

        Deletion rather than narrowing the window to nothing: an override is a
        decision about dates, and one entered in error has no first day it could
        legitimately end on. What the override *produced* is unaffected — a stored
        snapshot carries the minutes it was computed from, not a pointer to this row.
        """
        override = await self._repository.get_override(override_id)
        if override is None:
            raise DomainError(
                ScheduleErrorCode.SCHEDULE_OVERRIDE_NOT_FOUND,
                detail=f"unknown override {override_id}",
            )
        await self._repository.delete_override(override_id)
        await record(
            self._session,
            action=AuditAction.SCHEDULE_OVERRIDE_REMOVED,
            entity_type=OVERRIDE_ENTITY,
            entity_id=override_id,
            before=_override_audit(override),
            actor_user_id=actor_user_id,
            actor_roles=actor_roles,
        )
        await self._repository.commit()

    # --- holidays, maintenance --------------------------------------------

    async def list_holidays(
        self, *, year: int | None = None, scope: HolidayScope | None = None,
        region_code: str | None = None,
    ) -> list[Holiday]:
        """A year's calendar, cached. Writes move the cache key, so a write is
        visible to the very next read — see `schedule/cache.py`."""
        if year is None:
            year = madrid_today(self._now()).year
        holidays = await self._holidays(year)
        return [
            holiday
            for holiday in holidays
            if (scope is None or holiday.scope is scope)
            and (region_code is None or holiday.region_code == region_code)
        ]

    async def add_holiday(
        self,
        data: HolidayInput,
        *,
        actor_user_id: UUID | None = None,
        actor_roles: frozenset[str] | None = None,
    ) -> Holiday:
        validate_holiday(data)
        existing = await self._repository.find_holiday(
            data.date, data.scope.value, data.region_code
        )
        if existing is not None:
            raise DomainError(
                ScheduleErrorCode.SCHEDULE_HOLIDAY_EXISTS,
                detail=(
                    f"{data.date} is already a {data.scope} holiday"
                    + (f" in {data.region_code}" if data.region_code else "")
                ),
            )
        holiday = await self._repository.save_holiday(data)
        await record(
            self._session,
            action=AuditAction.HOLIDAY_CREATED,
            entity_type=HOLIDAY_ENTITY,
            entity_id=holiday.id,
            after=_holiday_audit(holiday),
            actor_user_id=actor_user_id,
            actor_roles=actor_roles,
        )
        await self._repository.commit()
        return holiday

    async def update_holiday(
        self,
        holiday_id: UUID,
        data: HolidayInput,
        *,
        actor_user_id: UUID | None = None,
        actor_roles: frozenset[str] | None = None,
    ) -> Holiday:
        """Correct one. The date, scope and region are its identity, so changing
        them is what the conflict check below is for."""
        current = await self._require_holiday(holiday_id)
        validate_holiday(data)
        clash = await self._repository.find_holiday(
            data.date, data.scope.value, data.region_code
        )
        if clash is not None and clash.id != holiday_id:
            raise DomainError(
                ScheduleErrorCode.SCHEDULE_HOLIDAY_EXISTS,
                detail=f"{data.date} already carries that holiday",
            )
        holiday = await self._repository.save_holiday(data, holiday_id=holiday_id)
        await record(
            self._session,
            action=AuditAction.HOLIDAY_UPDATED,
            entity_type=HOLIDAY_ENTITY,
            entity_id=holiday.id,
            before=_holiday_audit(current),
            after=_holiday_audit(holiday),
            actor_user_id=actor_user_id,
            actor_roles=actor_roles,
        )
        await self._repository.commit()
        return holiday

    async def delete_holiday(
        self,
        holiday_id: UUID,
        *,
        actor_user_id: UUID | None = None,
        actor_roles: frozenset[str] | None = None,
    ) -> None:
        """Remove one that should not have been there.

        Deleting does not rewrite any figure that was already computed: a stored
        snapshot carries the holiday rows it applied, so the record of what March
        was measured against survives the calendar being corrected.
        """
        holiday = await self._require_holiday(holiday_id)
        await self._repository.delete_holiday(holiday_id)
        await record(
            self._session,
            action=AuditAction.HOLIDAY_DELETED,
            entity_type=HOLIDAY_ENTITY,
            entity_id=holiday_id,
            before=_holiday_audit(holiday),
            actor_user_id=actor_user_id,
            actor_roles=actor_roles,
        )
        await self._repository.commit()

    async def import_holidays(
        self,
        rows: Sequence[HolidayInput],
        *,
        source: str | None = None,
        actor_user_id: UUID | None = None,
        actor_roles: frozenset[str] | None = None,
    ) -> HolidayImportReport:
        """Write a year's calendar, row by row, as one transaction.

        **Every row is validated before any is written.** The caller parses and
        validates the file (`schedule/importer.py`), so a file with one bad line
        never reaches here; this validates the values themselves, and a failure
        mid-way rolls the whole import back because nothing commits until the end.
        A half-imported holiday calendar is a payroll figure that is half wrong, and
        the caller cannot tell which half.

        Re-importing the same file changes nothing: a row is matched on its
        identity — date, scope and region — and reported as `unchanged`.
        """
        created = updated = unchanged = 0
        for row in rows:
            validate_holiday(row)
            existing = await self._repository.find_holiday(
                row.date, row.scope.value, row.region_code
            )
            if existing is None:
                await self._repository.save_holiday(row)
                created += 1
            elif _same_holiday(existing, row):
                unchanged += 1
            else:
                await self._repository.save_holiday(row, holiday_id=existing.id)
                updated += 1

        report = HolidayImportReport(
            created=created, updated=updated, unchanged=unchanged, total=len(rows)
        )
        await record(
            self._session,
            action=AuditAction.HOLIDAYS_IMPORTED,
            entity_type=HOLIDAY_ENTITY,
            entity_id=None,
            after={
                "created": created,
                "updated": updated,
                "unchanged": unchanged,
                "total": len(rows),
                "source": source,
            },
            actor_user_id=actor_user_id,
            actor_roles=actor_roles,
        )
        await self._repository.commit()
        return report

    # --- internals: resolution --------------------------------------------

    async def _expectations(
        self, employee_id: UUID, dates: tuple[date, ...]
    ) -> dict[date, DayExpectation]:
        """Every day asked for, on one pass over the rows that decide them.

        The fallback chain, the region and the calendar are each fetched once for
        the whole range: an override is one row and a month is thirty lookups, so
        the naive shape of this method is thirty times the queries for the same
        answer.
        """
        if not dates:
            return {}

        first, last = dates[0], dates[-1]
        spans = await self._repository.assignments_between(employee_id, first, last)
        overrides = await self._repository.overrides_covering(employee_id, first, last)
        default = await self._repository.default_schedule()

        schedules: dict[UUID, WorkSchedule | None] = {}
        by_department: dict[UUID | None, WorkSchedule | None] = {}
        regions: dict[UUID | None, str | None] = {None: None}
        calendars: dict[int, tuple[Holiday, ...]] = {}
        expectations: dict[date, DayExpectation] = {}

        for day in dates:
            department_id = department_on(spans, day)
            if department_id not in by_department:
                by_department[department_id] = (
                    await self._repository.schedule_for_department(department_id)
                    if department_id is not None
                    else None
                )
            if department_id not in regions:
                found = await self._repository.region_of([department_id])
                regions[department_id] = found.get(department_id)

            override = next((item for item in overrides if item.covers(day)), None)
            override_schedule: WorkSchedule | None = None
            if override is not None:
                if override.schedule_id not in schedules:
                    schedules[override.schedule_id] = await self._repository.get_schedule(
                        override.schedule_id
                    )
                override_schedule = schedules[override.schedule_id]

            if day.year not in calendars:
                calendars[day.year] = await self._holidays(day.year)

            expectations[day] = expectation_for(
                employee_id=employee_id,
                business_date=day,
                resolved=resolve_schedule(
                    override_schedule=override_schedule,
                    department_schedule=by_department[department_id],
                    default_schedule=default,
                    override_id=override.id if override is not None else None,
                ),
                holidays=calendars[day.year],
                region_code=regions[department_id],
            )
        return expectations

    async def _holidays(self, year: int) -> tuple[Holiday, ...]:
        """A year's rows, preferring the cached copy.

        The stamp is read from the table on every call, including the ones that hit
        the cache: it is what makes an edit — through this service, through the
        import command in another process, or through a `psql` session — visible to
        the next read rather than after a TTL.
        """
        stamp = await self._repository.holiday_stamp(year)
        if stamp is None:
            return ()
        cached = await self._cache.read(year, stamp)
        if cached is not None:
            return cached
        rows = tuple(await self._repository.holidays_in_year(year))
        await self._cache.write(year, stamp, rows)
        return rows

    async def _derive_month(self, employee_id: UUID, year: int, month: int) -> MonthExpectation:
        dates = month_dates(year, month)
        expectations = await self._expectations(employee_id, dates)
        days = tuple(expectations[day] for day in dates)
        return MonthExpectation(
            employee_id=employee_id,
            year=year,
            month=month,
            expected_minutes=month_total(days),
            inputs=month_inputs(employee_id=employee_id, year=year, month=month, days=days),
            days=days,
            holidays=applied_holidays(days),
        )

    # --- internals: lookups ------------------------------------------------

    async def _require_employee(self, employee_id: UUID) -> str:
        status = await self._repository.employee_status(employee_id)
        if status is None:
            raise DomainError(
                ScheduleErrorCode.EMPLOYEE_NOT_FOUND, detail=f"unknown employee {employee_id}"
            )
        return status

    async def _require_schedule(self, schedule_id: UUID) -> WorkSchedule:
        schedule = await self._repository.get_schedule(schedule_id)
        if schedule is None:
            raise DomainError(
                ScheduleErrorCode.SCHEDULE_NOT_FOUND, detail=f"unknown schedule {schedule_id}"
            )
        return schedule

    async def _require_holiday(self, holiday_id: UUID) -> Holiday:
        holiday = await self._repository.get_holiday(holiday_id)
        if holiday is None:
            raise DomainError(
                ScheduleErrorCode.SCHEDULE_HOLIDAY_NOT_FOUND, detail=f"unknown holiday {holiday_id}"
            )
        return holiday

    async def _require_scope_free(
        self,
        department_id: UUID | None,
        is_default: bool,
        is_active: bool,
        *,
        excluding: UUID | None = None,
    ) -> None:
        """One active schedule per scope, and the default belongs to nobody.

        Only checked for an active schedule: deactivating the old one and adding a
        new one in the same breath is a legitimate way to replace a pattern, and the
        partial unique index is written the same way.
        """
        if is_default and department_id is not None:
            raise DomainError(
                ScheduleErrorCode.INVALID_REQUEST,
                detail="a company default belongs to no department",
            )
        if not is_active:
            return
        if await self._repository.scope_taken(
            department_id=department_id, is_default=is_default, excluding=excluding
        ):
            scope = (
                "the company default"
                if is_default
                else f"department {department_id}"
            )
            raise DomainError(
                ScheduleErrorCode.SCHEDULE_ALREADY_SET,
                detail=f"{scope} already has an active schedule",
            )


def validate_holiday(data: HolidayInput) -> None:
    """The rules the holiday table states, in the caller's vocabulary.

    National holidays carry no region and regional and local ones must: matching is
    by region code, so a regional holiday without one would look like a holiday and
    change nobody's figure.
    """
    if not data.name_es.strip() or not data.name_en.strip():
        raise _holiday_refused("a holiday is named in both languages")
    if data.scope is HolidayScope.NATIONAL and data.region_code is not None:
        raise _holiday_refused(
            f"{data.date} is national, so it names no region; a holiday that applies "
            "everywhere cannot be narrowed by one"
        )
    if data.scope is not HolidayScope.NATIONAL and not data.region_code:
        raise _holiday_refused(
            f"{data.date} is {data.scope}, so it names the region it is observed in"
        )
    if data.region_code is not None and not _REGION_CODE.match(data.region_code):
        raise _holiday_refused(
            f"{data.region_code!r} is not a region code; they look like ES-MD or ES-MD-28079"
        )


def _holiday_refused(detail: str) -> DomainError:
    return DomainError(ScheduleErrorCode.SCHEDULE_INVALID_HOLIDAY, detail=detail)


def _same_holiday(existing: Holiday, row: HolidayInput) -> bool:
    return (
        existing.date == row.date
        and existing.name_es == row.name_es
        and existing.name_en == row.name_en
        and existing.scope is row.scope
        and existing.region_code == row.region_code
    )


def _as_inputs(days: tuple[ScheduleDay, ...]) -> tuple[ScheduleDayInput, ...]:
    return tuple(
        ScheduleDayInput(
            weekday=day.weekday,
            expected_minutes=day.expected_minutes,
            start_time=day.start_time,
            end_time=day.end_time,
            break_minutes=day.break_minutes,
        )
        for day in days
    )


def _schedule_audit(schedule: WorkSchedule) -> dict:
    """What a reader of the trail needs: the week, not the row."""
    return {
        "code": schedule.code,
        "name_es": schedule.name_es,
        "name_en": schedule.name_en,
        "department_id": str(schedule.department_id) if schedule.department_id else None,
        "weekly_hours": str(schedule.weekly_hours),
        "is_default": schedule.is_default,
        "is_active": schedule.is_active,
        "days": [
            {
                "weekday": day.weekday,
                "expected_minutes": day.expected_minutes,
                "start_time": day.start_time.isoformat() if day.start_time else None,
                "end_time": day.end_time.isoformat() if day.end_time else None,
                "break_minutes": day.break_minutes,
            }
            for day in schedule.days
        ],
    }


def _override_audit(override: ScheduleOverride) -> dict:
    return {
        "employee_id": str(override.employee_id),
        "schedule_id": str(override.schedule_id),
        "effective_from": override.effective_from,
        "effective_to": override.effective_to,
        "reason": override.reason,
    }


def _holiday_audit(holiday: Holiday) -> dict:
    return {
        "date": holiday.date,
        "name_es": holiday.name_es,
        "name_en": holiday.name_en,
        "scope": holiday.scope,
        "region_code": holiday.region_code,
        "year": holiday.year,
    }


def _dates_between(from_date: date, to_date: date) -> tuple[date, ...]:
    if to_date < from_date:
        return ()
    return tuple(
        date.fromordinal(ordinal)
        for ordinal in range(from_date.toordinal(), to_date.toordinal() + 1)
    )


#: ISO 3166-2, and the municipality level under it (`ES-MD-28079`). Deliberately a
#: shape check rather than a list of codes: the codes belong to the holiday rows,
#: and a hardcoded list of Spanish regions in Python is exactly the hardcoding Q22
#: rules out.
_REGION_CODE = compile_pattern(r"^[A-Z]{2}(-[A-Z0-9]{1,5}){0,2}$")


__all__ = ["HOLIDAY_ENTITY", "OVERRIDE_ENTITY", "SCHEDULE_ENTITY", "ScheduleService"]
