"""PostgreSQL implementation of the project repository.

Two reads here apply the kernel's `FilterSpec` rather than a query object, and they
are the reason this module exists as more than a table wrapper:

* `selectable_projects` translates every field of the spec into one `WHERE`, and
  translates it *conjunctively*. `allow_all` is read as "no department predicate"
  rather than as "no predicate at all", because the spec's `statuses` clause still
  applies to administration — an archived project is not bookable by the person who
  archived it either.
* `get` and `list` never widen. `list` takes a `ProjectQuery` from a route whose
  guard already asked the kernel, which is why it is the route's query rather than a
  spec: the question it answers is "which projects match these filters", asked by
  somebody who may list projects, and the filters are facets of a catalogue rather
  than a permission.

Nothing commits: the service commits once, so the row and its audit entry land
together.
"""

from collections.abc import Collection
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.access.kernel import FilterSpec
from app.domain.project.models import (
    PATCH_FIELDS,
    UNSET,
    Project,
    ProjectInput,
    ProjectPage,
    ProjectPatch,
    ProjectQuery,
    ProjectStatus,
    ProjectTask,
    ProjectTaskInput,
    ProjectTaskPatch,
)
from app.models.employee import Employee as EmployeeRow
from app.models.org import Department as DepartmentRow
from app.models.project import Project as ProjectRow
from app.models.project import ProjectTask as TaskRow

#: Every field a task patch may carry, for the same reason `PATCH_FIELDS` exists:
#: one list, read by both the loop below and the test that pins it.
TASK_PATCH_FIELDS: tuple[str, ...] = ("code", "name_es", "name_en", "is_billable", "is_active")


