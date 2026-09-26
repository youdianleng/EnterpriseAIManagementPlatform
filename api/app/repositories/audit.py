"""Reading audit records.

Read-only by construction: there is no `save`, no `delete`, and no `update` here,
and the application's database role could not use them anyway (ticket 13 revokes
UPDATE and DELETE on the table). A repository that cannot express a mutation is a
smaller thing to trust than one that merely does not perform any.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog


@dataclass(slots=True)
class AuditQuery:
    """What a compliance reader asked for.

    A value object rather than a pile of keyword arguments, so the same filters
    can be applied to the page and to its count without being restated — a count
    that disagrees with the page it describes is worse than no count.
    """

    occurred_from: datetime | None = None
    occurred_to: datetime | None = None
    actor_user_id: UUID | None = None
    action: str | None = None
    entity_type: str | None = None
    entity_id: UUID | None = None
    limit: int = 50
    offset: int = 0
    #: Newest first, which is the order an incident review reads in.
    newest_first: bool = True

    def conditions(self) -> list:
        clauses = []
        if self.occurred_from is not None:
            clauses.append(AuditLog.occurred_at >= self.occurred_from)
        if self.occurred_to is not None:
            # Exclusive: a range given as two dates then means whole days, which
            # is what a reader means by "from the 1st to the 2nd".
            clauses.append(AuditLog.occurred_at < self.occurred_to)
        if self.actor_user_id is not None:
            clauses.append(AuditLog.actor_user_id == self.actor_user_id)
        if self.action is not None:
            clauses.append(AuditLog.action == self.action)
        if self.entity_type is not None:
            clauses.append(AuditLog.entity_type == self.entity_type)
        if self.entity_id is not None:
            clauses.append(AuditLog.entity_id == self.entity_id)
        return clauses


class PostgresAuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _statement(self, query: AuditQuery) -> Select:
        statement = select(AuditLog)
        for clause in query.conditions():
            statement = statement.where(clause)
        order = AuditLog.occurred_at.desc() if query.newest_first else AuditLog.occurred_at
        # The id breaks ties: two records can share a timestamp, and a page that
        # can be ordered two ways can be paged twice or skipped once.
        return statement.order_by(order, AuditLog.id.desc() if query.newest_first else AuditLog.id)

    async def search(self, query: AuditQuery) -> list[AuditLog]:
        statement = self._statement(query).limit(query.limit).offset(query.offset)
        return list((await self._session.execute(statement)).scalars())

    async def count(self, query: AuditQuery) -> int:
        statement = select(func.count()).select_from(AuditLog)
        for clause in query.conditions():
            statement = statement.where(clause)
        return int(await self._session.scalar(statement) or 0)


__all__ = ["AuditQuery", "PostgresAuditRepository"]
