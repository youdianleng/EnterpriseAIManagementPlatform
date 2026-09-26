"""The compliance read surface.

One endpoint, read-only, and only for the compliance role. Everything else about
the audit trail is a permission the database enforces (ticket 13): the runtime
role cannot update or delete a record at all, so there is deliberately no write
endpoint here to protect.
"""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import current_principal, db_session, require
from app.domain.access import Action, Principal, ResourceKind
from app.repositories.audit import AuditQuery, PostgresAuditRepository

router = APIRouter(prefix="/audit-log", tags=["compliance"])

#: Compliance only. An administrator configures the system and an HR reader sees
#: personnel files; neither is the independent reader the requirement is about,
#: and "the auditor is also the administrator" is the arrangement audit exists to
#: avoid.
read_audit_log = require(Action.AUDIT_READ, ResourceKind.AUDIT_LOG)


class AuditEntryRead(BaseModel):
    id: int
    occurred_at: datetime
    actor_user_id: UUID | None
    actor_roles: list[str]
    action: str
    entity_type: str
    entity_id: UUID | None
    before: dict | None
    after: dict | None
    reason: str | None
    request_id: str | None
    ip_address: str | None
    user_agent: str | None
    initiated_by: str


class AuditPage(BaseModel):
    """A page of records, plus what a caller needs to ask for the next one.

    The total is returned rather than a cursor: the table is append-only and
    ordered by time, so a count is cheap and a reader almost always wants to know
    how much there is before paging through it.
    """

    items: list[AuditEntryRead]
    total: int
    limit: int
    offset: int


@router.get(
    "",
    response_model=AuditPage,
    summary="Search the audit trail",
    dependencies=[Depends(read_audit_log)],
)
async def list_audit_log(
    occurred_from: datetime | None = Query(default=None, description="Inclusive lower bound"),
    occurred_to: datetime | None = Query(default=None, description="Exclusive upper bound"),
    actor_user_id: UUID | None = None,
    #: A plain string, not the enum. The search surface must be able to look for
    #: what is *in the table*: a value recorded by an older version of this
    #: application is still evidence, and refusing to search for it because today's
    #: catalogue no longer names it would hide exactly the records a review is
    #: looking for. An unrecognised value returns no rows.
    action: str | None = None,
    entity_type: str | None = None,
    entity_id: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> AuditPage:
    """Filtered, paginated, newest first.

    The filters are exactly the questions the requirement names — when, who, what
    kind of action, which entity — plus the two identifiers an incident review
    follows from a record it already has (`request_id` is on the row, not a
    filter, because a reader follows it by eye).
    """
    repository = PostgresAuditRepository(session)
    query = AuditQuery(
        occurred_from=occurred_from,
        occurred_to=occurred_to,
        actor_user_id=actor_user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        limit=limit,
        offset=offset,
    )
    entries = await repository.search(query)
    total = await repository.count(query)
    return AuditPage(
        items=[AuditEntryRead.model_validate(entry, from_attributes=True) for entry in entries],
        total=total,
        limit=limit,
        offset=offset,
    )
