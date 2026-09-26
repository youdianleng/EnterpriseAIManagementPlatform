"""The notification centre.

Four endpoints, and every one of them answers about the caller alone. There is no
endpoint that takes somebody else's employee id, no administrator view of other
people's notifications, and no read-by-id: the recipient is the session's, so
"show me someone else's inbox" is not a request this surface can express.

**Which action guards this.** `notification.read_own` — one action for one
self-service surface, described in the catalogue as the whole signed-in
population reading its own centre. Not `employee.read_own`: that one is about a
personnel record, and this endpoint asks nothing about one.

**Reading somebody else's notification is a 403 with one code for both cases.**
`ERR_NTF_001` answers "no such notification" and "not yours" identically — same
status, same message key, same detail — so a caller cannot use the endpoint to
find out which notification ids exist. The refusal is recorded server-side
(`NotificationService._record_refusal`), which is where "who tried" belongs.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import current_principal, db_session, require
from app.domain.access import Action, Principal, ResourceKind
from app.domain.notification.models import Notification
from app.domain.notification.service import NotificationService
from app.repositories.notification import PostgresNotificationRepository

router = APIRouter(prefix="/notifications", tags=["notifications"])

#: Everyone, about themselves. The kind is the resource vocabulary's closest word
#: for "a person's own material" and is what the permission matrix names for this
#: action; the guard uses it only to label a refusal in the audit record.
read_own_notifications = require(Action.NOTIFICATION_READ_OWN, ResourceKind.EMPLOYEE)


class NotificationRead(BaseModel):
    """One notification, as the client renders it.

    `title_key` is the contract: the client resolves it in the reader's language,
    with `payload` supplying the fields. Nothing here is a sentence.

    `dedupe_key` and `recipient_employee_id` are deliberately absent. The first is
    the notifier's business — publishing it would invite a client to reason about
    idempotency it does not own — and the second is the caller, so it would be the
    same value on every row of every response.
    """

    id: UUID
    type: str
    title_key: str
    payload: dict[str, Any]
    entity_type: str
    entity_id: UUID
    read_at: datetime | None
    created_at: datetime
    expires_at: datetime | None


class NotificationPage(BaseModel):
    """A page of notifications, plus what it is a page of."""

    items: list[NotificationRead]
    total: int
    limit: int
    offset: int


class UnreadCountRead(BaseModel):
    """The badge number, counted over the rows the list would show."""

    unread: int


class ReadAllResult(BaseModel):
    """How many were unread and are not any more."""

    marked: int


def _service(session: AsyncSession) -> NotificationService:
    return NotificationService(PostgresNotificationRepository(session), session)


def _read(notification: Notification) -> NotificationRead:
    return NotificationRead(
        id=notification.id,
        type=str(notification.type),
        title_key=notification.title_key,
        payload=notification.payload,
        entity_type=notification.entity_type,
        entity_id=notification.entity_id,
        read_at=notification.read_at,
        created_at=notification.created_at,
        expires_at=notification.expires_at,
    )


@router.get(
    "",
    response_model=NotificationPage,
    summary="List your notifications, newest first",
    dependencies=[Depends(read_own_notifications)],
)
async def list_notifications(
    unread_only: bool = Query(default=False, description="Only what is still unread"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> NotificationPage:
    """The caller's own notifications.

    Expired ones are filtered out and stay in the table: `expires_at` decides what
    is worth showing, never what is kept.
    """
    page = await _service(session).list_for(
        principal.employee_id, unread_only=unread_only, limit=limit, offset=offset
    )
    return NotificationPage(
        items=[_read(item) for item in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get(
    "/unread-count",
    response_model=UnreadCountRead,
    summary="How many of your notifications are unread",
    dependencies=[Depends(read_own_notifications)],
)
async def read_unread_count(
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> UnreadCountRead:
    """The badge.

    Counted over the same rows the list shows — same recipient, same expiry rule —
    so the number and the list can never disagree about whether there is anything
    to read.
    """
    return UnreadCountRead(unread=await _service(session).unread_count(principal.employee_id))


@router.post(
    "/read-all",
    response_model=ReadAllResult,
    summary="Mark every unread notification read",
    dependencies=[Depends(read_own_notifications)],
)
async def mark_all_read(
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> ReadAllResult:
    """One click, and idempotent: a second call marks nothing and says so.

    Declared before `/{notification_id}/read` for readability only — the two paths
    cannot be confused for one another.
    """
    marked = await _service(session).mark_all_read(principal.employee_id)
    return ReadAllResult(marked=marked)


@router.post(
    "/{notification_id}/read",
    response_model=NotificationRead,
    summary="Mark one of your notifications read",
    dependencies=[Depends(read_own_notifications)],
)
async def mark_read(
    notification_id: UUID,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> NotificationRead:
    """Its own notification or nobody's — including the notifier's.

    Marking twice is not an error and does not move the timestamp: the first read
    is the one that answers "when did they see it".
    """
    notification = await _service(session).mark_read(
        notification_id, principal.employee_id
    )
    return _read(notification)