def _to_project(row: ProjectRow) -> Project:
    return Project(
        id=row.id,
        code=row.code,
        name_es=row.name_es,
        name_en=row.name_en,
        client_name=row.client_name,
        department_id=row.department_id,
        manager_employee_id=row.manager_employee_id,
        is_billable_default=row.is_billable_default,
        status=ProjectStatus(row.status),
        start_date=row.start_date,
        end_date=row.end_date,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_task(row: TaskRow) -> ProjectTask:
    return ProjectTask(
        id=row.id,
        project_id=row.project_id,
        code=row.code,
        name_es=row.name_es,
        name_en=row.name_en,
        is_billable=row.is_billable,
        is_active=row.is_active,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _apply_spec(statement, spec: FilterSpec):  # noqa: ANN001, ANN201 - SQLAlchemy Select
    """The kernel's description of a reachable project, as a `WHERE` clause.

    Every field of the spec is applied, and both callers that read "what may this
    person record against" go through here. `statuses` is a membership test rather
    than a lower bound, so a status added to the project module later is out of reach
    until somebody names it here — the direction that fails closed.
    """
    if spec.statuses:
        statement = statement.where(ProjectRow.status.in_(sorted(spec.statuses)))
    if not spec.allow_all:
        # The two ways in, as one predicate. Writing only the department clause would
        # take a project away from the manager who runs it the moment it moved to a
        # department they do not work in.
        reachable: list = [ProjectRow.department_id.in_(sorted(spec.department_ids))]
        if spec.manager_employee_id is not None:
            reachable.append(ProjectRow.manager_employee_id == spec.manager_employee_id)
        statement = statement.where(or_(*reachable))
    return statement


class PostgresProjectRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- projects ----------------------------------------------------------

    async def get(self, project_id: UUID) -> Project | None:
        row = await self._session.scalar(select(ProjectRow).where(ProjectRow.id == project_id))
        return _to_project(row) if row is not None else None

    async def find_by_code(self, code: str) -> Project | None:
        row = await self._session.scalar(select(ProjectRow).where(ProjectRow.code == code))
        return _to_project(row) if row is not None else None

    async def projects_by_ids(self, project_ids: Collection[UUID]) -> dict[UUID, Project]:
        """Several projects in one statement, keyed by id.

        The other half of ticket 28's grid read: a week's entries name projects as
        well as tasks, and resolving them one at a time is a query per cell.
        """
        if not project_ids:
            return {}
        rows = await self._session.scalars(
            select(ProjectRow).where(ProjectRow.id.in_(list(project_ids)))
        )
        return {row.id: _to_project(row) for row in rows}

    async def list_projects(self, query: ProjectQuery) -> ProjectPage:
        statement = select(ProjectRow)
        if query.department_id is not None:
            statement = statement.where(ProjectRow.department_id == query.department_id)
        if query.status is not None:
            statement = statement.where(ProjectRow.status == query.status.value)
        if query.client_name is not None:
            # `ilike` with the wildcards added here rather than by the caller: a
            # filter that took a pattern would let a client's `%` turn into a scan
            # of the whole table, and this is a filter box in a list view.
            statement = statement.where(ProjectRow.client_name.ilike(f"%{query.client_name}%"))

        total = await self._session.scalar(
            select(func.count()).select_from(statement.subquery())
        )
        rows = await self._session.scalars(
            statement.order_by(ProjectRow.code).limit(query.limit).offset(query.offset)
        )
        return ProjectPage(
            items=[_to_project(row) for row in rows],
            total=int(total or 0),
            limit=query.limit,
            offset=query.offset,
        )

    async def save(self, data: ProjectInput, *, created_by_employee_id: UUID) -> Project:
        row = ProjectRow(
            id=uuid4(),
            code=data.code,
            name_es=data.name_es,
            name_en=data.name_en,
            client_name=data.client_name,
            department_id=data.department_id,
            manager_employee_id=data.manager_employee_id,
            is_billable_default=data.is_billable_default,
            status=data.status.value,
            start_date=data.start_date,
            end_date=data.end_date,
            created_by_employee_id=created_by_employee_id,
        )
        self._session.add(row)
        await self._session.flush()
        return _to_project(row)

    async def update(self, project_id: UUID, patch: ProjectPatch) -> Project:
        row = await self._require_row(project_id)
        for field in PATCH_FIELDS:
            value = getattr(patch, field)
            if value is UNSET:
                continue
            if field == "status":
                value = value.value
            setattr(row, field, value)
        # Refreshed rather than merely flushed: `updated_at` is a SQL expression
        # (`onupdate=func.now()`), so the flush leaves it expired, and reading it back
        # afterwards is a lazy load — which an async session cannot perform outside a
        # greenlet. `await refresh` is the read, done where the caller can await it.
        await self._session.flush()
        await self._session.refresh(row)
        return _to_project(row)

    async def reassign_manager(self, project_id: UUID, manager_employee_id: UUID) -> Project:
        row = await self._require_row(project_id)
        row.manager_employee_id = manager_employee_id
        await self._session.flush()
        await self._session.refresh(row)
        return _to_project(row)

    async def list_selectable(
        self, spec: FilterSpec, query: ProjectQuery
    ) -> ProjectPage:
        """The projects in reach, narrowed by the list endpoint's own facets.

        The facets and the permission are composed in one statement, and the
        permission is applied by `_apply_spec` rather than by the caller: a route that
        built its own `WHERE` and forgot the clause would be a filter-free query with
        a filter-shaped signature.
        """
        statement = _apply_spec(select(ProjectRow), spec)
        if query.department_id is not None:
            statement = statement.where(ProjectRow.department_id == query.department_id)
        if query.status is not None:
            statement = statement.where(ProjectRow.status == query.status.value)
        if query.client_name is not None:
            statement = statement.where(
                ProjectRow.client_name.ilike(f"%{query.client_name}%")
            )

        total = await self._session.scalar(select(func.count()).select_from(statement.subquery()))
        rows = await self._session.scalars(
            statement.order_by(ProjectRow.code).limit(query.limit).offset(query.offset)
        )
        return ProjectPage(
            items=[_to_project(row) for row in rows],
            total=int(total or 0),
            limit=query.limit,
            offset=query.offset,
        )

    # --- tasks -------------------------------------------------------------

    async def get_task(self, task_id: UUID) -> ProjectTask | None:
        row = await self._session.scalar(select(TaskRow).where(TaskRow.id == task_id))
        return _to_task(row) if row is not None else None

    async def tasks_by_ids(self, task_ids: Collection[UUID]) -> dict[UUID, ProjectTask]:
        """Several tasks in one statement, keyed by id.

        Ticket 28's grid read: a week names a task per entry, and a lookup per cell is
        how a seven-day screen becomes twenty round trips. An unknown id is simply
        absent — nothing here decides whether that is an error.
        """
        if not task_ids:
            return {}
        rows = await self._session.scalars(
            select(TaskRow).where(TaskRow.id.in_(list(task_ids)))
        )
        return {row.id: _to_task(row) for row in rows}

    async def find_task_by_code(self, project_id: UUID, code: str) -> ProjectTask | None:
        row = await self._session.scalar(
            select(TaskRow).where(TaskRow.project_id == project_id, TaskRow.code == code)
        )
        return _to_task(row) if row is not None else None

    async def list_tasks(
        self, project_id: UUID, *, include_inactive: bool = True
    ) -> list[ProjectTask]:
        statement = select(TaskRow).where(TaskRow.project_id == project_id)
        if not include_inactive:
            statement = statement.where(TaskRow.is_active.is_(True))
        rows = await self._session.scalars(statement.order_by(TaskRow.code))
        return [_to_task(row) for row in rows]

    async def save_task(self, project_id: UUID, data: ProjectTaskInput) -> ProjectTask:
        row = TaskRow(
            id=uuid4(),
            project_id=project_id,
            code=data.code,
            name_es=data.name_es,
            name_en=data.name_en,
            is_billable=data.is_billable,
            is_active=data.is_active,
        )
        self._session.add(row)
        await self._session.flush()
        return _to_task(row)

    async def update_task(self, task_id: UUID, patch: ProjectTaskPatch) -> ProjectTask:
        row = await self._require_task_row(task_id)
        for field in TASK_PATCH_FIELDS:
            value = getattr(patch, field)
            # `is_billable=False` is a value and `UNSET` is an absence: the sentinel
            # is what keeps a task from being overridden to inherit by a request that
            # never mentioned the field.
            if value is UNSET:
                continue
            setattr(row, field, value)
        await self._session.flush()
        await self._session.refresh(row)
        return _to_task(row)

    # --- lookups the service validates against ------------------------------

    async def department_exists(self, department_id: UUID) -> bool:
        return bool(
            await self._session.scalar(
                select(func.count())
                .select_from(DepartmentRow)
                .where(DepartmentRow.id == department_id, DepartmentRow.is_active.is_(True))
            )
        )

    async def employee_status(self, employee_id: UUID) -> str | None:
        return await self._session.scalar(
            select(EmployeeRow.status).where(EmployeeRow.id == employee_id)
        )

    async def commit(self) -> None:
        await self._session.commit()

    # --- internals ---------------------------------------------------------

    async def _require_row(self, project_id: UUID) -> ProjectRow:
        row = await self._session.scalar(select(ProjectRow).where(ProjectRow.id == project_id))
        if row is None:  # pragma: no cover - the service reads the row first
            raise LookupError(f"project {project_id} disappeared between two reads")
        return row

    async def _require_task_row(self, task_id: UUID) -> TaskRow:
        row = await self._session.scalar(select(TaskRow).where(TaskRow.id == task_id))
        if row is None:  # pragma: no cover - the service reads the row first
            raise LookupError(f"task {task_id} disappeared between two reads")
        return row


__all__ = ["PostgresProjectRepository"]
