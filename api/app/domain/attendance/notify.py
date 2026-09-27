"""The attendance events, turned into notifications — without touching the streams.

`AttendanceService` and `AnomalyService` are not modified and do not know this
module exists. This is a decorator over the first and a companion to the second:

* `AttendanceNotifier` is the same four operations, and the one of them that
  deserves a notification raises it. A caller constructs one object where it used to
  construct the service, which is what makes the notification hard to forget — an
  "and then call the notifier" step after every punch is a step somebody eventually
  omits (the same argument `notification/approval.py` makes).
* `AnomalyReminder` is the morning half of ticket 23: it takes the anomalies that
  are still standing and still untold and tells each employee about their own.

**Who hears about a clock-out, in one rule.** The manager of the employee's
*primary position*, or the position's `notification_override_employee_id` when one
is set — that field exists for exactly this, and it wins because somebody typed it
for this person rather than inheriting it from the org chart. The department's
manager is the last fallback, the same one the approval route uses: a position that
names nobody still belongs to a department somebody is accountable for. Nobody at
all is not an error and not a reason to invent a recipient — the punch is the
record, this is a courtesy on top of it — so it is logged and skipped.

**Nobody is notified about their own punch.** A manager clocking out is their own
primary position's manager often enough to matter, and an override naming the
employee is a data mistake: either way the row would be a person being told what
they just did. Suppressed with a log line rather than written and filtered later.

**The reminder is one notification per anomaly, not one per day.** The anomaly row
is the entity: it is what the dedupe key names, it is what `notified_at` is stamped
on, and the two kinds a day usually has — a missing clock_in and a missing clock_out
— are fixed by two different corrections. The client groups them by date for the
reader, and the digest's personal section does the same.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Protocol
from uuid import UUID

from app.domain.attendance.anomalies import (
    Anomaly,
    AnomalyFailure,
    AnomalyReminderReport,
    failure_of,
)
from app.domain.attendance.anomaly_service import AnomalyService
from app.domain.attendance.models import (
    AttendanceEvent,
    DayRecord,
    EventSource,
    EventType,
)
from app.domain.attendance.service import AttendanceService
from app.domain.notification.models import (
    NotificationDraft,
    NotificationType,
    RaiseOutcome,
)
from app.domain.notification.service import NotificationService
from app.logging import get_logger

logger = get_logger(__name__)

#: The entity a notification is about, in the vocabulary the trail and the client
#: read. One string per kind of row, so a filter on `attendance_event` is
#: "everything anybody was told about a punch".
EVENT_ENTITY = "attendance_event"
ANOMALY_ENTITY = "attendance_anomaly"

#: What makes one clock-out notification distinct from the next for one recipient.
#: Constant, because the event id is already in the entity: a replay of the same
#: punch lands on the same dedupe key and is suppressed, and the next punch of the
#: day is a different row and a new notification.
CLOCK_OUT_EVENT = "clock_out"
REMINDER_EVENT = "reminder"


@dataclass(slots=True, frozen=True)
class NotificationRoute:
    """Who a person's punches are reported to, as the employee module stores it.

    Three primitives rather than a resolved recipient, so the *rule* — override,
    then the position's manager, then the department's — stays in `domain/` where
    rules live, and the query stays in the repository where rows live.
    """

    department_id: UUID
    manager_employee_id: UUID | None = None
    notification_override_employee_id: UUID | None = None


class NotificationRouteSource(Protocol):
    """What the notifier asks the employee module, and nothing else.

    A Protocol rather than the concrete repository, for the reason
    `models.ExpectationSource` gives: the dependency is stated as the two questions
    this module has, not as a whole persistence surface.
    """

    async def notification_route(self, employee_id: UUID) -> NotificationRoute | None:
        """The active primary assignment's contacts, or None when there is none."""
        ...

    async def department_manager(self, department_id: UUID) -> UUID | None:
        """The fallback: somebody accountable for the department."""
        ...


