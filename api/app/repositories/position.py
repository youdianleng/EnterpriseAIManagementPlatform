"""PostgreSQL implementation of the position catalogue repository.

Reads join the department so a caller can render a position without a second
lookup, and the assignment count comes from the same query rather than an N+1
loop over the list.
"""

from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.position.models import Position, PositionInput, PositionPatch
from app.models.employee import EmployeeAssignment as AssignmentRow
from app.models.employee import JobPosition as PositionRow
from app.models.org import Department as DepartmentRow


def _to_domain(
    row: PositionRow,
    department: DepartmentRow,
    active_count: int = 0,
    total_count: int = 0,
) -> Position:
    return Position(
        id=row.id,
        code=row.code,
        title_es=row.title_es,
        title_en=row.title_en,
        department_id=row.department_id,
        department_code=department.code,
        department_name_es=department.name_es,
        department_name_en=department.name_en,
        is_managerial=row.is_managerial,
        is_active=row.is_active,
        active_assignment_count=active_count,
        total_assignment_count=total_count,
    )


class PostgresPositionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _base_statement(self):
        """Positions joined to their department and both assignment counts.

        Counted in the same query rather than a loop per position: a catalogue of
        fifty roles should not issue fifty-one statements.
        """
        active = (
            select(
                AssignmentRow.job_position_id.label("position_id"),
                func.count().label("active_count"),
            )
            .where(AssignmentRow.end_date.is_(None))
            .group_by(AssignmentRow.job_position_id)
            .subquery()
        )
        total = (
            select(
                AssignmentRow.job_position_id.label("position_id"),
                func.count().label("total_count"),
            )
            .group_by(AssignmentRow.job_position_id)
            .subquery()
        )
        return (
            select(
                PositionRow,
                DepartmentRow,
                func.coalesce(active.c.active_count, 0),
                func.coalesce(total.c.total_count, 0),
            )
            .join(DepartmentRow, DepartmentRow.id == PositionRow.department_id)
            .outerjoin(active, active.c.position_id == PositionRow.id)
            .outerjoin(total, total.c.position_id == PositionRow.id)
        )

    async def get(self, position_id: UUID) -> Position | None:
        row = (
            await self._session.execute(
                self._base_statement().where(PositionRow.id == position_id)
            )
        ).first()
        return _to_domain(*row) if row else None

    async def list_positions(
        self, *, department_id: UUID | None = None, include_inactive: bool = True
    ) -> list[Position]:
        statement = self._base_statement()
        if department_id is not None:
            statement = statement.where(PositionRow.department_id == department_id)
        if not include_inactive:
            statement = statement.where(PositionRow.is_active.is_(True))
        rows = (
            await self._session.execute(
                statement.order_by(DepartmentRow.code, PositionRow.code)
            )
        ).all()
        return [_to_domain(*row) for row in rows]

    async def code_exists(
        self, *, department_id: UUID, code: str, exclude_id: UUID | None = None
    ) -> bool:
        statement = select(func.count()).select_from(PositionRow).where(
            PositionRow.department_id == department_id, PositionRow.code == code
        )
        if exclude_id is not None:
            statement = statement.where(PositionRow.id != exclude_id)
        return bool(await self._session.scalar(statement))

    async def count_assignments(self, position_id: UUID, *, active_only: bool) -> int:
        statement = (
            select(func.count())
            .select_from(AssignmentRow)
            .where(AssignmentRow.job_position_id == position_id)
        )
        if active_only:
            statement = statement.where(AssignmentRow.end_date.is_(None))
        return int(await self._session.scalar(statement) or 0)

    async def save(self, data: PositionInput) -> Position:
        row = PositionRow(
            code=data.code,
            title_es=data.title_es,
            title_en=data.title_en,
            department_id=data.department_id,
            is_managerial=data.is_managerial,
            is_active=True,
        )
        self._session.add(row)
        await self._session.flush()
        department = await self._session.get(DepartmentRow, data.department_id)
        assert department is not None  # the service checked before calling
        return _to_domain(row, department)

    async def update(self, position_id: UUID, patch: PositionPatch) -> Position:
        changes = patch.changes()
        if changes:
            await self._session.execute(
                update(PositionRow).where(PositionRow.id == position_id).values(**changes)
            )
            await self._session.flush()
        position = await self.get(position_id)
        assert position is not None
        return position

    async def delete(self, position_id: UUID) -> None:
        await self._session.execute(delete(PositionRow).where(PositionRow.id == position_id))
        await self._session.flush()

    async def commit(self) -> None:
        await self._session.commit()


__all__ = ["PostgresPositionRepository"]
