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

from sqlalchemy import (
    ColumnElement,
    Select,
    case,
    delete,
    false,
    func,
    literal,
    or_,
    select,
    true,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.access.kernel import FilterSpec
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
from app.domain.timesheet.report import (
    DimensionValue,
    ReportDimension,
    ReportFilter,
    ReportRow,
    ReportTotals,
)
from app.models.employee import Employee as EmployeeRow
from app.models.org import Department as DepartmentRow
from app.models.project import Project as ProjectRow
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

    # --- the report (ticket 30) --------------------------------------------

    async def report_rows(
        self, spec: FilterSpec, report_filter: ReportFilter
    ) -> list[ReportRow]:
        """One aggregate per group over the approved rows this caller may read.

        The kernel's `FilterSpec` is *translated* here rather than re-derived: this
        function decides no rule, it writes the one the kernel stated as SQL, and a
        spec that names nobody matches `false()` — the direction a mistake has to
        fall in. The predicate itself comes from `_report_where`, which
        `report_totals` shares, so the table and its totals row cannot describe
        different rows.

        Every key column is labelled `k<N>` because two dimensions can both name a
        `code`: positional labels keep the row readable and stop Postgres being asked
        whether one `name_es` is the project's or the department's.
        """
        keys = [
            column.label(f"k{index}")
            for index, column in enumerate(_report_keys(report_filter.group_by))
        ]
        statement = _report_from([*keys, *_report_aggregates()], report_filter.group_by)
        statement = (
            statement.where(*_report_where(spec, report_filter))
            # The grouping is stated as the columns it selected, so Postgres is never
            # asked whether a name is functionally dependent on the id beside it.
            .group_by(*keys)
            .order_by(*keys)
        )
        rows = (await self._session.execute(statement)).all()
        return [
            ReportRow(
                dimensions=_dimensions(row, report_filter.group_by),
                totals=_totals(row, len(keys)),
            )
            for row in rows
        ]

    async def report_totals(
        self, spec: FilterSpec, report_filter: ReportFilter
    ) -> ReportTotals:
        """The same figures over the whole period, in one row.

        Its own statement rather than the rows added up, because two of the six
        numbers are `count(distinct)` and a sum of per-group counts is not the count
        over the period: a week that booked time on two projects appears in two rows.
        The predicate is the same object the grouped statement used, which is what
        makes the totals line agree with the table above it by construction.
        """
        statement = _report_from(_report_aggregates(), ())
        row = (
            await self._session.execute(
                statement.where(*_report_where(spec, report_filter))
            )
        ).one()
        return _totals(row, 0)

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


#: The columns each dimension's key is made of, in the order its value reads them.
#: A dict rather than a chain of `if`s so that "which columns does a department row
#: carry" has one answer, and a dimension added to the module is a `KeyError` here
#: rather than a silently ungrouped report.
_KEY_COLUMNS: dict[ReportDimension, tuple[ColumnElement, ...]] = {
    ReportDimension.PROJECT: (
        EntryRow.project_id,
        ProjectRow.code,
        ProjectRow.name_es,
        ProjectRow.name_en,
    ),
    ReportDimension.DEPARTMENT: (
        ProjectRow.department_id,
        DepartmentRow.code,
        DepartmentRow.name_es,
        DepartmentRow.name_en,
    ),
    ReportDimension.EMPLOYEE: (
        EntryRow.employee_id,
        EmployeeRow.first_name,
        EmployeeRow.last_name,
    ),
    ReportDimension.PERIOD: (EntryRow.week_start,),
}


def _report_keys(group_by: tuple[ReportDimension, ...]) -> list[ColumnElement]:
    """The grouped columns, in the caller's order."""
    return [column for dimension in group_by for column in _KEY_COLUMNS[dimension]]


def _report_from(
    columns: list[ColumnElement], group_by: tuple[ReportDimension, ...]
) -> Select:
    """The report's FROM clause and whatever joins the grouping needs.

    The sheet join is what carries the approval — a corrected week has an approved
    original *and* a supplement of its own, and a correction counts only once that
    second document is decided. The project join carries the reach (its manager) and
    the department. The last two are read for their *names* and are joined only when
    a dimension is grouped by them, which keeps the totals statement to two joins.

    The department is the **project's**: where the work belongs, and a fact of the row.
    The department a person was in on the day is a time-dependent fact about an
    assignment, and reading it here would silently pick one date for it.
    """
    statement = (
        select(*columns)
        .select_from(EntryRow)
        .join(TimesheetRow, TimesheetRow.id == EntryRow.timesheet_id)
        .join(ProjectRow, ProjectRow.id == EntryRow.project_id)
    )
    if ReportDimension.DEPARTMENT in group_by:
        statement = statement.join(DepartmentRow, DepartmentRow.id == ProjectRow.department_id)
    if ReportDimension.EMPLOYEE in group_by:
        statement = statement.join(EmployeeRow, EmployeeRow.id == EntryRow.employee_id)
    return statement


def _report_where(
    spec: FilterSpec, report_filter: ReportFilter
) -> list[ColumnElement]:
    """The predicates both report statements share, so the two cannot disagree.

    Returned as a list rather than composed into one statement, because the totals row
    is a second query and the only way it describes a different set of rows is if the
    filter were written twice. It is not.
    """
    clauses: list[ColumnElement] = [
        # Approved *sheets*, not approved weeks: a draft or a week awaiting a decision
        # is absent from the result rather than present with zeros.
        TimesheetRow.status == TimesheetStatus.APPROVED.value,
        EntryRow.entry_date >= report_filter.from_date,
        EntryRow.entry_date <= report_filter.to_date,
        _reach(spec),
    ]
    if report_filter.project_ids:
        clauses.append(EntryRow.project_id.in_(report_filter.project_ids))
    if report_filter.employee_ids:
        clauses.append(EntryRow.employee_id.in_(report_filter.employee_ids))
    if report_filter.department_ids:
        clauses.append(ProjectRow.department_id.in_(report_filter.department_ids))
    return clauses


def _report_aggregates() -> list[ColumnElement]:
    """The five figures and the week count, in one statement.

    The billable split is a conditional sum over the stored flag rather than two
    passes over the rows: one query, so the two columns cannot be read from two
    different states of the table. `gross` and `reversed` are the pair the grid's day
    totals carry, and they are what makes the net explicable rather than merely
    correct.

    Every sum is coalesced, because `sum()` over *no rows* is NULL and the totals
    statement is exactly that when nothing matches: a report with no approved hours is
    a table of zeros, not a table of nulls, and `billable + non_billable` has to be
    arithmetic rather than a type error.
    """
    return [
        func.coalesce(
            func.sum(case((EntryRow.is_billable.is_(True), EntryRow.minutes), else_=0)), 0
        ).label("billable_minutes"),
        func.coalesce(
            func.sum(case((EntryRow.is_billable.is_(False), EntryRow.minutes), else_=0)), 0
        ).label("non_billable_minutes"),
        func.coalesce(
            func.sum(case((EntryRow.minutes > 0, EntryRow.minutes), else_=0)), 0
        ).label("gross_minutes"),
        func.coalesce(
            func.sum(case((EntryRow.minutes < 0, -EntryRow.minutes), else_=0)), 0
        ).label("reversal_minutes"),
        func.count().label("entries"),
        func.count(func.distinct(EntryRow.week_start)).label("weeks"),
    ]


def _reach(spec: FilterSpec) -> ColumnElement:
    """The kernel's `FilterSpec`, as a predicate over an entry.

    The union of two reaches and a company-wide remit, and nothing else: a caller who
    is neither HR, nor the person's manager, nor the manager of the project gets
    `false()` — a report that names nobody states no rows, which is the direction this
    has to fail in. The department is deliberately not a clause: a manager and a
    colleague share one, and reading it here is the escalation `_can_on_timesheet_line`
    refuses.
    """
    if spec.allow_all:
        return true()
    clauses: list[ColumnElement] = []
    if spec.reports_employee_ids:
        clauses.append(EntryRow.employee_id.in_(spec.reports_employee_ids))
    if spec.manager_employee_id is not None:
        clauses.append(ProjectRow.manager_employee_id == spec.manager_employee_id)
    return or_(*clauses) if clauses else false()


def _dimensions(
    row, group_by: tuple[ReportDimension, ...]  # noqa: ANN001 - a SQLAlchemy Row
) -> tuple[DimensionValue, ...]:
    """One row's key, read positionally against the columns each dimension selected."""
    values: list[DimensionValue] = []
    offset = 0
    for dimension in group_by:
        cells = row[offset : offset + len(_KEY_COLUMNS[dimension])]
        offset += len(_KEY_COLUMNS[dimension])
        if dimension is ReportDimension.PERIOD:
            values.append(DimensionValue(kind=dimension, week_start=cells[0]))
        elif dimension is ReportDimension.EMPLOYEE:
            # The two name halves become the one label a report shows, in the order
            # the employee module writes a name: given name first, then the surname.
            values.append(
                DimensionValue(
                    kind=dimension,
                    id=cells[0],
                    name_es=f"{cells[1]} {cells[2]}",
                    name_en=f"{cells[1]} {cells[2]}",
                )
            )
        else:
            values.append(
                DimensionValue(
                    kind=dimension,
                    id=cells[0],
                    code=cells[1],
                    name_es=cells[2],
                    name_en=cells[3],
                )
            )
    return tuple(values)


def _totals(row, offset: int) -> ReportTotals:  # noqa: ANN001 - a SQLAlchemy Row
    """The figures, read at the position the grouped keys end."""
    return ReportTotals(
        billable_minutes=row[offset],
        non_billable_minutes=row[offset + 1],
        gross_minutes=row[offset + 2],
        reversal_minutes=row[offset + 3],
        entries=row[offset + 4],
        weeks=row[offset + 5],
    )


__all__ = ["PostgresTimesheetRepository"]
