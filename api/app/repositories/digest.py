"""PostgreSQL implementation of the digest repository.

Four reads and four writes, and three of them are worth reading before the SQL:

* **The route is written once more in SQL** — `COALESCE(override, position's
  manager, department's manager)` over the active assignments of the person the
  anomaly belongs to. It is the rule `attendance/notify.py` applies to a single
  punch notification, asked the other way round (one query for a whole morning's
  mail rather than one per person); `terminated_approvers` is the precedent, and
  the `LATERAL` with `LIMIT 1` orders assignments the way that query orders them so
  that "the primary one" means the same row everywhere.
* **`record_sent` and `record_failure` are one upsert against
  `uq_daily_digests_recipient_date`.** A first attempt, a retry that failed again
  and a retry that finally went out are the same statement, and the unique index is
  what makes a second run of a day collide with the first rather than mail twice.
  `sent_at` comes from the database's clock, so it cannot disagree with the
  delivery rows written in the same transaction.
* **The delivery writes never un-send.** `delivered` and `failed` both exclude rows
  that are already `sent`: the queue is drained once, and a later pass that somehow
  reached the same row must not be able to take the delivery back.
"""

from collections.abc import Sequence
from datetime import date
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Select, func, select, true, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.domain.attendance.anomalies import AnomalyType
from app.domain.notification.digest_repository import (
    DayAnomaly,
    DigestRecipient,
    DigestRecord,
    QueuedDelivery,
)
from app.domain.notification.models import (
    DIGEST_CANDIDATE_TYPES,
    DeliveryChannel,
    DeliveryStatus,
)
from app.models.account import User as UserRow
from app.models.attendance import AttendanceAnomaly as AnomalyRow
from app.models.employee import Employee as EmployeeRow
from app.models.employee import EmployeeAssignment as AssignmentRow
from app.models.notification import DailyDigest as DigestRow
from app.models.notification import Notification as NotificationRow
from app.models.notification import NotificationDelivery as DeliveryRow
from app.models.org import Department as DepartmentRow

#: Who a person's day is reported to, in the order the route decides it. A module
#: constant so the lateral below reads as the rule rather than as three nested calls.
ROUTE = func.coalesce(
    AssignmentRow.notification_override_employee_id,
    AssignmentRow.manager_employee_id,
    DepartmentRow.manager_employee_id,
)


class PostgresDigestRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- reads -------------------------------------------------------------

    async def day_anomalies(self, digest_date: date) -> list[DayAnomaly]:
        route = _route()
        recipient = aliased(EmployeeRow, name="digest_recipient")
        account = aliased(UserRow, name="digest_recipient_account")
        statement = (
            select(
                AnomalyRow.id,
                AnomalyRow.employee_id,
                EmployeeRow.first_name,
                EmployeeRow.last_name,
                AnomalyRow.type,
                AnomalyRow.business_date,
                route.c.recipient_employee_id,
                recipient.email,
                account.locale,
            )
            # Explicit: the lateral introduces a second FROM, so SQLAlchemy cannot
            # tell which one the employee join starts from.
            .select_from(AnomalyRow)
            .join(EmployeeRow, EmployeeRow.id == AnomalyRow.employee_id)
            .join(route, true())
            .outerjoin(recipient, recipient.id == route.c.recipient_employee_id)
            .outerjoin(account, account.employee_id == route.c.recipient_employee_id)
            .where(
                AnomalyRow.business_date == digest_date,
                # Still standing: a mail listing a punch somebody has already made
                # up sends its reader to look at nothing.
                AnomalyRow.resolved_by_event_id.is_(None),
            )
            .order_by(EmployeeRow.last_name, EmployeeRow.first_name, AnomalyRow.type)
        )
        return [
            DayAnomaly(
                anomaly_id=row[0],
                employee_id=row[1],
                employee_name=_full_name(row[2], row[3]),
                type=AnomalyType(row[4]),
                business_date=row[5],
                recipient=_recipient(row[6], row[7], row[8]),
            )
            for row in (await self._session.execute(statement)).all()
        ]

    async def queued_deliveries(self, digest_date: date) -> list[QueuedDelivery]:
        recipient = aliased(EmployeeRow, name="queued_recipient")
        account = aliased(UserRow, name="queued_recipient_account")
        statement = (
            select(
                DeliveryRow.id,
                NotificationRow.id,
                NotificationRow.recipient_employee_id,
                # The reminder's payload names the anomaly, not the employee, so
                # the person it is about is the person it is addressed to; the
                # clock-out notification names them explicitly.
                NotificationRow.payload["employee_id"].astext,
                recipient.email,
                account.locale,
            )
            .select_from(DeliveryRow)
            .join(NotificationRow, NotificationRow.id == DeliveryRow.notification_id)
            .join(recipient, recipient.id == NotificationRow.recipient_employee_id)
            .outerjoin(account, account.employee_id == NotificationRow.recipient_employee_id)
            .where(
                DeliveryRow.channel == DeliveryChannel.EMAIL.value,
                # Not `= 'pending'`: a row whose digest failed is still owed a mail,
                # and whether it is attempted again is the digest row's decision.
                DeliveryRow.status != DeliveryStatus.SENT.value,
                NotificationRow.type.in_([str(kind) for kind in DIGEST_CANDIDATE_TYPES]),
                # Both candidate types carry the day they are about, written by
                # `notify.py` as an ISO date: the payload is structure, and comparing
                # it as text means no cast can fail on a row somebody typed.
                NotificationRow.payload["business_date"].astext == digest_date.isoformat(),
            )
            .order_by(NotificationRow.recipient_employee_id, DeliveryRow.id)
        )
        queued: list[QueuedDelivery] = []
        for row in (await self._session.execute(statement)).all():
            addressed_to = _recipient(row[2], row[4], row[5])
            if addressed_to is None:  # pragma: no cover - the join guarantees it
                continue
            queued.append(
                QueuedDelivery(
                    delivery_id=row[0],
                    notification_id=row[1],
                    recipient=addressed_to,
                    subject_employee_id=UUID(row[3]) if row[3] else row[2],
                )
            )
        return queued

    async def digests_on(self, digest_date: date) -> list[DigestRecord]:
        rows = (
            await self._session.execute(
                select(
                    DigestRow.recipient_employee_id,
                    DigestRow.attempts,
                    DigestRow.sent_at,
                    DigestRow.error,
                )
                .where(DigestRow.digest_date == digest_date)
                .order_by(DigestRow.recipient_employee_id)
            )
        ).all()
        return [
            DigestRecord(
                recipient_employee_id=row[0], attempts=row[1], sent_at=row[2], error=row[3]
            )
            for row in rows
        ]

    # --- writes ------------------------------------------------------------

    async def forget_attempts(self, digest_date: date) -> int:
        result = await self._session.execute(
            update(DigestRow)
            .where(DigestRow.digest_date == digest_date, DigestRow.sent_at.is_(None))
            .values(attempts=0, error=None)
            .execution_options(synchronize_session=False)
        )
        return int(result.rowcount or 0)

    async def record_sent(
        self,
        recipient_employee_id: UUID,
        digest_date: date,
        *,
        payload: dict[str, Any],
        anomaly_count: int,
    ) -> None:
        statement = _upsert(
            recipient_employee_id, digest_date, payload, anomaly_count, sent_at=func.now()
        )
        await self._session.execute(
            statement.on_conflict_do_update(
                constraint="uq_daily_digests_recipient_date",
                set_={
                    "payload": payload,
                    "anomaly_count": anomaly_count,
                    "attempts": DigestRow.attempts + 1,
                    "sent_at": func.now(),
                    "error": None,
                },
            )
        )

    async def record_failure(
        self,
        recipient_employee_id: UUID,
        digest_date: date,
        *,
        payload: dict[str, Any],
        anomaly_count: int,
        error: str,
    ) -> int:
        statement = _upsert(
            recipient_employee_id, digest_date, payload, anomaly_count, error=error
        )
        # `sent_at` is absent from the update on purpose: an attempt that failed
        # must not clear the timestamp of one that succeeded.
        attempts = await self._session.scalar(
            statement.on_conflict_do_update(
                constraint="uq_daily_digests_recipient_date",
                set_={
                    "payload": payload,
                    "anomaly_count": anomaly_count,
                    "attempts": DigestRow.attempts + 1,
                    "error": error,
                },
            ).returning(DigestRow.attempts)
        )
        return int(attempts or 0)

    async def delivered(self, delivery_ids: Sequence[UUID]) -> int:
        return await self._move(
            delivery_ids,
            values={
                "status": DeliveryStatus.SENT.value,
                "error": None,
                "sent_at": func.now(),
            },
        )

    async def failed(self, delivery_ids: Sequence[UUID], error: str) -> int:
        return await self._move(
            delivery_ids,
            values={"status": DeliveryStatus.FAILED.value, "error": error},
        )

    async def held(self, delivery_ids: Sequence[UUID], reason: str) -> int:
        """Rewrite the reason only: same status, same attempt count."""
        if not delivery_ids:
            return 0
        result = await self._session.execute(
            update(DeliveryRow)
            .where(
                DeliveryRow.id.in_(list(delivery_ids)),
                DeliveryRow.status == DeliveryStatus.PENDING.value,
            )
            .values(error=reason)
            .execution_options(synchronize_session=False)
        )
        return int(result.rowcount or 0)

    async def commit(self) -> None:
        await self._session.commit()

    # --- internals ---------------------------------------------------------

    async def _move(self, delivery_ids: Sequence[UUID], *, values: dict[str, Any]) -> int:
        if not delivery_ids:
            return 0
        result = await self._session.execute(
            update(DeliveryRow)
            .where(
                DeliveryRow.id.in_(list(delivery_ids)),
                # Never un-send: a row that went out stays sent whatever a later
                # pass believes about the day.
                DeliveryRow.status != DeliveryStatus.SENT.value,
            )
            .values(attempts=DeliveryRow.attempts + 1, **values)
            .execution_options(synchronize_session=False)
        )
        return int(result.rowcount or 0)


