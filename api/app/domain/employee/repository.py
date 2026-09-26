"""Persistence contract for employees.

A real seam: the PostgreSQL implementation runs in production, an in-memory
substitute carries the rule tests. Implementations never commit — a profile and
its first assignment have to land together.
"""

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from app.domain.employee.models import (
    Assignment,
    AssignmentInput,
    DirectoryEntry,
    Employee,
    EmployeeInput,
    EmployeePatch,
    EmployeePrivate,
    EmployeeRecord,
    JobPosition,
)


class EmployeeRepository(Protocol):
    async def get(self, employee_id: UUID) -> Employee | None: ...

    async def get_by_email(self, email: str) -> Employee | None: ...

    async def get_by_employee_no(self, employee_no: str) -> Employee | None: ...

    async def get_private(self, employee_id: UUID) -> EmployeePrivate: ...

    async def load(
        self, employee_id: UUID, *, include_assignments: bool = True
    ) -> EmployeeRecord | None: ...

    async def list_directory(
        self, *, department_ids: frozenset[UUID] | None = None, include_terminated: bool = False
    ) -> list[DirectoryEntry]: ...

    async def save(self, data: EmployeeInput) -> Employee: ...

    async def update(self, employee_id: UUID, patch: EmployeePatch) -> Employee: ...

    async def save_private(
        self, employee_id: UUID, private: EmployeePrivate
    ) -> EmployeePrivate: ...

    async def list_assignments(
        self, employee_id: UUID, *, on_date: object | None = None
    ) -> list[Assignment]: ...

    async def get_position(self, position_id: UUID) -> JobPosition | None: ...

    async def get_position_title(self, position_id: UUID) -> tuple[str, str] | None: ...

    async def save_assignment(
        self, employee_id: UUID, data: AssignmentInput, *, is_primary: bool
    ) -> Assignment: ...

    async def end_assignment(
        self, assignment_id: UUID, *, on_date: object | None = None
    ) -> None: ...

    async def set_primary(self, employee_id: UUID, assignment_id: UUID) -> None: ...

    async def employee_exists(self, employee_id: UUID) -> bool: ...

    async def commit(self) -> None: ...


class DepartmentLookup(Protocol):
    """The slice of the organisation module the employee service needs."""

    async def get(self, department_id: UUID) -> object | None: ...

    async def list_subtree(self, path: str, *, include_self: bool = True) -> Sequence[object]: ...


__all__ = ["DepartmentLookup", "EmployeeRepository"]
