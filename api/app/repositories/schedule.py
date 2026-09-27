"""PostgreSQL implementation of the scheduling repository.

Four things here are worth reading before the SQL:

* **`save_schedule` replaces the week.** The days are deleted and rewritten inside
  one statement pair rather than merged: a pattern is read as a whole, and an
  update that only touched the days it was given could not express "Friday is no
  longer worked". The delete and the insert are in the caller's transaction, so a
  schedule is never briefly dayless in a way anybody can see.
* **`holiday_stamp` is the cache key, and it is a hash of the rows.** `count`, the
  newest `updated_at` and the newest `id` — everything a write to this year's table
  can move. `NULL` for a year with no rows: "there is nothing to cache" and "the
  cached answer is empty" have to be told apart, because a first write must not be
  served the empty answer it cached a moment earlier.
* **`append_snapshot` numbers the revision in the statement that inserts it.**
  `COALESCE(MAX(revision), 0) + 1` inside the INSERT, so two callers cannot both
  decide they are revision 2 — the unique constraint refuses the loser rather than
  silently producing two rows a reader would have to choose between.
* **`holiday_stamp` and `find_holiday` are reads with no joins**, so the module
  stays a leaf: nothing here writes `employees`, `departments` or anything else it
  does not own.
"""

from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.schedule.calculation import weekly_hours_of
from app.domain.schedule.models import (
    AssignmentSpan,
    ExpectedHoursSnapshot,
    Holiday,
    HolidayInput,
    HolidayScope,
    OverrideInput,
    ScheduleDay,
    ScheduleInput,
    ScheduleOverride,
    WorkSchedule,
)
from app.models.employee import Employee as EmployeeRow
from app.models.org import Department as DepartmentRow
from app.models.schedule import EmployeeScheduleOverride as OverrideRow
from app.models.schedule import ExpectedHoursSnapshot as SnapshotRow
from app.models.schedule import Holiday as HolidayRow
from app.models.schedule import WorkSchedule as ScheduleRow
from app.models.schedule import WorkScheduleDay as DayRow

#: The statuses that are not a closed record. Ticket 18 owns what termination
#: means; the month-end pass only needs to know who is still here.
TERMINATED_STATUS = "terminated"

def _to_schedule(row: ScheduleRow, days: list[DayRow]) -> WorkSchedule:
    return WorkSchedule(
        id=row.id,
        code=row.code,
        name_es=row.name_es,
        name_en=row.name_en,
        weekly_hours=row.weekly_hours,
        is_default=row.is_default,
        is_active=row.is_active,
        department_id=row.department_id,
        days=tuple(
            ScheduleDay(
                weekday=day.weekday,
                expected_minutes=day.expected_minutes,
                start_time=day.start_time,
                end_time=day.end_time,
                break_minutes=day.break_minutes,
            )
            for day in sorted(days, key=lambda item: item.weekday)
        ),
    )


def _to_override(row: OverrideRow) -> ScheduleOverride:
    return ScheduleOverride(
        id=row.id,
        employee_id=row.employee_id,
        schedule_id=row.schedule_id,
        effective_from=row.effective_from,
        effective_to=row.effective_to,
        reason=row.reason,
        created_by_employee_id=row.created_by_employee_id,
    )


def _to_holiday(row: HolidayRow) -> Holiday:
    return Holiday(
        id=row.id,
        date=row.date,
        name_es=row.name_es,
        name_en=row.name_en,
        scope=HolidayScope(row.scope),
        region_code=row.region_code,
        year=row.year,
    )


def _to_snapshot(row: SnapshotRow) -> ExpectedHoursSnapshot:
    return ExpectedHoursSnapshot(
        id=row.id,
        employee_id=row.employee_id,
        year=row.year,
        month=row.month,
        revision=row.revision,
        expected_minutes=row.expected_minutes,
        inputs=row.inputs,
        computed_at=row.computed_at,
        computed_by_employee_id=row.computed_by_employee_id,
    )


class PostgresScheduleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- the employee side, read-only --------------------------------------

    async def employee_status(self, employee_id: UUID) -> str | None:
        return await self._session.scalar(
            select(EmployeeRow.status).where(EmployeeRow.id == employee_id)
        )

    async def assignments_between(
        self, employee_id: UUID, from_date: date, to_date: date
    ) -> list[AssignmentSpan]:
        """Overlapping, not contained: a transfer mid-month needs both spans.

        `end_date IS NULL` is an open assignment, and a NULL that fell out of the
        range comparison would lose the person's current department entirely.
        """
        rows = await self._session.execute(
            text(
                """
                SELECT department_id, start_date, end_date, is_primary
                FROM employee_assignments
                WHERE employee_id = :employee_id
                  AND start_date <= :to_date
                  AND (end_date IS NULL OR end_date >= :from_date)
                ORDER BY start_date, id
                """
            ),
            {"employee_id": employee_id, "from_date": from_date, "to_date": to_date},
        )
        return [
            AssignmentSpan(
                department_id=row[0],
                start_date=row[1],
                end_date=row[2],
                is_primary=row[3],
            )
            for row in rows
        ]

    async def region_of(self, department_ids: list[UUID]) -> dict[UUID, str | None]:
        if not department_ids:
            return {}
        rows = await self._session.execute(
            select(DepartmentRow.id, DepartmentRow.region_code).where(
                DepartmentRow.id.in_(department_ids)
            )
        )
        return {row[0]: row[1] for row in rows}

    async def active_employee_ids(self) -> list[UUID]:
        rows = await self._session.scalars(
            select(EmployeeRow.id)
            .where(EmployeeRow.status != TERMINATED_STATUS)
            .order_by(EmployeeRow.hire_date, EmployeeRow.id)
        )
        return list(rows)

    # --- schedules ---------------------------------------------------------

    async def list_schedules(self, *, include_inactive: bool = False) -> list[WorkSchedule]:
        statement = select(ScheduleRow).order_by(ScheduleRow.code)
        if not include_inactive:
            statement = statement.where(ScheduleRow.is_active)
        rows = list(await self._session.scalars(statement))
        days = await self._days_for([row.id for row in rows])
        return [_to_schedule(row, days.get(row.id, [])) for row in rows]

    async def get_schedule(self, schedule_id: UUID) -> WorkSchedule | None:
        row = await self._session.get(ScheduleRow, schedule_id)
        if row is None:
            return None
        days = await self._days_for([schedule_id])
        return _to_schedule(row, days.get(schedule_id, []))

    async def schedule_for_department(self, department_id: UUID) -> WorkSchedule | None:
        """The department's active schedule. `is_active` is filtered here and only
        here: a schedule somebody's override points at stays in force whatever the
        catalogue says (see the model's comment on `is_active`)."""
        row = await self._session.scalar(
            select(ScheduleRow).where(
                ScheduleRow.department_id == department_id,
                ScheduleRow.is_active,
            )
        )
        if row is None:
            return None
        days = await self._days_for([row.id])
        return _to_schedule(row, days.get(row.id, []))

    async def default_schedule(self) -> WorkSchedule | None:
        row = await self._session.scalar(
            select(ScheduleRow).where(ScheduleRow.is_default, ScheduleRow.is_active)
        )
        if row is None:
            return None
        days = await self._days_for([row.id])
        return _to_schedule(row, days.get(row.id, []))

    async def code_taken(self, code: str, *, excluding: UUID | None = None) -> bool:
        statement = select(func.count()).select_from(ScheduleRow).where(ScheduleRow.code == code)
        if excluding is not None:
            statement = statement.where(ScheduleRow.id != excluding)
        return bool(await self._session.scalar(statement))

    async def scope_taken(
        self,
        *,
        department_id: UUID | None,
        is_default: bool,
        excluding: UUID | None = None,
    ) -> bool:
        if is_default:
            scope = ScheduleRow.is_default
        elif department_id is not None:
            scope = ScheduleRow.department_id == department_id
        else:
            # A pattern nobody has attached to a scope: an override's, and no
            # department's. There is nothing for it to conflict with.
            return False

        statement = (
            select(func.count()).select_from(ScheduleRow).where(scope, ScheduleRow.is_active)
        )
        if excluding is not None:
            statement = statement.where(ScheduleRow.id != excluding)
        return bool(await self._session.scalar(statement))

    async def save_schedule(
        self, data: ScheduleInput, *, schedule_id: UUID | None = None
    ) -> WorkSchedule:
        """Insert or replace one schedule and its whole week.

        `weekly_hours` is computed from the days here rather than accepted from the
        caller: it is the one figure in this table that is a sum of others, and a
        sum written down twice is a sum that disagrees.
        """
        hours = weekly_hours_of(data.days)
        if schedule_id is None:
            row = ScheduleRow(
                id=uuid4(),
                code=data.code,
                name_es=data.name_es,
                name_en=data.name_en,
                department_id=data.department_id,
                weekly_hours=hours,
                is_default=data.is_default,
                is_active=data.is_active,
            )
            self._session.add(row)
            await self._session.flush()
        else:
            row = await self._session.get(ScheduleRow, schedule_id)
            if row is None:  # pragma: no cover - the service reads before it writes
                raise LookupError(f"unknown schedule {schedule_id}")
            row.name_es = data.name_es
            row.name_en = data.name_en
            row.department_id = data.department_id
            row.weekly_hours = hours
            row.is_default = data.is_default
            row.is_active = data.is_active
            await self._session.execute(
                delete(DayRow).where(DayRow.schedule_id == schedule_id)
            )
            await self._session.flush()

        self._session.add_all(
            [
                DayRow(
                    schedule_id=row.id,
                    weekday=day.weekday,
                    expected_minutes=day.expected_minutes,
                    start_time=day.start_time,
                    end_time=day.end_time,
                    break_minutes=day.break_minutes,
                )
                for day in data.days
            ]
        )
        await self._session.flush()
        days = await self._days_for([row.id])
        return _to_schedule(row, days.get(row.id, []))

    # --- overrides ---------------------------------------------------------

    async def overrides_covering(
        self, employee_id: UUID, from_date: date, to_date: date
    ) -> list[ScheduleOverride]:
        rows = await self._session.scalars(
            select(OverrideRow)
            .where(
                OverrideRow.employee_id == employee_id,
                OverrideRow.effective_from <= to_date,
                (OverrideRow.effective_to.is_(None)) | (OverrideRow.effective_to >= from_date),
            )
            .order_by(OverrideRow.effective_from, OverrideRow.id)
        )
        return [_to_override(row) for row in rows]

    async def get_override(self, override_id: UUID) -> ScheduleOverride | None:
        row = await self._session.get(OverrideRow, override_id)
        return _to_override(row) if row is not None else None

    async def overlapping_override(
        self,
        employee_id: UUID,
        effective_from: date,
        effective_to: date | None,
        *,
        excluding: UUID | None = None,
    ) -> ScheduleOverride | None:
        """The overlap test, in SQL, matching the exclusion constraint's rule.

        `effective_to IS NULL` is open-ended, so it overlaps everything from its
        start onwards; otherwise the two windows meet when each starts no later
        than the other ends.
        """
        statement = (
            select(OverrideRow)
            .where(
                OverrideRow.employee_id == employee_id,
                OverrideRow.effective_from <= func.coalesce(effective_to, date.max),
                func.coalesce(OverrideRow.effective_to, date.max) >= effective_from,
            )
            .order_by(OverrideRow.effective_from)
            .limit(1)
        )
        if excluding is not None:
            statement = statement.where(OverrideRow.id != excluding)
        row = await self._session.scalar(statement)
        return _to_override(row) if row is not None else None

    async def save_override(
        self, data: OverrideInput, *, override_id: UUID | None = None
    ) -> ScheduleOverride:
        if override_id is None:
            row = OverrideRow(
                id=uuid4(),
                employee_id=data.employee_id,
                schedule_id=data.schedule_id,
                effective_from=data.effective_from,
                effective_to=data.effective_to,
                reason=data.reason.strip(),
                created_by_employee_id=data.created_by_employee_id,
            )
            self._session.add(row)
        else:
            row = await self._session.get(OverrideRow, override_id)
            if row is None:  # pragma: no cover - the service reads before it writes
                raise LookupError(f"unknown override {override_id}")
            row.employee_id = data.employee_id
            row.schedule_id = data.schedule_id
            row.effective_from = data.effective_from
            row.effective_to = data.effective_to
            row.reason = data.reason.strip()
        await self._session.flush()
        return _to_override(row)

    async def delete_override(self, override_id: UUID) -> None:
        await self._session.execute(delete(OverrideRow).where(OverrideRow.id == override_id))

    # --- holidays ----------------------------------------------------------

    async def holidays_in_year(self, year: int) -> list[Holiday]:
        rows = await self._session.scalars(
            select(HolidayRow).where(HolidayRow.year == year).order_by(HolidayRow.date)
        )
        return [_to_holiday(row) for row in rows]

    async def holiday_stamp(self, year: int) -> str | None:
        """A digest of this year's rows, or None when there are no rows.

        The cache key carries it (`schedule/cache.py`), so it has to move for *any*
        change to the year — including the two the table's own columns do not record:
        a correction made by hand in `psql`, which does not touch `updated_at`, and
        an insert or a delete, which changes which rows exist at all. Digesting the
        rows themselves is what makes all three indistinguishable from the cache's
        point of view, and it costs one aggregate over a table with a few dozen rows
        per year.

        `md5` rather than `hashtext`: a 32-bit hash is enough to make an entry
        unreachable but not enough to be worth the argument, and this is not a
        security boundary. `ORDER BY id` so the digest is stable across two runs
        that read the same rows.
        """
        return await self._session.scalar(
            text(
                """
                SELECT CASE WHEN count(*) = 0 THEN NULL ELSE
                    md5(
                        string_agg(
                            id::text || '|' || date::text || '|' || name_es || '|' ||
                            name_en || '|' || scope || '|' || COALESCE(region_code, '-') ||
                            '|' || year::text,
                            '#' ORDER BY id
                        )
                    )
                END
                FROM holidays WHERE year = :year
                """
            ),
            {"year": year},
        )

    async def get_holiday(self, holiday_id: UUID) -> Holiday | None:
        row = await self._session.get(HolidayRow, holiday_id)
        return _to_holiday(row) if row is not None else None

    async def find_holiday(
        self, on_date: date, scope: str, region_code: str | None
    ) -> Holiday | None:
        """The uniqueness rule as a read: `(date, scope, region_code)`, with NULL
        matching NULL for a national holiday, exactly as the index does."""
        row = await self._session.scalar(
            select(HolidayRow).where(
                HolidayRow.date == on_date,
                HolidayRow.scope == scope,
                HolidayRow.region_code.is_not_distinct_from(region_code),
            )
        )
        return _to_holiday(row) if row is not None else None

    async def save_holiday(
        self, data: HolidayInput, *, holiday_id: UUID | None = None
    ) -> Holiday:
        values = {
            "date": data.date,
            "name_es": data.name_es,
            "name_en": data.name_en,
            "scope": data.scope.value,
            "region_code": data.region_code,
            # Derived, never accepted: the table's check constraint states the same
            # equality, and a caller that could set it could break it.
            "year": data.date.year,
        }
        if holiday_id is None:
            row = HolidayRow(id=uuid4(), **values)
            self._session.add(row)
        else:
            row = await self._session.get(HolidayRow, holiday_id)
            if row is None:  # pragma: no cover - the service reads before it writes
                raise LookupError(f"unknown holiday {holiday_id}")
            for field, value in values.items():
                setattr(row, field, value)
        await self._session.flush()
        return _to_holiday(row)

    async def delete_holiday(self, holiday_id: UUID) -> None:
        await self._session.execute(delete(HolidayRow).where(HolidayRow.id == holiday_id))

    # --- snapshots ---------------------------------------------------------

    async def latest_snapshot(
        self, employee_id: UUID, year: int, month: int
    ) -> ExpectedHoursSnapshot | None:
        row = await self._session.scalar(
            select(SnapshotRow)
            .where(
                SnapshotRow.employee_id == employee_id,
                SnapshotRow.year == year,
                SnapshotRow.month == month,
            )
            .order_by(SnapshotRow.revision.desc())
            .limit(1)
        )
        return _to_snapshot(row) if row is not None else None

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
        """Append the next revision, numbered by the insert itself.

        `UPDATE` and `DELETE` are revoked on this table (migration 0013), so this is
        the only write it accepts — which is what makes "a later schedule edit
        cannot change what March's figure was" a property of PostgreSQL.
        """
        next_revision = (
            select(func.coalesce(func.max(SnapshotRow.revision), 0) + 1)
            .where(
                SnapshotRow.employee_id == employee_id,
                SnapshotRow.year == year,
                SnapshotRow.month == month,
            )
            .scalar_subquery()
        )
        row = (
            await self._session.execute(
                pg_insert(SnapshotRow)
                .values(
                    id=uuid4(),
                    employee_id=employee_id,
                    year=year,
                    month=month,
                    revision=next_revision,
                    expected_minutes=expected_minutes,
                    inputs=inputs,
                    computed_by_employee_id=computed_by_employee_id,
                    computed_at=computed_at,
                )
                .returning(SnapshotRow)
            )
        ).scalars().one()
        await self._session.flush()
        return _to_snapshot(row)

    async def commit(self) -> None:
        await self._session.commit()

    # --- internals ---------------------------------------------------------

    async def _days_for(self, schedule_ids: list[UUID]) -> dict[UUID, list[DayRow]]:
        if not schedule_ids:
            return {}
        rows = await self._session.scalars(
            select(DayRow)
            .where(DayRow.schedule_id.in_(schedule_ids))
            .order_by(DayRow.weekday)
        )
        grouped: dict[UUID, list[DayRow]] = {}
        for row in rows:
            grouped.setdefault(row.schedule_id, []).append(row)
        return grouped


__all__ = ["PostgresScheduleRepository"]
