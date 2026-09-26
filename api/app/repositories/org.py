"""PostgreSQL implementation of the department repository.

Every query that asks "under which department" uses ltree's `<@` operator so the
answer comes from an index rather than a recursive walk. The ancestry operand is
passed as text and cast in SQL, because PostgreSQL does not implicitly coerce a
bind parameter to ltree.
"""

from uuid import UUID

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.org.models import (
    ClearanceLevel,
    Department,
    DepartmentInput,
    DepartmentPatch,
)
from app.models.org import Department as DepartmentRow

# Casting a bind parameter to ltree requires naming the type explicitly.
LTREE_CAST = text("CAST(:path AS ltree)")


def _to_domain(row: DepartmentRow) -> Department:
    return Department(
        id=row.id,
        code=row.code,
        name_es=row.name_es,
        name_en=row.name_en,
        parent_id=row.parent_id,
        path=row.path,
        depth=row.depth,
        clearance_level=ClearanceLevel(row.clearance_level),
        cost_center=row.cost_center,
        manager_employee_id=row.manager_employee_id,
        description_es=row.description_es,
        description_en=row.description_en,
        is_active=row.is_active,
    )


class PostgresDepartmentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, department_id: UUID) -> Department | None:
        row = await self._session.get(DepartmentRow, department_id)
        return _to_domain(row) if row else None

    async def get_by_code(self, code: str) -> Department | None:
        row = await self._session.scalar(select(DepartmentRow).where(DepartmentRow.code == code))
        return _to_domain(row) if row else None

    async def list_all(self, *, include_inactive: bool = True) -> list[Department]:
        statement = select(DepartmentRow).order_by(DepartmentRow.path)
        if not include_inactive:
            statement = statement.where(DepartmentRow.is_active.is_(True))
        rows = (await self._session.scalars(statement)).all()
        return [_to_domain(row) for row in rows]

    async def list_subtree(self, path: str, *, include_self: bool = True) -> list[Department]:
        statement = (
            select(DepartmentRow)
            .where(text("path <@ CAST(:ancestor AS ltree)"))
            .order_by(DepartmentRow.path)
        )
        if not include_self:
            statement = statement.where(DepartmentRow.path != path)
        rows = (
            await self._session.scalars(statement, {"ancestor": path})
        ).all()
        return [_to_domain(row) for row in rows]

    async def subtree_height(self, path: str) -> int:
        """Depth of the deepest row under `path`, relative to `path` itself."""
        deepest = await self._session.scalar(
            text(
                """
                SELECT COALESCE(MAX(nlevel(path)), nlevel(CAST(:path AS ltree)))
                       - nlevel(CAST(:path AS ltree))
                FROM departments
                WHERE path <@ CAST(:path AS ltree)
                """
            ),
            {"path": path},
        )
        return int(deepest or 0)

    async def employee_ids(self, department_id: UUID) -> frozenset[UUID]:
        """Who currently works in this department, by active assignment.

        Read here rather than through the employee module because the department
        is the one asking, and it needs only the ids: whether a named person has a
        connection to this department is a question about the department.
        """
        rows = await self._session.execute(
            text(
                """
                SELECT DISTINCT a.employee_id
                FROM employee_assignments a
                WHERE a.department_id = :department_id AND a.end_date IS NULL
                """
            ),
            {"department_id": department_id},
        )
        return frozenset(rows.scalars())

    async def count_children(self, department_id: UUID) -> int:
        return int(
            await self._session.scalar(
                select(func.count())
                .select_from(DepartmentRow)
                .where(DepartmentRow.parent_id == department_id)
            )
            or 0
        )

    async def count_employees(self, department_id: UUID) -> int:
        """Active staff in this department's subtree.

        Someone in a child team still occupies the parent. The assignments table
        arrives in ticket 07; `to_regclass` returns NULL rather than raising when
        it is absent, so this stays correct on both sides of that ticket.
        """
        has_table = await self._session.scalar(
            text("SELECT to_regclass('public.employee_assignments') IS NOT NULL")
        )
        if not has_table:
            return 0

        result = await self._session.scalar(
            text(
                """
                SELECT COUNT(DISTINCT a.employee_id)
                FROM employee_assignments a
                JOIN departments d ON d.id = a.department_id
                WHERE d.path <@ (SELECT path FROM departments WHERE id = :department_id)
                  AND a.end_date IS NULL
                """
            ),
            {"department_id": department_id},
        )
        return int(result or 0)

    async def save(self, data: DepartmentInput, *, path: str, depth: int) -> Department:
        row = DepartmentRow(
            code=data.code,
            name_es=data.name_es,
            name_en=data.name_en,
            parent_id=data.parent_id,
            path=path,
            depth=depth,
            clearance_level=data.clearance_level.value,
            cost_center=data.cost_center,
            description_es=data.description_es,
            description_en=data.description_en,
            is_active=True,
        )
        self._session.add(row)
        await self._session.flush()
        return _to_domain(row)

    async def set_manager(self, department_id: UUID, employee_id: UUID | None) -> Department:
        """Point the department at whoever approves for it, or at nobody.

        Written directly rather than through `update`, because clearing the field
        is a real operation and the patch convention reads an explicit `None` as
        "leave alone".
        """
        await self._session.execute(
            update(DepartmentRow)
            .where(DepartmentRow.id == department_id)
            .values(manager_employee_id=employee_id)
        )
        await self._session.flush()
        row = await self._session.get(DepartmentRow, department_id)
        assert row is not None
        return _to_domain(row)

    async def update(self, department_id: UUID, patch: DepartmentPatch) -> Department:
        changes = patch.changes()
        if "clearance_level" in changes:
            changes["clearance_level"] = ClearanceLevel(changes["clearance_level"]).value
        if changes:
            await self._session.execute(
                update(DepartmentRow).where(DepartmentRow.id == department_id).values(**changes)
            )
            await self._session.flush()
        row = await self._session.get(DepartmentRow, department_id)
        assert row is not None  # the service checks existence before calling
        return _to_domain(row)

    async def update_code_and_path(
        self, department_id: UUID, *, code: str, path: str, depth: int
    ) -> Department:
        await self._session.execute(
            update(DepartmentRow)
            .where(DepartmentRow.id == department_id)
            .values(code=code, path=path, depth=depth)
        )
        await self._session.flush()
        row = await self._session.get(DepartmentRow, department_id)
        assert row is not None
        return _to_domain(row)

    async def move_subtree(
        self, *, old_path: str, old_code: str, new_path: str, new_code: str
    ) -> None:
        """Rewrite the moved node and every descendant in one statement.

        `subpath(path, nlevel(old))` keeps whatever sits below the moved node, so
        a five-level subtree is fixed with a single UPDATE rather than one per
        row. `old_code`/`new_code` are part of the interface for a future code
        rename; they are unused while moves preserve the code.
        """
        await self._session.execute(
            text(
                """
                UPDATE departments
                SET path = CAST(:new_path AS ltree)
                           || subpath(path, nlevel(CAST(:old_path AS ltree))),
                    depth = depth + (nlevel(CAST(:new_path AS ltree))
                                     - nlevel(CAST(:old_path AS ltree)))
                WHERE path <@ CAST(:old_path AS ltree)
                """
            ),
            {"old_path": old_path, "new_path": new_path},
        )
        await self._session.flush()

    async def delete(self, department_id: UUID) -> None:
        await self._session.execute(delete(DepartmentRow).where(DepartmentRow.id == department_id))
        await self._session.flush()

    async def commit(self) -> None:
        await self._session.commit()
