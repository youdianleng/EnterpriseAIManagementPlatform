"""Writing audit records.

Records are *append-only*, and that is now enforced by the database rather than by
this module's good manners: the application connects as a role holding INSERT and
SELECT on `audit_log` and nothing else (ticket 13), so an UPDATE or a DELETE is
refused by PostgreSQL, not by a code path somebody has to remember.

**One entry point, and no arguments the caller has to remember.** `record()` takes
what changed; who did it, from where and with which roles come from the request
context that `bind_actor` publishes when the principal is resolved. That is what
makes auditing a write a one-line change instead of a signature change — and the
reason the department, position and employee services could be audited in ticket
14 without threading an actor through every method.
"""

from datetime import date, datetime
from enum import Enum, StrEnum
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.logging import get_logger
from app.models.audit import AuditLog

logger = get_logger(__name__)


class AuditAction(StrEnum):
    """The action catalogue.

    Named `entity.verb` so a filter on "everything that happened to accounts" is
    a prefix match rather than a list of unrelated strings.

    Actions for the modules that do not exist yet are listed with the ticket that
    will emit them. They are here now because the compliance requirement names
    them (`docs/DESIGN.md` §6), and an action that has to be invented at the point
    of use is an action that gets spelled differently at each point of use.
    """

    ACCOUNT_CREATED = "account.created"
    ACCOUNT_DEACTIVATED = "account.deactivated"
    ACCOUNT_REACTIVATED = "account.reactivated"
    ACCOUNT_PASSWORD_RESET = "account.password_reset"

    # Authentication. Failures are recorded too: a trail of successes only cannot
    # answer "was somebody trying to get in".
    LOGIN_SUCCEEDED = "auth.login_succeeded"
    LOGIN_FAILED = "auth.login_failed"
    LOGOUT = "auth.logout"
    #: One action for "somebody set their own password", whichever screen did it.
    PASSWORD_CHANGED = "auth.password_changed"
    SESSIONS_FORCED_OUT = "auth.sessions_forced_out"

    # Authorisation. A refused attempt is half of what an incident review needs;
    # the other half is who was allowed.
    ACCESS_REFUSED = "access.refused"

    # Organisation and people.
    DEPARTMENT_CREATED = "department.created"
    DEPARTMENT_UPDATED = "department.updated"
    #: Moving a subtree is its own action: it changes the reach of everybody in
    #: it, which is a different question from a rename.
    DEPARTMENT_MOVED = "department.moved"
    DEPARTMENT_DELETED = "department.deleted"
    POSITION_CREATED = "position.created"
    POSITION_UPDATED = "position.updated"
    POSITION_DEACTIVATED = "position.deactivated"
    POSITION_DELETED = "position.deleted"
    EMPLOYEE_CREATED = "employee.created"
    EMPLOYEE_UPDATED = "employee.updated"
    EMPLOYEE_PRIVATE_UPDATED = "employee.private_updated"
    ASSIGNMENT_ADDED = "assignment.added"
    ASSIGNMENT_ENDED = "assignment.ended"
    ASSIGNMENT_PRIMARY_CHANGED = "assignment.primary_changed"

    # Roles and clearance are the two inputs to every permission decision, so a
    # change to either is the change an incident review looks for first.
    ROLES_CHANGED = "user.roles_changed"
    CLEARANCE_CHANGED = "user.clearance_changed"

    # Projects and their tasks (ticket 27). Archive and deactivate are their own
    # actions rather than an update: each is the moment the record stops accepting
    # new work, which is the change an incident review asks about, and neither is
    # expressible as "some column moved".
    PROJECT_CREATED = "project.created"
    PROJECT_UPDATED = "project.updated"
    PROJECT_ARCHIVED = "project.archived"
    PROJECT_TASK_CREATED = "project_task.created"
    PROJECT_TASK_UPDATED = "project_task.updated"
    PROJECT_TASK_DEACTIVATED = "project_task.deactivated"

    # Later tickets, named now so the catalogue is the one place a reader has to
    # look to know what this system can tell them about itself.
    DOCUMENT_UPLOADED = "document.uploaded"  # 31
    #: Parsing (ticket 31). Two actions rather than one, and the pair is the point:
    #: "this document is ready" and "this document could not be read" are different
    #: facts about a company's knowledge base, and an incident review asks which
    #: documents were *refused* — a scanned file that nobody noticed is a gap in the
    #: answers the assistant gives, not a failed request in a log.
    #:
    #: Written by the job, and therefore `initiated_by="system"`: no user is acting,
    #: so attributing them to whoever happened to upload the file would be a lie the
    #: trail tells about itself.
    DOCUMENT_PARSED = "document.parsed"  # 31
    DOCUMENT_PARSE_FAILED = "document.parse_failed"  # 31
    DOCUMENT_VISIBILITY_CHANGED = "document.visibility_changed"  # 36
    SALARY_RECORD_READ = "salary.record_read"  # 43
    PAYSLIP_UPLOADED = "payslip.uploaded"  # 44
    PAYSLIP_DOWNLOADED = "payslip.downloaded"  # 45
    PAYSLIP_WITHDRAWN = "payslip.withdrawn"  # 46
    #: A second attempt at an event that was already raised. Recorded rather than
    #: dropped silently: "the notifier ran twice" and "the notifier never ran" look
    #: identical from a notification that is simply absent.
    NOTIFICATION_DUPLICATE_SUPPRESSED = "notification.duplicate_suppressed"  # 19
    APPROVAL_DECIDED = "approval.decided"  # 16
    #: Filed and taken back. A withdrawal is a state change somebody made, and the
    #: request row would otherwise be the only trace of it — with no record of who
    #: did it, which is the question an incident review asks first.
    APPROVAL_SUBMITTED = "approval.submitted"  # 16
    APPROVAL_WITHDRAWN = "approval.withdrawn"  # 16
    #: A personnel change taking effect, which is not the moment it was approved.
    #: The record carries the before/after pair each field change was applied with,
    #: because the payload states what was agreed and this states what was written.
    PERSONNEL_CHANGE_APPLIED = "personnel_change.applied"  # 17
    #: Cancelling an approved-but-unapplied change: a state change somebody made,
    #: with a reason, and the row's own columns are not the trail for it.
    PERSONNEL_CHANGE_CANCELLED = "personnel_change.cancelled"  # 17
    #: Work schedules and the holiday calendar (ticket 22). The two are the inputs
    #: to every expected-hours figure, so a change to either changes what the
    #: company owes somebody — which is why each is its own action rather than one
    #: "configuration changed".
    SCHEDULE_CREATED = "schedule.created"  # 22
    SCHEDULE_UPDATED = "schedule.updated"  # 22
    SCHEDULE_OVERRIDE_SET = "schedule.override_set"  # 22
    SCHEDULE_OVERRIDE_REMOVED = "schedule.override_removed"  # 22
    HOLIDAY_CREATED = "holiday.created"  # 22
    HOLIDAY_UPDATED = "holiday.updated"  # 22
    HOLIDAY_DELETED = "holiday.deleted"  # 22
    #: One record per import, with the counts: a calendar loaded from a file is a
    #: bulk change to a figure that reaches a payroll report, and "who loaded which
    #: file, and what did it do" is the question asked afterwards.
    HOLIDAYS_IMPORTED = "holiday.imported"  # 22
    #: A month's expected hours frozen with the rules that produced them. Recorded
    #: because the figure is evidence: the row says what it was measured against,
    #: and this says when it was written and on whose instruction.
    EXPECTED_HOURS_SNAPSHOTTED = "attendance.expected_hours_snapshotted"  # 22
    #: Weekly timesheets (ticket 28). Three actions rather than one, and the third
    #: is the one that matters: filing a week is the moment it stops being the
    #: employee's to change, and "who filed which week, when" is the question an
    #: approval dispute asks. The two edit actions are separate for the same reason
    #: the punch stream separates recording from correcting: an entry written and an
    #: entry removed are different facts about a week.
    TIMESHEET_CREATED = "timesheet.created"  # 28
    TIMESHEET_ENTRY_WRITTEN = "timesheet.entry_written"  # 28
    TIMESHEET_ENTRY_REMOVED = "timesheet.entry_removed"  # 28
    TIMESHEET_SUBMITTED = "timesheet.submitted"  # 28
    TIMESHEET_COPIED = "timesheet.copied"  # 28
    #: Ticket 29. Filing writes the engine's own `approval.submitted` and each decision
    #: writes `approval.decided`, so what this module records itself is the three
    #: moments that are *its*: the stored status catching up with a decision taken
    #: elsewhere (written by the system, because the engine decided and this module
    #: only wrote it down), a correction being opened — with the reversal pairs it
    #: wrote — and a week being closed for good when a write reached it too late.
    TIMESHEET_DECISION_APPLIED = "timesheet.decision_applied"  # 29
    TIMESHEET_SUPPLEMENT_OPENED = "timesheet.supplement_opened"  # 29
    TIMESHEET_WEEK_LOCKED = "timesheet.week_locked"  # 29
    #: A correction request (ticket 24): the document a person writes to restate a
    #: punch, and the append that carries it out. Two actions, not three, because
    #: the middle one is the engine's: filing writes `approval.submitted` and each
    #: decision writes `approval.decided`, both keyed on the correction's own id, so
    #: "everything that happened to this correction" is one equality filter. A
    #: second record of the same decision under a different name would be a copy
    #: that eventually disagrees with the engine.
    ATTENDANCE_CORRECTION_REQUESTED = "attendance_correction.requested"  # 24
    ATTENDANCE_CORRECTION_UPDATED = "attendance_correction.updated"  # 24
    ATTENDANCE_CORRECTION_APPLIED = "attendance_correction.applied"  # 24
    #: Leave (ticket 25). Six actions, and the middle moment is the engine's here
    #: too: filing writes `approval.submitted` and each decision writes
    #: `approval.decided`, keyed on the request's own id. What this module records
    #: itself is the catalogue it maintains, the allowance it adjusts, the balance
    #: movement an approval or a rejection caused, and the requester's withdrawal —
    #: the last one because an approved leave withdrawn before it starts is an act
    #: the engine never sees, and a leave that suppresses an absence with no record
    #: of who stopped it is exactly what an incident review asks about.
    LEAVE_TYPE_CREATED = "leave_type.created"  # 25
    LEAVE_TYPE_UPDATED = "leave_type.updated"  # 25
    LEAVE_BALANCE_ADJUSTED = "leave_balance.adjusted"  # 25
    LEAVE_REQUEST_DRAFTED = "leave_request.drafted"  # 25
    LEAVE_REQUEST_SETTLED = "leave_request.settled"  # 25
    LEAVE_REQUEST_WITHDRAWN = "leave_request.withdrawn"  # 25
    #: Overtime (ticket 26). The middle moment is the engine's here too — filing
    #: writes `approval.submitted` and each decision writes `approval.decided`, keyed
    #: on the request's own id — so what this module records itself is the draft it
    #: wrote, the record an approval produced, the settlement that compared the two
    #: figures, HR's confirmation of one of them, and the requester's withdrawal. The
    #: settle and the confirm are separate actions because they are separate facts: one
    #: is arithmetic over the day, the other is a person overruling it with a reason,
    #: and an incident review asks which of the two produced the figure it is reading.
    OVERTIME_REQUEST_DRAFTED = "overtime_request.drafted"  # 26
    OVERTIME_REQUEST_UPDATED = "overtime_request.updated"  # 26
    OVERTIME_REQUEST_WITHDRAWN = "overtime_request.withdrawn"  # 26
    OVERTIME_REQUEST_RESOLVED = "overtime_request.resolved"  # 26
    OVERTIME_RECORD_SETTLED = "overtime_record.settled"  # 26
    OVERTIME_RECORD_CONFIRMED = "overtime_record.confirmed"  # 26
    AGENT_ACTION_PROPOSED = "agent.action_proposed"  # 40
    AGENT_ACTION_CONFIRMED = "agent.action_confirmed"  # 41
    #: One record per export, carrying the period and what the file stated. Finance
    #: re-runs a month as a matter of course, so the trail is what makes "who exported
    #: which period, when, and how many rows did it say" answerable; the file itself is
    #: a report and holds no marker of its own (ticket 26, and ticket 47's exports).
    DATA_EXPORTED = "data.exported"  # 26, 47