def _route() -> Select:
    """One person's route, as a lateral subquery: the recipient, or no row.

    The organisation *as it stands*, which is what the approval engine and the
    notifier both resolve against — a route is not a fact about a date.
    """
    return (
        select(ROUTE.label("recipient_employee_id"))
        .select_from(AssignmentRow)
        .outerjoin(DepartmentRow, DepartmentRow.id == AssignmentRow.department_id)
        .where(
            AssignmentRow.employee_id == AnomalyRow.employee_id,
            AssignmentRow.end_date.is_(None),
        )
        .order_by(AssignmentRow.is_primary.desc(), AssignmentRow.start_date)
        .limit(1)
        .lateral("digest_route")
    )


def _upsert(
    recipient_employee_id: UUID,
    digest_date: date,
    payload: dict[str, Any],
    anomaly_count: int,
    *,
    sent_at: Any = None,
    error: str | None = None,
):
    """The insert half of both outcomes: a new row, on its first attempt.

    The outcome fields are written here as well as in the conflict branch, because
    a first attempt *is* an insert: a row created by `record_sent` with a NULL
    `sent_at` would be a digest that went out and claims it did not, which is
    exactly the state the next run would mail again.
    """
    return pg_insert(DigestRow).values(
        id=uuid4(),
        recipient_employee_id=recipient_employee_id,
        digest_date=digest_date,
        payload=payload,
        anomaly_count=anomaly_count,
        attempts=1,
        sent_at=sent_at,
        error=error,
    )


def _full_name(first: str, last: str) -> str:
    return f"{first} {last}".strip()


def _recipient(
    employee_id: UUID | None, email: str | None, locale: str | None
) -> DigestRecipient | None:
    """A recipient, or None when the route named nobody — reported, never invented."""
    if employee_id is None or email is None:
        return None
    return DigestRecipient(employee_id=employee_id, email=email, locale=locale)


__all__ = ["PostgresDigestRepository"]
