"""The domain services a read-only tool calls, assembled exactly as the API assembles them.

A tool reads; it does not implement a query. 「不要重新实现考勤/假期/工时/通讯录查询」 is a
requirement of ticket 39, so every one of the five tools below hands off to one of the
four collaborators here — `AttendanceService`, `LeaveService`, `TimesheetService` and
the employee repository's directory read — and this module is the only place in
`app/ai/**` that knows what a repository is.

**Why the wiring is repeated rather than imported from the routers.** The request
handlers build the same four objects (`app/api/v1/attendance.py:_collaborators`,
`leave.py:_service`, `timesheets.py:_service`), but those helpers are private to the
routers and parameterised by a `Request`: a tool has no request, and importing a
router into an agent would make the dependency direction `ai → api` — the one
direction this tree does not have. What the two must not disagree about is the
*service* — the rules — and they do not: both hand the same class the same
collaborators, and a test asserts the tools reach those classes.

**Nothing here writes, and that is not a promise.** None of these functions calls a
write method, `tests/test_agent_readonly_tools.py` walks their source and the tools'
source with `ast` and fails on a write-shaped call or a write-shaped SQL literal, and
a positive control proves the walker catches what it is looking for. Constraint B's
runtime and database layers (tickets 41 and the read-only role) are what stop a
*wrong* read from being a write; this layer is what stops a write tool existing.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.domain.access.principal import Principal
from app.domain.approval.service import ApprovalService
from app.domain.attendance.service import AttendanceService
from app.domain.employee.models import DirectoryEntry
from app.domain.employee.repository import EmployeeRepository
from app.domain.leave.service import LeaveService
from app.domain.notification.approval import ApprovalNotifier
from app.domain.notification.service import NotificationService
from app.domain.overtime.service import OvertimeLedger
from app.domain.project.service import ProjectService
from app.domain.schedule.service import ScheduleService
from app.domain.timesheet.service import TimesheetService
from app.repositories.approval import PostgresApprovalRepository
from app.repositories.attendance import PostgresAttendanceRepository
from app.repositories.employee import PostgresEmployeeRepository
from app.repositories.leave import PostgresLeaveRepository
from app.repositories.notification import PostgresNotificationRepository
from app.repositories.overtime import PostgresOvertimeRepository
from app.repositories.project import PostgresProjectRepository
from app.repositories.schedule import PostgresScheduleRepository
from app.repositories.timesheet import PostgresTimesheetRepository


def attendance(session: AsyncSession) -> AttendanceService:
    """The attendance module, with the schedule and the overtime ledger behind it.

    The two collaborators are optional on `AttendanceService` and supplied here for
    the reason the request path supplies them: a day read without them has no
    `expected_minutes` and no `overtime_minutes`, and the figures a caller asks about
    would depend on which surface answered.
    """
    return AttendanceService(
        PostgresAttendanceRepository(session),
        expectations=ScheduleService(PostgresScheduleRepository(session), session),
        overtime=OvertimeLedger(PostgresOvertimeRepository(session)),
    )


def leave(session: AsyncSession) -> LeaveService:
    """The leave module, wired the way `app/api/v1/leave.py` wires it.

    `balances()` reads the ledger and nothing else, but the module's constructor asks
    for the schedule and the wrapped approval engine, and handing it `None` would be a
    lie about an object the module may consult on any other call.
    """
    approvals = PostgresApprovalRepository(session)
    return LeaveService(
        PostgresLeaveRepository(session),
        session,
        expectations=ScheduleService(PostgresScheduleRepository(session), session),
        approvals=ApprovalNotifier(
            engine=ApprovalService(approvals, session),
            notifications=NotificationService(
                PostgresNotificationRepository(session), session
            ),
            approvals=approvals,
        ),
        annual_leave_days=get_settings().annual_leave_days,
    )


def timesheets(session: AsyncSession, principal: Principal) -> TimesheetService:
    """The timesheet module, whose `employee_id` *is* the caller.

    `principal` is not decoration: `TimesheetService` is built with it and its every
    read is scoped to `principal.employee_id`, so a tool that used this object cannot
    name somebody else even by mistake.
    """
    projects = PostgresProjectRepository(session)
    approvals = PostgresApprovalRepository(session)
    return TimesheetService(
        PostgresTimesheetRepository(session),
        session,
        principal=principal,
        projects=ProjectService(projects, session),
        project_repository=projects,
        expectations=ScheduleService(PostgresScheduleRepository(session), session),
        approvals=ApprovalNotifier(
            engine=ApprovalService(approvals, session),
            notifications=NotificationService(
                PostgresNotificationRepository(session), session
            ),
            approvals=approvals,
        ),
    )


def directory(session: AsyncSession) -> EmployeeRepository:
    """The employee repository's directory read — the one the contact tool filters.

    A repository rather than `EmployeeService.list_directory`, because that method is
    a one-line delegation to this one: the service exists for the *rules* (uniqueness,
    the primary position, the projection), and none of them is reached by a listing.
    Which repository the API's own directory endpoint reads through is asserted in
    `tests/test_agent_readonly_tools.py`.
    """
    return PostgresEmployeeRepository(session)


async def contact_rows(session: AsyncSession) -> list[DirectoryEntry]:
    """Every directory row. The caller projects and filters; this only reads."""
    return await directory(session).list_directory()


__all__ = ["attendance", "contact_rows", "directory", "leave", "timesheets"]
