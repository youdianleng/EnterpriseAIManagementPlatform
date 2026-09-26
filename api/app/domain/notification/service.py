"""The notification centre.

Four things it owns, and nothing else:

* **One notification per event per person.** The dedupe key is derived from the
  event (`models.NotificationDraft`) and enforced by a unique index, so a retry or
  a double submission lands on the same row. A suppression is *recorded* — see
  `_record_duplicate` — because "the notifier ran twice" and "the notifier never
  ran" otherwise look identical from the outside.
* **A delivery record per channel.** In-app is delivered the moment the row exists;
  email is queued as `pending` and belongs to whatever sends it (ticket 20). The
  centre tracks both from the first write, which is what makes "I never got the
  email" answerable later.
* **Read state, one row at a time or all of them.** Marking read is idempotent and
  keeps the *first* time it was read.
* **Whose notification it is.** Every read and write takes the recipient, and the
  refusal for anything else is one code for both "no such row" and "not yours"
  (`errors.NotificationErrorCode`).

It never renders anything: the row carries a bilingual key and structured fields,
and the client turns those into a sentence in the reader's language.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditAction, record
from app.domain.errors import DomainError
from app.domain.notification.errors import NotificationErrorCode
from app.domain.notification.models import (
    NOT_ATTEMPTED,
    DeliveryChannel,
    DeliveryPlan,
    DeliveryStatus,
    Notification,
    NotificationDraft,
    NotificationPage,
    RaiseOutcome,
)
from app.domain.notification.repository import NotificationRepository

#: Every notification is tracked on both channels from the moment it is raised.
#:
#: In-app is `sent` with one attempt: the row *is* the delivery, and it is readable
#: as soon as the transaction commits. Email is `pending` with no attempts and the
#: reason it has none, because nothing sends mail yet — recording it as sent would
#: be a lie the first person to ask "did the email go out" would believe, and
#: leaving the reason empty would read as an attempt that failed silently.
DELIVERY_PLAN: tuple[DeliveryPlan, ...] = (
    DeliveryPlan(channel=DeliveryChannel.IN_APP, status=DeliveryStatus.SENT, attempts=1),
    DeliveryPlan(
        channel=DeliveryChannel.EMAIL, status=DeliveryStatus.PENDING, error=NOT_ATTEMPTED
    ),
)

#: Deliberately free of both ids. The refusal has to be the same bytes whether the
#: row exists and belongs to somebody else or does not exist at all.
REFUSAL_DETAIL = "no notification with that id is addressed to this employee"


class NotificationService:
    """Raising notifications, and reading one person's centre."""

    def __init__(self, repository: NotificationRepository, session: AsyncSession) -> None:
        self._repository = repository
        # Held for the audit records, so a suppression and the notification it
        # declined to write are one transaction.
        self._session = session

    # --- raising -----------------------------------------------------------

    async def notify(self, draft: NotificationDraft) -> RaiseOutcome:
        """Raise one notification, or report that this event already has one.

        A duplicate is not an error: the caller asked for the event to be
        represented once, and it is. It is reported rather than swallowed so the
        caller can tell the two apart without a second query.
        """
        notification_id = await self._repository.insert(draft)
        if notification_id is None:
            await self._record_duplicate(draft)
            await self._repository.commit()
            return RaiseOutcome(notification_id=None, duplicate=True)

        await self._repository.add_deliveries(notification_id, DELIVERY_PLAN)
        await self._repository.commit()
        return RaiseOutcome(notification_id=notification_id, duplicate=False)

    async def _record_duplicate(self, draft: NotificationDraft) -> None:
        """Write down that a second attempt was suppressed.

        Keyed on the entity the notification is about, like every other record
        about it, so "everything that happened to this request" is one filter. The
        dedupe key is in the body: it is what says *which* event was suppressed.
        """
        await record(
            self._session,
            action=AuditAction.NOTIFICATION_DUPLICATE_SUPPRESSED,
            entity_type=draft.entity_type,
            entity_id=draft.entity_id,
            after={
                "notification_type": str(draft.type),
                "recipient_employee_id": str(draft.recipient_employee_id),
                "dedupe_key": draft.dedupe_key,
            },
        )

    # --- reading -----------------------------------------------------------

    async def list_for(
        self,
        recipient_employee_id: UUID,
        *,
        unread_only: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> NotificationPage:
        """One page of somebody's notifications, newest first.

        Expired rows are excluded and stay in the table: `expires_at` is a read
        filter, not a deletion, so the answer to "was this person told" survives
        the notification no longer being worth showing.
        """
        items = await self._repository.list_for(
            recipient_employee_id, unread_only=unread_only, limit=limit, offset=offset
        )
        total = await self._repository.count_for(
            recipient_employee_id, unread_only=unread_only
        )
        return NotificationPage(items=items, total=total, limit=limit, offset=offset)

    async def unread_count(self, recipient_employee_id: UUID) -> int:
        """The badge number, counted over the same rows the list shows."""
        return await self._repository.unread_count(recipient_employee_id)

    # --- marking -----------------------------------------------------------

    async def mark_read(
        self, notification_id: UUID, recipient_employee_id: UUID
    ) -> Notification:
        """Mark one notification read, if it is the caller's.

        Idempotent: marking an already-read notification returns it unchanged
        rather than refusing, because a second click is not a mistake worth an
        error. The timestamp keeps the *first* read, which is the one a reader
        means by "when did they see it".
        """
        notification = await self._repository.mark_read(
            notification_id, recipient_employee_id
        )
        if notification is None:
            await self._record_refusal(notification_id)
            raise DomainError(
                NotificationErrorCode.NOTIFICATION_NOT_YOURS, detail=REFUSAL_DETAIL
            )

        await self._repository.commit()
        return notification

    async def mark_all_read(self, recipient_employee_id: UUID) -> int:
        """Mark everything of the caller's unread as read. Returns how many."""
        marked = await self._repository.mark_all_read(recipient_employee_id)
        await self._repository.commit()
        return marked

    async def _record_refusal(self, notification_id: UUID) -> None:
        """Record the attempt, then refuse it.

        Committed on its own, because the request is about to fail: a record that
        rolled back with it would leave "somebody tried to mark another person's
        notification" invisible, which is the half of the trail an incident review
        actually needs. The record is server-side; the response it accompanies says
        nothing about whether the row exists.
        """
        await record(
            self._session,
            action=AuditAction.ACCESS_REFUSED,
            entity_type="notification",
            entity_id=notification_id,
            after={
                "operation": "notification.mark_read",
                "reason": "not_the_recipient",
            },
        )
        await self._repository.commit()


__all__ = ["DELIVERY_PLAN", "REFUSAL_DETAIL", "NotificationService"]