#: Context keys `bind_actor` publishes and `record` reads.
_ACTOR_KEYS = ("actor_user_id", "actor_roles", "ip_address", "user_agent")


def bind_actor(
    *,
    user_id: UUID | None,
    roles: frozenset[str],
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Publish who is acting, for the rest of this request.

    Bound, not passed: every service method would otherwise need an `actor`
    parameter it does nothing with but forward, and the one that forgets is the
    one whose changes are unattributed. The request middleware clears the context
    at the start of each request, so this cannot leak into the next one.
    """
    structlog.contextvars.bind_contextvars(
        actor_user_id=str(user_id) if user_id else None,
        actor_roles=sorted(roles),
        ip_address=ip_address,
        user_agent=user_agent,
    )


def _bound(key: str) -> Any:
    return structlog.contextvars.get_contextvars().get(key)


def _as_uuid(value: object) -> UUID | None:
    """The context carries strings, because logs do; the column wants a UUID."""
    if isinstance(value, UUID):
        return value
    if isinstance(value, str) and value:
        try:
            return UUID(value)
        except ValueError:
            return None
    return None


def _snapshot(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """Copy of a snapshot, storable and minus anything that must never be stored.

    Two jobs, both of them this module's rather than every caller's. A password
    hash is not a secret worth keeping in a second table, and a plaintext password
    must never reach one at all; and a snapshot has to survive JSON, because the
    column is JSONB and an audit write that raises is an audit write that loses the
    change it was describing. Callers pass domain values — an enum, a `date`, a
    `UUID` — and this turns them into something the column accepts.
    """
    if payload is None:
        return None
    redacted = {"password", "password_hash", "temporary_password", "token", "secret"}
    return {
        key: _storable(value) for key, value in payload.items() if key not in redacted
    }


def _storable(value: Any) -> Any:
    """Anything a domain object can hold, as something JSONB can hold."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _storable(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_storable(item) for item in value]
    return value


async def record(
    session: AsyncSession,
    *,
    action: AuditAction,
    entity_type: str,
    entity_id: UUID | None,
    actor_user_id: UUID | None = None,
    actor_roles: frozenset[str] | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    reason: str | None = None,
    initiated_by: str = "user",
    request_id: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Append one record.

    Does not commit: an audit entry belongs to the same transaction as the change
    it describes, so the two either both land or neither does. An audit trail
    that can disagree with the data is worse than none.

    Actor, address and client come from the request context unless the caller
    knows better — a login records the credentials' username before any principal
    exists, so authentication passes them explicitly.
    """
    bound = structlog.contextvars.get_contextvars()
    if request_id is None:
        request_id = bound.get("request_id")
    if actor_user_id is None:
        actor_user_id = _as_uuid(bound.get("actor_user_id"))
    if actor_roles is None:
        actor_roles = frozenset(bound.get("actor_roles") or ())
    if ip_address is None:
        ip_address = bound.get("ip_address")
    if user_agent is None:
        user_agent = bound.get("user_agent")

    entry = AuditLog(
        action=action.value,
        entity_type=entity_type,
        entity_id=entity_id,
        actor_user_id=actor_user_id,
        actor_roles=sorted(actor_roles or ()),
        before=_snapshot(before),
        after=_snapshot(after),
        reason=reason,
        initiated_by=initiated_by,
        request_id=request_id,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    session.add(entry)
    await session.flush()
    logger.info(
        "audit_recorded",
        action=action.value,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id else None,
        actor_user_id=str(actor_user_id) if actor_user_id else None,
    )


__all__ = ["AuditAction", "record"]
