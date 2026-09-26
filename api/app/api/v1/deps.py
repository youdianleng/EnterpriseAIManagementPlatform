"""Request-scoped dependencies shared by the v1 routers.

Authorisation lives in exactly one place: the `require` dependency factory below,
which asks the access kernel and nothing else. Routers name the action they
perform; they never test a role themselves.
"""

from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ErrorCode
from app.db import get_session
from app.domain.access.kernel import Action, ResourceKind, can
from app.domain.access.principal import Principal
from app.domain.access.snapshot import invalidate_user, resolve_principal


async def db_session() -> AsyncIterator[AsyncSession]:
    async for session in get_session():
        yield session


async def current_principal(
    request: Request,
    session: AsyncSession = Depends(db_session),
) -> Principal:
    """The caller's permission snapshot.

    Read from the session established at login. `resolve_principal` prefers a
    cached snapshot keyed by the account's epoch and the organisation's structure
    version, so a changed input misses the cache rather than being served stale.

    A test harness may set `request.state.principal` before the request runs;
    that is an explicit injection point, not a bypass, because nothing in
    production sets it.
    """
    injected = getattr(request.state, "principal", None)
    if injected is not None:
        return injected

    from app.api.v1.auth import resolve_session

    resolved = await resolve_session(request, session)
    principal = await resolve_principal(session, resolved.account.id)
    if principal is None:
        raise AppError(ErrorCode.SESSION_INVALID, detail="no permission snapshot")
    return principal


def require(action: Action, kind: ResourceKind | None = None):
    """Build a dependency that enforces one catalogued action.

    The refusal is audited, because "who tried and was told no" is a question an
    incident review asks and a plain 403 cannot answer.
    """

    async def guard(
        request: Request,
        principal: Principal = Depends(current_principal),
    ) -> Principal:
        decision = can(principal, action)
        if decision.allowed:
            return principal

        await _audit_refusal(request, principal, action, decision, kind)
        raise AppError(
            ErrorCode.FORBIDDEN,
            detail=f"{action} refused: {decision.primary_reason} ({decision.detail})",
        )

    return guard


def require_public() -> None:
    """Marker for an endpoint that deliberately needs no permission.

    Exists so that opening an endpoint is a visible, reviewable act rather than
    something that happens by omission.
    """
    return None


async def _audit_refusal(
    request: Request,
    principal: Principal,
    action: Action,
    decision,  # noqa: ANN001 - Decision
    kind: ResourceKind | None,
) -> None:
    """Record a refused access attempt.

    In its own session and committed immediately: the request is about to fail,
    and a record that is rolled back with it would leave the refusals invisible —
    which is the half of the audit trail an incident actually needs.
    """
    from app.audit import AuditAction, record
    from app.db import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        await record(
            session,
            action=AuditAction.ACCESS_REFUSED,
            entity_type=kind.value if kind else "endpoint",
            entity_id=None,
            actor_user_id=principal.user_id,
            actor_roles=principal.roles,
            after={
                "action": str(action),
                "path": request.url.path,
                "method": request.method,
                **decision.as_audit_fields(),
            },
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
        await session.commit()


async def invalidate_principal_cache(user_id: UUID) -> None:
    """Drops cached snapshots for one user.

    Called by the writes that change a snapshot's inputs. The key version also
    changes in those cases, so this is belt and braces — but it is the mechanism
    that makes "immediately, not after the TTL" literally true rather than
    approximately true.
    """
    await invalidate_user(user_id)