class AttendanceNotifier:
    """`AttendanceService`'s four operations, with the clock-out notification."""

    def __init__(
        self,
        service: AttendanceService,
        notifications: NotificationService,
        routes: NotificationRouteSource,
    ) -> None:
        self._service = service
        self._notifications = notifications
        self._routes = routes

    # --- the service's operations, one of them notifying ---------------------

    async def clock(
        self,
        employee_id: UUID,
        kind: EventType | str,
        at: datetime,
        source: EventSource | str,
        *,
        ip_address: str | None = None,
        created_by_employee_id: UUID | None = None,
    ) -> AttendanceEvent:
        """Append the punch, and tell the manager when it closes the day.

        The event the service returns decides, not the `kind` the caller passed: a
        replayed request comes back as the row that already existed, and that row's
        type is what happened. A replay raises the same draft again and the dedupe
        key suppresses it, which is what makes a network retry cost nothing.
        """
        event = await self._service.clock(
            employee_id,
            kind,
            at,
            source,
            ip_address=ip_address,
            created_by_employee_id=created_by_employee_id,
        )
        if event.event_type is EventType.CLOCK_OUT:
            await self.clocked_out(event)
        return event

    async def day_view(self, employee_id: UUID, business_date: date) -> DayRecord:
        """Reading a day raises nothing. Passed through unchanged."""
        return await self._service.day_view(employee_id, business_date)

    async def recompute_day(self, employee_id: UUID, business_date: date) -> DayRecord:
        """Rebuilding a day raises nothing either: it is not an event."""
        return await self._service.recompute_day(employee_id, business_date)

    async def range_view(
        self, employee_id: UUID, from_date: date, to_date: date
    ) -> list[DayRecord]:
        return await self._service.range_view(employee_id, from_date, to_date)

    # --- the event ----------------------------------------------------------

    async def clocked_out(self, event: AttendanceEvent) -> RaiseOutcome | None:
        """Tell whoever the rule names about one finished day.

        Public because a caller that already has the event — an import, or a test
        that appended a row itself — should be able to raise the same
        notification without punching again. `None` means there was nobody to tell.
        """
        recipient = await self._recipient_for(event.employee_id)
        if recipient is None:
            return None
        return await self._notifications.notify(
            NotificationDraft(
                recipient_employee_id=recipient,
                type=NotificationType.ATTENDANCE_CLOCK_OUT,
                payload={
                    "employee_id": str(event.employee_id),
                    "event_id": str(event.id),
                    "business_date": event.business_date.isoformat(),
                    "occurred_at": event.occurred_at.astimezone(UTC).isoformat(),
                },
                entity_type=EVENT_ENTITY,
                entity_id=event.id,
                event=CLOCK_OUT_EVENT,
            )
        )

    async def _recipient_for(self, employee_id: UUID) -> UUID | None:
        route = await self._routes.notification_route(employee_id)
        if route is None:
            _skipped(employee_id, "no_primary_position")
            return None

        recipient = (
            route.notification_override_employee_id
            or route.manager_employee_id
            or await self._routes.department_manager(route.department_id)
        )
        if recipient is None:
            _skipped(employee_id, "no_manager_configured")
            return None
        if recipient == employee_id:
            _skipped(employee_id, "recipient_is_the_employee")
            return None
        return recipient


class AnomalyReminder:
    """The morning pass: each employee hears about their own outstanding punches."""

    def __init__(
        self, anomalies: AnomalyService, notifications: NotificationService
    ) -> None:
        self._anomalies = anomalies
        self._notifications = notifications

    async def remind(self, business_date: date) -> AnomalyReminderReport:
        """Raise the reminders for one day and stamp the rows they were raised for.

        A day with no anomalies raises nothing at all — the query is the day's
        still-standing, still-untold rows, so "no anomalies" and "already reminded"
        both come back as an empty list and neither produces a notification.

        A failure is reported and the pass continues, for the reason the scan gives:
        one employee with a broken row must not swallow the other ninety-nine's
        reminders. Rows whose notification was raised are stamped whether it was
        created or suppressed as a duplicate — the employee has been told either
        way, and a crash between the raise and the stamp is exactly how a duplicate
        arises.
        """
        rows = await self._anomalies.unnotified(business_date)
        reminded: list[Anomaly] = []
        duplicates = 0
        failed: list[AnomalyFailure] = []

        for row in rows:
            try:
                outcome = await self._notifications.notify(_reminder(row))
            except Exception as error:  # noqa: BLE001 - reported, never fatal
                failed.append(failure_of(row.employee_id, error))
                continue
            duplicates += 1 if outcome.duplicate else 0
            reminded.append(row)

        if reminded:
            await self._anomalies.mark_notified([row.id for row in reminded])
        return AnomalyReminderReport(
            business_date=business_date,
            reminded=tuple(reminded),
            duplicates=duplicates,
            failed=tuple(failed),
        )


def _reminder(row: Anomaly) -> NotificationDraft:
    """One anomaly, addressed to the person it belongs to.

    The row is the entity and the discriminator is constant, so the dedupe key is
    this anomaly and nothing else: re-raising after a crash is the same event, and
    a second anomaly for the same day is a second notification.
    """
    return NotificationDraft(
        recipient_employee_id=row.employee_id,
        type=NotificationType.ATTENDANCE_ANOMALY_REMINDER,
        payload={
            "anomaly_id": str(row.id),
            "anomaly_type": str(row.type),
            "business_date": row.business_date.isoformat(),
        },
        entity_type=ANOMALY_ENTITY,
        entity_id=row.id,
        event=REMINDER_EVENT,
    )


def _skipped(employee_id: UUID, reason: str) -> None:
    """A punch nobody was told about, and why. Not an error: the record is the punch."""
    logger.info(
        "clock_out_notification_skipped", employee_id=str(employee_id), reason=reason
    )


__all__ = [
    "ANOMALY_ENTITY",
    "CLOCK_OUT_EVENT",
    "EVENT_ENTITY",
    "REMINDER_EVENT",
    "AnomalyReminder",
    "AttendanceNotifier",
    "NotificationRoute",
    "NotificationRouteSource",
]
