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
from app.domain.attendance.anomaly_service import AnomalyService
from app.domain.attendance.correction_service import CorrectionService
from app.domain.attendance.service import AttendanceService
from app.domain.employee.models import DirectoryEntry
from app.domain.employee.repository import EmployeeRepository
from app.domain.leave.service import LeaveCalendar, LeaveService
from app.domain.notification.approval import ApprovalNotifier
from app.domain.notification.service import NotificationService
from app.domain.overtime.service import OvertimeLedger
from app.domain.project.service import ProjectService
from app.domain.schedule.service import ScheduleService
from app.domain.timesheet.service import TimesheetService
from app.repositories.approval import PostgresApprovalRepository
from app.repositories.attendance import (
    PostgresAnomalyRepository,
    PostgresAttendanceRepository,
    PostgresCorrectionRepository,
)
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
    return _attendance_collaborators(session)[1]


def _attendance_collaborators(
    session: AsyncSession,
) -> tuple[PostgresAttendanceRepository, AttendanceService]:
    """The stream's repository and the day service beside it, as the routers build them.

    Two callers need both (ticket 24's correction flow takes the repository *and* the
    service), so the pair is assembled once here rather than twice with the risk of two
    different day reads.
    """
    punches = PostgresAttendanceRepository(session)
    service = AttendanceService(
        punches,
        expectations=ScheduleService(PostgresScheduleRepository(session), session),
        overtime=OvertimeLedger(PostgresOvertimeRepository(session)),
    )
    return punches, service


def _approvals(session: AsyncSession) -> ApprovalNotifier:
    """The engine, wrapped so the notifications cannot be forgotten.

    One builder for the three modules that submit documents (ticket 40 adds the correction
    flow to the two ticket 39 wired): a bare `ApprovalService` at one of them would still
    record decisions and silently lose the notices that follow.
    """
    repository = PostgresApprovalRepository(session)
    return ApprovalNotifier(
        engine=ApprovalService(repository, session),
        notifications=NotificationService(PostgresNotificationRepository(session), session),
        approvals=repository,
    )


def leave(session: AsyncSession) -> LeaveService:
    """The leave module, wired the way `app/api/v1/leave.py` wires it.

    `balances()` reads the ledger and nothing else, but the module's constructor asks
    for the schedule and the wrapped approval engine, and handing it `None` would be a
    lie about an object the module may consult on any other call.
    """
    return LeaveService(
        PostgresLeaveRepository(session),
        session,
        expectations=ScheduleService(PostgresScheduleRepository(session), session),
        approvals=_approvals(session),
        annual_leave_days=get_settings().annual_leave_days,
    )


def timesheets(session: AsyncSession, principal: Principal) -> TimesheetService:
    """The timesheet module, whose `employee_id` *is* the caller.

    `principal` is not decoration: `TimesheetService` is built with it and its every
    read is scoped to `principal.employee_id`, so a tool that used this object cannot
    name somebody else even by mistake.
    """
    projects = PostgresProjectRepository(session)
    return TimesheetService(
        PostgresTimesheetRepository(session),
        session,
        principal=principal,
        projects=ProjectService(projects, session),
        project_repository=projects,
        expectations=ScheduleService(PostgresScheduleRepository(session), session),
        approvals=_approvals(session),
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


def corrections(session: AsyncSession) -> CorrectionService:
    """The correction flow, wired the way `app/api/v1/attendance.py::_corrections` wires it.

    Ticket 40's draft tool calls `check_draft` on this — the four rules a correction is
    refused by, and no write. The collaborators are the ones the request path supplies, so
    the punch lineage the draft is checked against is the one the flow itself reads: an
    anomaly scan wired differently here would make a draft refuse a day the flow accepts.
    """
    punches, attendance_service = _attendance_collaborators(session)
    expectations = ScheduleService(PostgresScheduleRepository(session), session)
    return CorrectionService(
        PostgresCorrectionRepository(session),
        session,
        punches=punches,
        attendance=attendance_service,
        anomalies=AnomalyService(
            PostgresAnomalyRepository(session),
            expectations=expectations,
            leave=LeaveCalendar(PostgresLeaveRepository(session)),
        ),
        approvals=_approvals(session),
    )


def projects(session: AsyncSession) -> ProjectService:
    """The project module: the only thing that decides whether a task may be booked.

    A draft's two selects are drawn from it, and the check the form passed came from the
    same `resolve_record_target` — which is what makes "a task the form offered" and "a task
    the submission accepts" the same set rather than two similar ones.
    """
    return ProjectService(PostgresProjectRepository(session), session)


async def contact_rows(session: AsyncSession) -> list[DirectoryEntry]:
    """Every directory row. The caller projects and filters; this only reads."""
    return await directory(session).list_directory()


__all__ = [
    "attendance",
    "contact_rows",
    "corrections",
    "directory",
    "leave",
    "projects",
    "timesheets",
]
