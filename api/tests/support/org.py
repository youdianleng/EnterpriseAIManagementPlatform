"""In-memory department repository.

Satisfies the same Protocol as the PostgreSQL implementation, which is what lets
the structural rules be tested without a database. Kept deliberately small: it
reimplements only the behaviour the rules depend on, and it is never used by the
application.
"""

from uuid import UUID, uuid4

from app.domain.org.models import (
    ClearanceLevel,
    Department,
    DepartmentInput,
    DepartmentPatch,
)
from app.domain.org.paths import depth_of, is_descendant_path


class InMemoryDepartmentRepository:
    def __init__(self, department_count_with_employees: dict[UUID, int] | None = None) -> None:
        self.rows: dict[UUID, Department] = {}
        # Lets a test declare that a department "has staff" before ticket 07
        # introduces the assignments table.
        self.employees: dict[UUID, int] = department_count_with_employees or {}
        self.commits = 0

    # --- helpers -----------------------------------------------------------

    def _replace(self, department: Department) -> None:
        self.rows[department.id] = department

    # --- reads -------------------------------------------------------------

    async def get(self, department_id: UUID) -> Department | None:
        return self.rows.get(department_id)

    async def get_by_code(self, code: str) -> Department | None:
        return next((row for row in self.rows.values() if row.code == code), None)

    async def list_all(self, *, include_inactive: bool = True) -> list[Department]:
        rows = [row for row in self.rows.values() if include_inactive or row.is_active]
        return sorted(rows, key=lambda row: row.path)

    async def list_subtree(self, path: str, *, include_self: bool = True) -> list[Department]:
        rows = [row for row in self.rows.values() if is_descendant_path(row.path, path)]
        if not include_self:
            rows = [row for row in rows if row.path != path]
        return sorted(rows, key=lambda row: row.path)

    async def subtree_height(self, path: str) -> int:
        """Levels below `path`. Matches the SQL, which counts descendant depth
        minus the node's own depth, so a childless node reports 0."""
        own_depth = depth_of(path)
        return max(
            (
                row.depth - own_depth
                for row in self.rows.values()
                if is_descendant_path(row.path, path)
            ),
            default=0,
        )

    async def count_children(self, department_id: UUID) -> int:
        return sum(1 for row in self.rows.values() if row.parent_id == department_id)

    async def count_employees(self, department_id: UUID) -> int:
        return self.employees.get(department_id, 0)

    # --- writes ------------------------------------------------------------

    async def save(self, data: DepartmentInput, *, path: str, depth: int) -> Department:
        department = Department(
            id=uuid4(),
            code=data.code,
            name_es=data.name_es,
            name_en=data.name_en,
            parent_id=data.parent_id,
            path=path,
            depth=depth,
            clearance_level=data.clearance_level,
            cost_center=data.cost_center,
            manager_employee_id=None,
            description_es=data.description_es,
            description_en=data.description_en,
            is_active=True,
        )
        self._replace(department)
        return department

    async def update(self, department_id: UUID, patch: DepartmentPatch) -> Department:
        current = self.rows[department_id]
        changes = patch.changes()
        if "clearance_level" in changes:
            changes["clearance_level"] = ClearanceLevel(changes["clearance_level"])
        updated = Department(**{**self._as_dict(current), **changes})
        self._replace(updated)
        return updated

    async def update_code_and_path(
        self, department_id: UUID, *, code: str, path: str, depth: int
    ) -> Department:
        current = self.rows[department_id]
        updated = Department(
            **{**self._as_dict(current), "code": code, "path": path, "depth": depth}
        )
        self._replace(updated)
        return updated

    async def move_subtree(
        self, *, old_path: str, old_code: str, new_path: str, new_code: str
    ) -> None:
        """Mirror of the SQL: keep whatever sits *below* the moved node.

        The suffix has to start after a whole label, not after the prefix
        characters, otherwise the descendant path loses its separator — this
        substitute got that wrong once, which is exactly the class of bug a
        substitute can introduce and the real repository cannot.
        """
        for row in list(self.rows.values()):
            if not is_descendant_path(row.path, old_path):
                continue
            suffix = row.path[len(old_path) :]  # "" for the node itself
            self._replace(
                Department(
                    **{
                        **self._as_dict(row),
                        "path": f"{new_path}{suffix}",
                        "depth": row.depth + (depth_of(new_path) - depth_of(old_path)),
                    }
                )
            )

    async def delete(self, department_id: UUID) -> None:
        del self.rows[department_id]

    async def commit(self) -> None:
        self.commits += 1

    @staticmethod
    def _as_dict(department: Department) -> dict:
        return {
            field: getattr(department, field)
            for field in (
                "id",
                "code",
                "name_es",
                "name_en",
                "parent_id",
                "path",
                "depth",
                "clearance_level",
                "cost_center",
                "manager_employee_id",
                "description_es",
                "description_en",
                "is_active",
            )
        }


def make_input(code: str, parent_id: UUID | None = None, **overrides: object) -> DepartmentInput:
    defaults: dict = {
        "code": code,
        "name_es": f"{code} es",
        "name_en": f"{code} en",
        "parent_id": parent_id,
    }
    defaults.update(overrides)
    return DepartmentInput(**defaults)


__all__ = ["InMemoryDepartmentRepository", "make_input"]
