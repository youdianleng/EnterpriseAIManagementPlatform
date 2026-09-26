"""The role catalogue, published.

`GET /roles` answers "what may this role do" from the tables migration 0008
maintains, which are rewritten from `domain/access/permissions.py` at application
start. Granting a role to an account lives in `accounts.py`, next to the rest of
the account operations.

The endpoint reads the *tables* rather than the code, because that is the point of
publishing them: a consumer that read the code would make the tables decorative.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import db_session, require
from app.core.errors import AppError, ErrorCode
from app.domain.access import Action, Principal, ResourceKind
from app.domain.access.catalogue import published_catalogue

router = APIRouter(tags=["roles"])

#: Readable by anyone signed in. Knowing what the system allows is not a secret,
#: and a user asking "why can I not do this" deserves the real answer.
read_roles = require(Action.ROLE_READ, ResourceKind.ACCOUNT)


class RoleRead(BaseModel):
    name: str
    description: str
    #: True when a managerial position confers this role without a grant.
    is_derived: bool
    actions: list[str]


class RoleList(BaseModel):
    items: list[RoleRead]


@router.get("/roles", response_model=RoleList, summary="What each role may do")
async def list_roles(
    principal: Principal = Depends(read_roles),
    session: AsyncSession = Depends(db_session),
) -> RoleList:
    catalogue = await published_catalogue(session)
    if not catalogue:
        # The tables are rewritten at application start. An empty answer would
        # read as "no roles exist", which is both false and alarming.
        raise AppError(
            ErrorCode.NOT_FOUND,
            detail="the role catalogue has not been published; has the application started?",
        )
    return RoleList(items=[RoleRead(**entry) for entry in catalogue.values()])
