"""ORM models package: importing it registers every table with the metadata."""

from app.models.account import User
from app.models.approval import ApprovalDecision, ApprovalRequest, ApprovalStep
from app.models.audit import AuditLog
from app.models.employee import Employee, EmployeeAssignment, EmployeePrivate, JobPosition
from app.models.org import Department

__all__ = [
    "ApprovalDecision",
    "ApprovalRequest",
    "ApprovalStep",
    "AuditLog",
    "Department",
    "Employee",
    "EmployeeAssignment",
    "EmployeePrivate",
    "JobPosition",
    "User",
]
