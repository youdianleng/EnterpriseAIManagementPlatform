"""Writing audit records.

Records are *append-only*: this module offers an `record` function and no way to
update or remove one. That is deliberate, but it is not yet sufficient — the
application connection currently owns the table, so a bug could still issue an
UPDATE. Ticket 13 addresses it at the database level by running the application
under a role with INSERT and SELECT only on `audit_log`, which is the point at
which the guarantee stops depending on application code. It is noted here so the
gap is not mistaken for a finished design.

Ticket 14 builds the compliance read surface and the full action catalogue on top
of this; ticket 09 needs only the write path, for the three account operations it
must audit.
"""

from enum import StrEnum
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
    a prefix match rather than a list of unrelated strings. Ticket 14 extends it
    to the remaining domains.
    """

    ACCOUNT_CREATED = "account.created"
    ACCOUNT_DEACTIVATED = "account.deactivated"
    ACCOUNT_REACTIVATED = "account.reactivated"
    ACCOUNT_PASSWORD_RESET = "account.password_reset"
    ACCOUNT_PASSWORD_CHANGED = "account.password_changed"

    # Authentication. Failures are recorded too: a trail of successes only cannot
    # answer "was somebody trying to get in".
    LOGIN_SUCCEEDED = "auth.login_succeeded"
    LOGIN_FAILED = "auth.login_failed"
    LOGOUT = "auth.logout"
    PASSWORD_CHANGED = "auth.password_changed"
    SESSIONS_FORCED_OUT = "auth.sessions_forced_out"


def _snapshot(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """Copy of a snapshot, minus anything that must never be stored.

    A password hash is not a secret worth keeping in a second table, and a
    plaintext password must never reach one at all.
    """
    if payload is None:
        return None
    redacted = {"password", "password_hash", "temporary_password", "token", "secret"}
    return {key: value for key, value in payload.items() if key not in redacted}


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
    """
    if request_id is None:
        bound = structlog.contextvars.get_contextvars()
        request_id = bound.get("request_id")

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
