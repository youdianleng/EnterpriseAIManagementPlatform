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

    # Later tickets, named now so the catalogue is the one place a reader has to
    # look to know what this system can tell them about itself.
    DOCUMENT_UPLOADED = "document.uploaded"  # 31
    DOCUMENT_VISIBILITY_CHANGED = "document.visibility_changed"  # 36
    SALARY_RECORD_READ = "salary.record_read"  # 43
    PAYSLIP_UPLOADED = "payslip.uploaded"  # 44
    PAYSLIP_DOWNLOADED = "payslip.downloaded"  # 45
    PAYSLIP_WITHDRAWN = "payslip.withdrawn"  # 46
    APPROVAL_DECIDED = "approval.decided"  # 16
    #: Filed and taken back. A withdrawal is a state change somebody made, and the
    #: request row would otherwise be the only trace of it — with no record of who
    #: did it, which is the question an incident review asks first.
    APPROVAL_SUBMITTED = "approval.submitted"  # 16
    APPROVAL_WITHDRAWN = "approval.withdrawn"  # 16
    AGENT_ACTION_PROPOSED = "agent.action_proposed"  # 40
    AGENT_ACTION_CONFIRMED = "agent.action_confirmed"  # 41
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
