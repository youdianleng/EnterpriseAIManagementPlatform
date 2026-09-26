"""Request-scoped dependencies shared by the v1 routers."""

from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ErrorCode
from app.db import get_session
from app.domain.employee.service import resolve_viewer_context
from app.domain.employee.visibility import ViewerContext
from app.repositories.employee import PostgresEmployeeRepository
from app.repositories.org import PostgresDepartmentRepository

# Roles permitted to change the organisation structure.
STRUCTURE_ROLES = {"admin", "hr"}

# Roles permitted to create or correct employee records.
EMPLOYEE_MANAGING_ROLES = {"admin", "hr"}


async def db_session() -> AsyncIterator[AsyncSession]:
    async for session in get_session():
        yield session


def parse_roles(header_value: str | None) -> frozenset[str]:
    return frozenset(role.strip() for role in (header_value or "").split(",") if role.strip())


def actor_roles(
    x_actor_roles: str | None = Header(
        default=None,
        alias="X-Actor-Roles",
        description="Temporary stand-in for the session until ticket 11 lands.",
    ),
) -> frozenset[str]:
    """Roles the caller claims.

    Placeholder for real authentication. Deny-by-default callers must still ask
    for a specific role, so forgetting to wire the session later fails closed.
    """
    return parse_roles(x_actor_roles)


def actor_employee_id(
    x_actor_employee: str | None = Header(
        default=None,
        alias="X-Actor-Employee",
        description="Temporary stand-in for the session until ticket 11 lands.",
    ),
) -> UUID | None:
    if not x_actor_employee:
        return None
    try:
        return UUID(x_actor_employee)
    except ValueError as exc:
        raise AppError(ErrorCode.INVALID_REQUEST, detail="X-Actor-Employee is not a UUID") from exc


def require_roles(*allowed: str):
    """Build a dependency that admits the given roles and refuses everyone else."""

    async def guard(roles: frozenset[str] = Depends(actor_roles)) -> frozenset[str]:
        if not roles & set(allowed):
            raise AppError(ErrorCode.FORBIDDEN, detail=f"roles={sorted(roles)}")
        return roles

    return guard


require_structure_role = require_roles(*STRUCTURE_ROLES)
require_employee_managing_role = require_roles(*EMPLOYEE_MANAGING_ROLES)


async def viewer_context(
    session: AsyncSession = Depends(db_session),
    roles: frozenset[str] = Depends(actor_roles),
    employee_id: UUID | None = Depends(actor_employee_id),
) -> ViewerContext:
    """Who is asking, with their departments expanded to include descendants.

    Anonymous requests get an empty context, which the visibility rules treat as
    the minimal contact list — never as privileged.
    """
    return await resolve_viewer_context(
        employee_id=employee_id,
        roles=roles,
        clearance_level="low",
        departments=PostgresDepartmentRepository(session),
        employee_repository=PostgresEmployeeRepository(session),
    )
