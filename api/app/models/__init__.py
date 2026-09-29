"""ORM models package: importing it registers every table with the metadata."""

from app.models.account import User
from app.models.agent_action import AgentAction
from app.models.approval import ApprovalDecision, ApprovalRequest, ApprovalStep
from app.models.audit import AuditLog
from app.models.document import Document, DocumentChunk
from app.models.employee import Employee, EmployeeAssignment, EmployeePrivate, JobPosition
from app.models.leave import (
    LeaveBalance,
    LeaveBalanceEntry,
    LeaveRequest,
    LeaveType,
)
from app.models.notification import Notification, NotificationDelivery
from app.models.org import Department
from app.models.overtime import OvertimeEntry, OvertimeRecord, OvertimeRequest
from app.models.payroll import SalaryRecord
from app.models.personnel import PersonnelChange
from app.models.project import Project, ProjectTask
from app.models.schedule import (
    EmployeeScheduleOverride,
    ExpectedHoursSnapshot,
    Holiday,
    WorkSchedule,
    WorkScheduleDay,
)
from app.models.timesheet import Timesheet, TimesheetEntry

__all__ = [
    "AgentAction",
    "ApprovalDecision",
    "ApprovalRequest",
    "ApprovalStep",
    "AuditLog",
    "Department",
    "Document",
    "DocumentChunk",
    "Employee",
    "EmployeeAssignment",
    "EmployeePrivate",
    "EmployeeScheduleOverride",
    "ExpectedHoursSnapshot",
    "Holiday",
    "JobPosition",
    "LeaveBalance",
    "LeaveBalanceEntry",
    "LeaveRequest",
    "LeaveType",
    "Notification",
    "NotificationDelivery",
    "OvertimeEntry",
    "OvertimeRecord",
    "OvertimeRequest",
    "PersonnelChange",
    "Project",
    "ProjectTask",
    "SalaryRecord",
    "Timesheet",
    "TimesheetEntry",
    "User",
    "WorkSchedule",
    "WorkScheduleDay",
]
