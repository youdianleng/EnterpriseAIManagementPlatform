"""PostgreSQL implementation of the notification repository.

Three implementation notes, all of them about the same question — what happens on
the second write:

* **The insert is the idempotency mechanism.** `ON CONFLICT DO NOTHING` against the
  `(recipient, dedupe_key)` unique index, with `RETURNING id`: a row back means this
  raise created the notification, no row means this event already has one. An
  application-side "SELECT then INSERT" would be a race, and the duplicate it let
  through would be discovered by a user seeing the same notification twice.
* **`mark_read` is one statement with both conditions.** `WHERE id = :id AND
  recipient_employee_id = :me` means the database decides, once, and the caller gets
  the same empty answer for "no such notification" and "not yours". Reading first
  to decide which refusal to raise would put the existence of the row on the wire.
* **`read_at` keeps its first value.** `COALESCE(read_at, now())` makes marking read
  idempotent without rewriting when somebody first saw it, which is the part an
  incident review would be reading.
"""

from uuid import UUID

from sqlalchemy import Select, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.notification.models import (
    DeliveryPlan,
    DeliveryStatus,
    Notification,
    NotificationDraft,
    NotificationType,
)
from app.models.notification import Notification as NotificationRow
from app.models.notification import NotificationDelivery as DeliveryRow


def _to_notification(row) -> Notification:  # noqa: ANN001 - ORM row or Core row
    return Notification(
        id=row.id,
        recipient_employee_id=row.recipient_employee_id,
        type=NotificationType(row.type),
        title_key=row.title_key,
        payload=row.payload,
        entity_type=row.entity_type,
        entity_id=row.entity_id,
        dedupe_key=row.dedupe_key,
        read_at=row.read_at,
        created_at=row.created_at,
        expires_at=row.expires_at,
    )


def _live() -> object:
    """Not expired. `expires_at IS NULL` means it never expires, not that it has."""
    return or_(NotificationRow.expires_at.is_(None), NotificationRow.expires_at > func.now())


class PostgresNotificationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- writes ------------------------------------------------------------

    async def insert(self, draft: NotificationDraft) -> UUID | None:
        statement = (
            pg_insert(NotificationRow)
            .values(
                recipient_employee_id=draft.recipient_employee_id,
                type=str(draft.type),
                title_key=draft.title_key,
                payload=draft.payload,
                entity_type=draft.entity_type,
                entity_id=draft.entity_id,
                dedupe_key=draft.dedupe_key,
                expires_at=draft.expires_at,
            )
            .on_conflict_do_nothing(index_elements=["recipient_employee_id", "dedupe_key"])
            .returning(NotificationRow.id)
        )
        return await self._session.scalar(statement)

    async def add_deliveries(
        self, notification_id: UUID, plans: tuple[DeliveryPlan, ...]
    ) -> None:
        for plan in plans:
            # A successful delivery happened *now*, on the database's clock, so the
            # row cannot claim a time the notification itself does not agree with.
            sent_at = plan.sent_at or (
                func.now() if plan.status is DeliveryStatus.SENT else None
            )
            self._session.add(
                DeliveryRow(
                    notification_id=notification_id,
                    channel=plan.channel.value,
                    status=plan.status.value,
                    attempts=plan.attempts,
                    error=plan.error,
                    sent_at=sent_at,
                )
            )
        await self._session.flush()

    async def mark_read(
        self, notification_id: UUID, recipient_employee_id: UUID
    ) -> Notification | None:
        statement = (
            update(NotificationRow)
            .where(
                NotificationRow.id == notification_id,
                NotificationRow.recipient_employee_id == recipient_employee_id,
            )
            .values(read_at=func.coalesce(NotificationRow.read_at, func.now()))
            .returning(*NotificationRow.__table__.c)
            .execution_options(synchronize_session=False)
        )
        row = (await self._session.execute(statement)).first()
        return _to_notification(row) if row is not None else None

    async def mark_all_read(self, recipient_employee_id: UUID) -> int:
        statement = (
            update(NotificationRow)
            .where(
                NotificationRow.recipient_employee_id == recipient_employee_id,
                NotificationRow.read_at.is_(None),
            )
            .values(read_at=func.now())
            .execution_options(synchronize_session=False)
        )
        result = await self._session.execute(statement)
        return int(result.rowcount or 0)

    async def commit(self) -> None:
        await self._session.commit()

    # --- reads -------------------------------------------------------------

    async def list_for(
        self,
        recipient_employee_id: UUID,
        *,
        unread_only: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Notification]:
        statement = self._list_statement(recipient_employee_id, unread_only=unread_only)
        # The id breaks ties: two notifications raised in one transaction share a
        # timestamp, and a page that can be ordered two ways can be paged twice.
        statement = (
            statement.order_by(NotificationRow.created_at.desc(), NotificationRow.id.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = (await self._session.execute(statement)).scalars()
        return [_to_notification(row) for row in rows]

    async def count_for(
        self, recipient_employee_id: UUID, *, unread_only: bool = False
    ) -> int:
        statement = select(func.count()).select_from(
            self._list_statement(recipient_employee_id, unread_only=unread_only).subquery()
        )
        return int(await self._session.scalar(statement) or 0)

    async def unread_count(self, recipient_employee_id: UUID) -> int:
        statement = select(func.count()).select_from(NotificationRow).where(
            NotificationRow.recipient_employee_id == recipient_employee_id,
            NotificationRow.read_at.is_(None),
            _live(),
        )
        return int(await self._session.scalar(statement) or 0)

    def _list_statement(
        self, recipient_employee_id: UUID, *, unread_only: bool
    ) -> Select:
        conditions = [
            NotificationRow.recipient_employee_id == recipient_employee_id,
            _live(),
        ]
        if unread_only:
            conditions.append(NotificationRow.read_at.is_(None))
        return select(NotificationRow).where(*conditions)


__all__ = ["PostgresNotificationRepository"]
