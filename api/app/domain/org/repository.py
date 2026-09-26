"""Persistence contract for departments.

This is a real seam rather than a hypothetical one: production uses the
PostgreSQL implementation, and the service's rules are exercised against
in-memory substitutes that do not need a database.

Implementations must never commit. Transaction control belongs to the caller,
because a department move writes several rows that have to land together.
"""

from typing import Protocol
from uuid import UUID

from app.domain.org.models import Department, DepartmentInput, DepartmentPatch


class DepartmentRepository(Protocol):
    async def get(self, department_id: UUID) -> Department | None: ...

    async def get_by_code(self, code: str) -> Department | None: ...

    async def list_all(self, *, include_inactive: bool = True) -> list[Department]: ...

    async def list_subtree(self, path: str, *, include_self: bool = True) -> list[Department]: ...

    async def subtree_height(self, path: str) -> int:
        """Deepest level below `path`, relative to it (0 for a childless node)."""
        ...

    async def count_children(self, department_id: UUID) -> int: ...

    async def count_employees(self, department_id: UUID) -> int: ...

    async def save(self, data: DepartmentInput, *, path: str, depth: int) -> Department: ...

    async def update(self, department_id: UUID, patch: DepartmentPatch) -> Department: ...

    async def update_code_and_path(
        self, department_id: UUID, *, code: str, path: str, depth: int
    ) -> Department: ...

    async def move_subtree(
        self, *, old_path: str, old_code: str, new_path: str, new_code: str
    ) -> None: ...

    async def delete(self, department_id: UUID) -> None: ...

    async def commit(self) -> None: ...
