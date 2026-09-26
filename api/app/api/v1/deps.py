"""Request-scoped dependencies shared by the v1 routers."""

from collections.abc import AsyncIterator

from fastapi import Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ErrorCode
from app.db import get_session

# Roles permitted to change the organisation structure. The full role model and
# the `can()` kernel arrive in tickets 08 and 11; until then this is the single
# place the rule is expressed, so the routers do not each invent one.
STRUCTURE_ROLES = {"admin", "hr"}


async def db_session() -> AsyncIterator[AsyncSession]:
    async for session in get_session():
        yield session


async def require_structure_role(
    x_actor_roles: str | None = Header(
        default=None,
        alias="X-Actor-Roles",
        description="Temporary stand-in for the session until ticket 11 lands.",
    ),
) -> set[str]:
    """Gate structural changes on a role.

    Placeholder for real authentication: callers declare their roles in a header.
    It is deliberately deny-by-default, so forgetting to wire the real session
    later fails closed rather than opening the endpoint.
    """
    roles = {role.strip() for role in (x_actor_roles or "").split(",") if role.strip()}
    if not roles & STRUCTURE_ROLES:
        raise AppError(ErrorCode.FORBIDDEN, detail=f"roles={sorted(roles)}")
    return roles
