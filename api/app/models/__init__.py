"""ORM models package: importing it registers every table with the metadata."""

from app.models.employee import Employee, EmployeeAssignment, EmployeePrivate, JobPosition
from app.models.org import Department

__all__ = [
    "Department",
    "Employee",
    "EmployeeAssignment",
    "EmployeePrivate",
    "JobPosition",
]
