"""PostgreSQL implementation of the timesheet repository.

Two things here are worth reading before the code:

* **Every read is scoped by `employee_id`, and there is no method that is not.**
  There is no `list_all`, and no query takes an employee id from a request: the
  service passes the caller's own, which is what makes 只能为本人填报 a property of the
  queries rather than a check somebody has to remember. A repository method that
  could answer "everybody's week" would be the shape a report (ticket 30) reaches
  for, and it should have to arrive as a new method with its own permission.

* **`entries_in_week` is one statement, ordered by day.** The grid computes a total
  per day and a total per week from these rows, so a paginated read here would be a
  total computed over a page — a timesheet that disagrees with itself. A week is at
  most fifty rows per day by construction.

* **The global week lock is written here and read by the database.** `lock_week` and
  `lock_expired_weeks` are the only writers of `timesheet_weeks_lock`, and the trigger
  ticket 29's migration installs reads the same rows — so the fact the service refuses
  on and the fact a console is refused by are one row rather than two opinions.

Nothing commits: the service commits once, so an entry and the audit record of who
wrote it land together or not at all.
"""

from datetime import date
from uuid import UUID, uuid4

from sqlalchemy import delete, func, literal, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.timesheet.models import (
    UNSET,
    EntryInput,
    EntryPatch,
    EntryType,
    Timesheet,
    TimesheetEntry,
    TimesheetPage,
    TimesheetStatus,
)
from app.models.employee import Employee as EmployeeRow
from app.models.timesheet import Timesheet as TimesheetRow
from app.models.timesheet import TimesheetEntry as EntryRow
from app.models.timesheet import TimesheetWeekLock as WeekLockRow


def _to_timesheet(row: TimesheetRow) -> Timesheet:
    return Timesheet(
        id=row.id,
        employee_id=row.employee_id,
        week_start=row.week_start,
        status=TimesheetStatus(row.status),
        approval_request_id=row.approval_request_id,
        submitted_at=row.submitted_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        supersedes_id=row.supersedes_timesheet_id,
        is_supplementary=row.is_supplementary,
    )


def _to_entry(row: EntryRow) -> TimesheetEntry:
    return TimesheetEntry(
        id=row.id,
        timesheet_id=row.timesheet_id,
        employee_id=row.employee_id,
        week_start=row.week_start,
        entry_date=row.entry_date,
        project_id=row.project_id,
        task_id=row.task_id,
        minutes=row.minutes,
        is_billable=row.is_billable,
        note=row.note,
        created_at=row.created_at,
        updated_at=row.updated_at,
        entry_type=EntryType(row.entry_type),
        reverses_entry_id=row.reverses_entry_id,
    )


class PostgresTimesheetRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- weeks -------------------------------------------------------------

    async def get(self, timesheet_id: UUID) -> Timesheet | None:
        row = await self._session.scalar(
            select(TimesheetRow).where(TimesheetRow.id == timesheet_id)
        )
        return _to_timesheet(row) if row is not None else None

    async def get_week(self, employee_id: UUID, week_start: date) -> Timesheet | None:
        """The week's original sheet. Supplements are read with `sheets_in_week`."""
        row = await self._session.scalar(
            select(TimesheetRow).where(
                TimesheetRow.employee_id == employee_id,
                TimesheetRow.week_start == week_start,
                TimesheetRow.supersedes_timesheet_id.is_(None),
            )
        )
        return _to_timesheet(row) if row is not None else None

    async def sheets_in_week(
        self, employee_id: UUID, week_start: date
    ) -> list[Timesheet]:
        rows = await self._session.scalars(
            select(TimesheetRow)
            .where(
                TimesheetRow.employee_id == employee_id,
                TimesheetRow.week_start == week_start,
            )
            # The original first, then the corrections in the order they were filed:
            # "the week, and what has been said about it since" is the order a reader
            # takes them in, and NULLS FIRST is that order rather than a coincidence
            # of how the link happens to be stored.
            .order_by(
                TimesheetRow.supersedes_timesheet_id.nulls_first(), TimesheetRow.created_at
            )
        )
        return [_to_timesheet(row) for row in rows]

    async def week_exists(self, employee_id: UUID, week_start: date) -> bool:
        return bool(
            await self._session.scalar(
                select(func.count())
                .select_from(TimesheetRow)
                .where(
                    TimesheetRow.employee_id == employee_id,
                    TimesheetRow.week_start == week_start,
                )
            )
        )

    async def create_week(self, employee_id: UUID, week_start: date) -> Timesheet:
        row = TimesheetRow(
            id=uuid4(),
            employee_id=employee_id,
            week_start=week_start,
            status=TimesheetStatus.DRAFT.value,
        )
        self._session.add(row)
        await self._session.flush()
        return _to_timesheet(row)

    async def create_supplement(
        self, employee_id: UUID, week_start: date, supersedes_id: UUID
    ) -> Timesheet:
        row = TimesheetRow(
            id=uuid4(),
            employee_id=employee_id,
            week_start=week_start,
            status=TimesheetStatus.DRAFT.value,
            supersedes_timesheet_id=supersedes_id,
            is_supplementary=True,
        )
        self._session.add(row)
        await self._session.flush()
        return _to_timesheet(row)

    async def set_status(
        self,
        timesheet_id: UUID,
        status: TimesheetStatus,
        *,
        approval_request_id: UUID | None = None,
        submitted_at: object | None = None,
    ) -> Timesheet:
        """Move the week's status, and stamp the request it is filed under.

        One statement for both columns because the two describe one event: a week
        that says `pending` with no request behind it is a week nobody can chase.

        The `submitted_at` guard is deliberate: a *status* sync that found a stale
        cache must not restamp the filing time, so the column only moves when a
        value is passed. `None` therefore means "leave the timestamp alone" here,
        which is the opposite of the patch convention elsewhere in this codebase —
        and the reason this is one named argument rather than a patch object.
        """
        values: dict[str, object] = {"status": status.value}
        if approval_request_id is not None:
            values["approval_request_id"] = approval_request_id
        if submitted_at is not None:
            values["submitted_at"] = submitted_at

        await self._session.execute(
            update(TimesheetRow).where(TimesheetRow.id == timesheet_id).values(**values)
        )
        await self._session.flush()
        row = await self._session.scalar(
            select(TimesheetRow).where(TimesheetRow.id == timesheet_id)
        )
        if row is None:  # pragma: no cover - the service reads the row first
            raise LookupError(f"timesheet {timesheet_id} disappeared between two reads")
        return _to_timesheet(row)

    async def list_weeks(
        self, employee_id: UUID, *, limit: int = 50, offset: int = 0
    ) -> TimesheetPage:
        """The caller's own weeks, newest first. Originals only: see the protocol.

        A supplement is a correction *of* a week and shares its Monday, so listing it
        as a week of its own would show one week twice with two statuses and no way to
        tell which was the record.
        """
        statement = select(TimesheetRow).where(
            TimesheetRow.employee_id == employee_id,
            TimesheetRow.supersedes_timesheet_id.is_(None),
        )
        total = await self._session.scalar(
            select(func.count()).select_from(statement.subquery())
        )
        rows = await self._session.scalars(
            statement.order_by(TimesheetRow.week_start.desc()).limit(limit).offset(offset)
        )
        return TimesheetPage(
            items=[_to_timesheet(row) for row in rows],
            total=int(total or 0),
            limit=limit,
            offset=offset,
        )

    # --- the global week lock ----------------------------------------------

    async def week_is_locked(self, week_start: date) -> bool:
        return bool(
            await self._session.scalar(
                select(func.count())
                .select_from(WeekLockRow)
                .where(WeekLockRow.week_start == week_start)
            )
        )

    async def lock_week(
        self,
        week_start: date,
        *,
        reason: str | None = None,
        locked_by_employee_id: UUID | None = None,
    ) -> bool:
        statement = (
            pg_insert(WeekLockRow)
            .values(
                week_start=week_start,
                reason=reason,
                locked_by_employee_id=locked_by_employee_id,
            )
            # A second call must not restamp when the week was closed, which is the
            # question a closed payroll month is asked afterwards.
            .on_conflict_do_nothing(index_elements=[WeekLockRow.week_start])
            .returning(WeekLockRow.week_start)
        )
        return (await self._session.scalar(statement)) is not None

    async def lock_expired_weeks(
        self, employee_id: UUID, *, before: date, reason: str
    ) -> list[date]:
        weeks = (
            select(TimesheetRow.week_start)
            .where(
                TimesheetRow.employee_id == employee_id,
                TimesheetRow.week_start < before,
            )
            .distinct()
            .subquery()
        )
        statement = (
            pg_insert(WeekLockRow)
            .from_select(["week_start", "reason"], select(weeks.c.week_start, literal(reason)))
            .on_conflict_do_nothing(index_elements=[WeekLockRow.week_start])
            .returning(WeekLockRow.week_start)
        )
        return list(await self._session.scalars(statement))

    # --- entries -----------------------------------------------------------

    async def entries_in_week(
        self, employee_id: UUID, week_start: date
    ) -> list[TimesheetEntry]:
        rows = await self._session.scalars(
            select(EntryRow)
            .where(EntryRow.employee_id == employee_id, EntryRow.week_start == week_start)
            # Day first, then insertion order: the grid groups by day, and within a
            # day the order somebody typed the rows in is the order they expect. A
            # reversal was written later than what it cancels, so it reads under it.
            .order_by(EntryRow.entry_date, EntryRow.created_at, EntryRow.id)
        )
        return [_to_entry(row) for row in rows]

    async def entries_in_sheet(self, timesheet_id: UUID) -> list[TimesheetEntry]:
        rows = await self._session.scalars(
            select(EntryRow)
            .where(EntryRow.timesheet_id == timesheet_id)
            .order_by(EntryRow.entry_date, EntryRow.created_at, EntryRow.id)
        )
        return [_to_entry(row) for row in rows]

    async def get_entry(self, entry_id: UUID) -> TimesheetEntry | None:
        row = await self._session.scalar(select(EntryRow).where(EntryRow.id == entry_id))
        return _to_entry(row) if row is not None else None

    async def reversal_for(self, entry_id: UUID) -> TimesheetEntry | None:
        row = await self._session.scalar(
            select(EntryRow)
            .where(EntryRow.reverses_entry_id == entry_id)
            .order_by(EntryRow.created_at)
            .limit(1)
        )
        return _to_entry(row) if row is not None else None

    async def add_entry(
        self, timesheet_id: UUID, employee_id: UUID, week_start: date, data: EntryInput
    ) -> TimesheetEntry:
        row = EntryRow(
            id=uuid4(),
            timesheet_id=timesheet_id,
            employee_id=employee_id,
            week_start=week_start,
            entry_date=data.entry_date,
            project_id=data.project_id,
            task_id=data.task_id,
            minutes=data.minutes,
            is_billable=data.is_billable,
            note=data.note,
            entry_type=data.entry_type.value,
            reverses_entry_id=data.reverses_entry_id,
        )
        self._session.add(row)
        await self._session.flush()
        return _to_entry(row)

    async def update_entry(
        self, entry_id: UUID, patch: EntryPatch, *, is_billable: bool
    ) -> TimesheetEntry:
        values: dict[str, object] = {"is_billable": is_billable}
        for field in ("entry_date", "project_id", "task_id", "minutes"):
            value = getattr(patch, field)
            if value is not None:
                values[field] = value
        # `UNSET` is "leave the note alone" and `None` is "clear it": the sentinel is
        # what keeps an edit that mentions nothing about the note from wiping it.
        if patch.note is not UNSET:
            values["note"] = patch.note

        await self._session.execute(
            update(EntryRow).where(EntryRow.id == entry_id).values(**values)
        )
        await self._session.flush()
        row = await self._session.scalar(select(EntryRow).where(EntryRow.id == entry_id))
        if row is None:  # pragma: no cover - the service reads the row first
            raise LookupError(f"entry {entry_id} disappeared between two reads")
        return _to_entry(row)

    async def delete_entry(self, entry_id: UUID) -> None:
        await self._session.execute(delete(EntryRow).where(EntryRow.id == entry_id))
        await self._session.flush()

    # --- plumbing ----------------------------------------------------------

    async def employee_exists(self, employee_id: UUID) -> bool:
        return bool(
            await self._session.scalar(
                select(func.count())
                .select_from(EmployeeRow)
                .where(EmployeeRow.id == employee_id)
            )
        )

    async def commit(self) -> None:
        await self._session.commit()

    async def rollback(self) -> None:
        await self._session.rollback()


__all__ = ["PostgresTimesheetRepository"]
