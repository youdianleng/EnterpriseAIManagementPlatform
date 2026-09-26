"""Persistence contract for notifications.

Three things about this interface are load-bearing, and all three are about a
query somebody could write by accident:

* **Every read takes the recipient.** There is no `get(notification_id)` and no
  `list_all()`. A method that could return somebody else's notification does not
  exist, so "the filter was forgotten" is not a mistake this module can express —
  the same reasoning the authorisation kernel's `FilterSpec` documents.
* **`mark_read` answers None for both "no such row" and "not yours".** One
  statement, one outcome: the caller cannot tell the two apart because this layer
  never learns which one happened. That is what makes the 403 the API returns
  identical in both cases.
* **No clock is passed in.** "Expired" and "read at" are decided by the database's
  own `now()`, the same clock that stamps `created_at`. A caller's clock would make
  the list and the badge disagree about a row near its expiry, which is a bug
  nobody would think to look for.

`insert` is the idempotency point: it returns the new id, or None when the
recipient's dedupe key was already used.
"""

from typing import Protocol
from uuid import UUID

from app.domain.notification.models import DeliveryPlan, Notification, NotificationDraft


class NotificationRepository(Protocol):
    async def insert(self, draft: NotificationDraft) -> UUID | None: ...

    async def add_deliveries(
        self, notification_id: UUID, plans: tuple[DeliveryPlan, ...]
    ) -> None: ...

    async def list_for(
        self,
        recipient_employee_id: UUID,
        *,
        unread_only: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Notification]: ...

    async def count_for(
        self, recipient_employee_id: UUID, *, unread_only: bool = False
    ) -> int: ...

    async def unread_count(self, recipient_employee_id: UUID) -> int: ...

    async def mark_read(
        self, notification_id: UUID, recipient_employee_id: UUID
    ) -> Notification | None: ...

    async def mark_all_read(self, recipient_employee_id: UUID) -> int: ...

    async def commit(self) -> None: ...


__all__ = ["NotificationRepository"]
