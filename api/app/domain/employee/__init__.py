"""Employee domain: records, position assignments and field visibility."""

from app.domain.employee.models import (
    Assignment,
    AssignmentInput,
    DirectoryEntry,
    Employee,
    EmployeeInput,
    EmployeePatch,
    EmployeePrivate,
    EmployeeRecord,
    EmploymentStatus,
    JobPosition,
)
from app.domain.employee.repository import EmployeeRepository
from app.domain.employee.service import EmployeeService, resolve_viewer_context
from app.domain.employee.visibility import (
    MANAGING_ROLES,
    PRIVILEGED_ROLES,
    Projection,
    ViewerContext,
    project_directory_row,
    project_private,
    resolve_visibility,
)

__all__ = [
    "MANAGING_ROLES",
    "PRIVILEGED_ROLES",
    "Assignment",
    "AssignmentInput",
    "DirectoryEntry",
    "Employee",
    "EmployeeInput",
    "EmployeePatch",
    "EmployeePrivate",
    "EmployeeRecord",
    "EmployeeRepository",
    "EmployeeService",
    "EmploymentStatus",
    "JobPosition",
    "Projection",
    "ViewerContext",
    "project_directory_row",
    "project_private",
    "resolve_viewer_context",
    "resolve_visibility",
]
