"""PostgreSQL implementation of the attendance repositories.

Three things here are worth reading before the SQL:

* **`append_event` cannot rewrite anything.** The runtime role has INSERT and
  SELECT on `attendance_events` and nothing else (migration 0012), so the absence
  of an update or a delete is a property of the connection rather than a promise
  this module makes.
* **A punch collides on its way in and is read back.** `ON CONFLICT DO NOTHING`
  against the partial unique index is what makes two simultaneous retries of one
  request produce one row and one answer, instead of a second row or a 500. The
  service checks first; this is the half that holds when two requests check at the
  same moment, which is exactly the case the index exists for.
* **The snapshot is written with `ON CONFLICT DO UPDATE`.** `recompute_day` rebuilds
  a day rather than accumulating into it, and the upsert is where "replace, never
  add" becomes true of the storage as well as the arithmetic.

`PostgresAnomalyRepository` (ticket 23) is the second class here, and it reads the
stream through the same `_events_by_date` the daily snapshot uses. That sharing is
the point rather than a convenience: an anomaly judged from a different reading of a
day than the day's own record was built from would contradict the record it is
about.
"""

from collections.abc import Sequence
from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.domain.attendance.anomalies import Anomaly, AnomalyType, NewAnomaly
from app.domain.attendance.models import (
    PUNCH_EVENT_TYPES,
    TERMINATED_STATUS,
    AttendanceEvent,
    DayRecord,
    DayStatus,
    EventSource,
    EventType,
    NewEvent,
)
from app.domain.attendance.notify import NotificationRoute
from app.models.attendance import PUNCH_DEDUPE_PREDICATE
from app.models.attendance import AttendanceAnomaly as AnomalyRow
from app.models.attendance import AttendanceDaily as DailyRow
from app.models.attendance import AttendanceEvent as EventRow
from app.models.employee import Employee as EmployeeRow
from app.models.employee import EmployeeAssignment as AssignmentRow
from app.models.org import Department as DepartmentRow


def _to_event(row: EventRow) -> AttendanceEvent:
    return AttendanceEvent(
        id=row.id,
        employee_id=row.employee_id,
        event_type=EventType(row.event_type),
        occurred_at=row.occurred_at,
        business_date=row.business_date,
        source=EventSource(row.source),
        created_at=row.created_at,
        ip_address=row.ip_address,
        created_by_employee_id=row.created_by_employee_id,
        correction_of_event_id=row.correction_of_event_id,
        reason=row.reason,
    )


def _to_day(row: DailyRow) -> DayRecord:
    return DayRecord(
        employee_id=row.employee_id,
        business_date=row.business_date,
        status=DayStatus(row.status),
        first_in=row.first_in,
        last_out=row.last_out,
        worked_minutes=row.worked_minutes or 0,
        expected_minutes=row.expected_minutes,
        overtime_minutes=row.overtime_minutes,
        snapshot_schedule_id=row.snapshot_schedule_id,
        recomputed_at=row.recomputed_at,
    )


class PostgresAttendanceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- reads -------------------------------------------------------------

    async def employee_status(self, employee_id: UUID) -> str | None:
        return await self._session.scalar(
            select(EmployeeRow.status).where(EmployeeRow.id == employee_id)
        )

    async def find_punch(
        self, employee_id: UUID, event_type: EventType, occurred_at: datetime
    ) -> AttendanceEvent | None:
        row = await self._session.scalar(
            select(EventRow).where(
                EventRow.employee_id == employee_id,
                EventRow.event_type == event_type.value,
                EventRow.occurred_at == occurred_at,
            )
        )
        return _to_event(row) if row is not None else None

    async def latest_punch(self, employee_id: UUID) -> AttendanceEvent | None:
        """The most recent punch *as recorded*, not as corrected.

        Deciding whether a shift is open is a question about the punch clock, and
        it is asked before the new row exists. A correction moves a punch's instant
        inside the day it belongs to and the correction flow recomputes the days it
        touches (ticket 24); it does not change which punch was last.
        """
        row = await self._session.scalar(
            select(EventRow)
            .where(
                EventRow.employee_id == employee_id,
                EventRow.event_type.in_([kind.value for kind in PUNCH_EVENT_TYPES]),
            )
            .order_by(EventRow.occurred_at.desc(), EventRow.created_at.desc())
            .limit(1)
        )
        return _to_event(row) if row is not None else None

    async def events_for_day(
        self, employee_id: UUID, business_date: date
    ) -> list[AttendanceEvent]:
        grouped = await self._events(employee_id, business_date, business_date)
        return grouped.get(business_date, [])

    async def events_by_date(
        self, employee_id: UUID, from_date: date, to_date: date
    ) -> dict[date, list[AttendanceEvent]]:
        return await self._events(employee_id, from_date, to_date)

    async def day_record(self, employee_id: UUID, business_date: date) -> DayRecord | None:
        row = await self._session.scalar(
            select(DailyRow).where(
                DailyRow.employee_id == employee_id,
                DailyRow.business_date == business_date,
            )
        )
        return _to_day(row) if row is not None else None

    async def day_records(
        self, employee_id: UUID, from_date: date, to_date: date
    ) -> list[DayRecord]:
        rows = await self._session.scalars(
            select(DailyRow)
            .where(
                DailyRow.employee_id == employee_id,
                DailyRow.business_date.between(from_date, to_date),
            )
            .order_by(DailyRow.business_date)
        )
        return [_to_day(row) for row in rows]

    # --- writes ------------------------------------------------------------

    async def append_event(self, event: NewEvent) -> AttendanceEvent:
        if event.event_type is EventType.CORRECTION:
            # Outside the partial unique index by design: a correction's identity
            # is the chain it belongs to, and two corrections of two different
            # punches may legitimately carry the same instant.
            return await self._append_correction(event)

        statement = (
            pg_insert(EventRow)
            .values(
                id=uuid4(),
                employee_id=event.employee_id,
                event_type=event.event_type.value,
                occurred_at=event.occurred_at,
                business_date=event.business_date,
                source=event.source.value,
                ip_address=event.ip_address,
                created_by_employee_id=event.created_by_employee_id,
            )
            .on_conflict_do_nothing(
                index_elements=["employee_id", "event_type", "occurred_at"],
                index_where=text(PUNCH_DEDUPE_PREDICATE),
            )
            .returning(EventRow)
        )
        row = (await self._session.execute(statement)).scalars().first()
        if row is not None:
            return _to_event(row)

        # The index refused it: the same punch landed between the service's read
        # and this write. That row is the answer to the request, not an error.
        existing = await self._session.scalar(
            select(EventRow).where(
                EventRow.employee_id == event.employee_id,
                EventRow.event_type == event.event_type.value,
                EventRow.occurred_at == event.occurred_at,
            )
        )
        if existing is None:  # pragma: no cover - the colliding row cannot vanish
            raise RuntimeError(
                "the punch index refused an insert and the row it collided with is gone"
            )
        return _to_event(existing)

    async def save_day(self, record: DayRecord) -> None:
        """Write the snapshot, replacing whatever was there.

        `recomputed_at` comes from the record when the service stamped it, so the
        row a reader finds carries the same instant the caller was handed; the
        database's clock is the fallback for a record written without one.
        """
        stamped = record.recomputed_at or func.now()
        statement = (
            pg_insert(DailyRow)
            .values(
                id=uuid4(),
                employee_id=record.employee_id,
                business_date=record.business_date,
                first_in=record.first_in,
                last_out=record.last_out,
                worked_minutes=record.worked_minutes,
                expected_minutes=record.expected_minutes,
                overtime_minutes=record.overtime_minutes,
                status=record.status.value,
                snapshot_schedule_id=record.snapshot_schedule_id,
                recomputed_at=stamped,
            )
            .on_conflict_do_update(
                constraint="uq_attendance_daily_employee_date",
                set_={
                    "first_in": record.first_in,
                    "last_out": record.last_out,
                    "worked_minutes": record.worked_minutes,
                    "expected_minutes": record.expected_minutes,
                    "overtime_minutes": record.overtime_minutes,
                    "status": record.status.value,
                    "snapshot_schedule_id": record.snapshot_schedule_id,
                    "recomputed_at": stamped,
                },
            )
        )
        await self._session.execute(statement)

    async def commit(self) -> None:
        await self._session.commit()

    # --- who hears about a punch (ticket 23) -------------------------------

    async def notification_route(self, employee_id: UUID) -> NotificationRoute | None:
        """The active primary assignment's three contacts.

        Read the same way the approval route reads it — primary and open-ended —
        and for the same reason: the primary position is the one somebody is
        principally in, so a second assignment does not redirect their
        notifications. The *rule* that picks one of the three lives in
        `domain/attendance/notify.py`; this returns what the row says.
        """
        row = (
            await self._session.execute(
                select(
                    AssignmentRow.department_id,
                    AssignmentRow.manager_employee_id,
                    AssignmentRow.notification_override_employee_id,
                ).where(
                    AssignmentRow.employee_id == employee_id,
                    AssignmentRow.is_primary.is_(True),
                    AssignmentRow.end_date.is_(None),
                )
            )
        ).first()
        if row is None:
            return None
        return NotificationRoute(
            department_id=row[0],
            manager_employee_id=row[1],
            notification_override_employee_id=row[2],
        )

    async def department_manager(self, department_id: UUID) -> UUID | None:
        """The fallback the approval route also uses: somebody accountable for it."""
        return await self._session.scalar(
            select(DepartmentRow.manager_employee_id).where(DepartmentRow.id == department_id)
        )

    # --- internals ---------------------------------------------------------

    async def _events(
        self, employee_id: UUID, from_date: date, to_date: date
    ) -> dict[date, list[AttendanceEvent]]:
        return await _events_by_date(self._session, employee_id, from_date, to_date)

    async def _append_correction(self, event: NewEvent) -> AttendanceEvent:
        row = EventRow(
            employee_id=event.employee_id,
            event_type=event.event_type.value,
            occurred_at=event.occurred_at,
            business_date=event.business_date,
            source=event.source.value,
            ip_address=event.ip_address,
            created_by_employee_id=event.created_by_employee_id,
            correction_of_event_id=event.correction_of_event_id,
            reason=event.reason,
        )
        if row.correction_of_event_id is None or not row.reason:
            # The database refuses both, and by name: a correction that corrects
            # nothing, or that gives no reason, would be a row nobody can read.
            raise ValueError("a correction needs the event it corrects and a reason")
        self._session.add(row)
        await self._session.flush()
        return _to_event(row)


class PostgresAnomalyRepository:
    """The scan's storage (ticket 23), and the reminder's queue.

    Nothing here commits except `commit`, so a pass writes its day once. `insert`
    is the idempotency point: `ON CONFLICT DO NOTHING` against
    `uq_attendance_anomalies_day_type` returns no row when the day already carried
    this anomaly, and the caller reports that rather than treating it as a failure.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def employee_ids(self) -> list[UUID]:
        """Everybody whose record is open — the population the month-end pass covers.

        Terminated people are excluded, and nobody else: whether somebody was *due*
        on a date is the schedule's question, asked one employee at a time.
        """
        rows = await self._session.scalars(
            select(EmployeeRow.id)
            .where(EmployeeRow.status != TERMINATED_STATUS)
            .order_by(EmployeeRow.hire_date, EmployeeRow.id)
        )
        return list(rows)

    async def events_for_day(
        self, employee_id: UUID, business_date: date
    ) -> list[AttendanceEvent]:
        return (await self._events(employee_id, business_date, business_date)).get(
            business_date, []
        )

    async def insert(self, anomaly: NewAnomaly, detected_at: datetime) -> Anomaly | None:
        statement = (
            pg_insert(AnomalyRow)
            .values(
                id=uuid4(),
                employee_id=anomaly.employee_id,
                business_date=anomaly.business_date,
                type=anomaly.type.value,
                detected_at=detected_at,
            )
            .on_conflict_do_nothing(constraint="uq_attendance_anomalies_day_type")
            .returning(AnomalyRow)
        )
        row = (await self._session.execute(statement)).scalars().first()
        return _to_anomaly(row) if row is not None else None

    async def day_anomalies(self, employee_id: UUID, business_date: date) -> list[Anomaly]:
        rows = await self._session.scalars(
            select(AnomalyRow)
            .where(
                AnomalyRow.employee_id == employee_id,
                AnomalyRow.business_date == business_date,
            )
            .order_by(AnomalyRow.type)
        )
        return [_to_anomaly(row) for row in rows]

    async def unnotified(self, business_date: date) -> list[Anomaly]:
        """The day's standing, untold rows, in a stable order.

        Resolved rows are excluded: a reminder to make up a punch somebody has
        already made up is the one message this pass must not send.
        """
        rows = await self._session.scalars(
            select(AnomalyRow)
            .where(
                AnomalyRow.business_date == business_date,
                AnomalyRow.notified_at.is_(None),
                AnomalyRow.resolved_by_event_id.is_(None),
            )
            .order_by(AnomalyRow.employee_id, AnomalyRow.type)
        )
        return [_to_anomaly(row) for row in rows]

    async def mark_notified(self, anomaly_ids: Sequence[UUID], at: datetime) -> int:
        """Stamp the reminder, once: a row that already carries one is left alone."""
        if not anomaly_ids:
            return 0
        result = await self._session.execute(
            update(AnomalyRow)
            .where(
                AnomalyRow.id.in_(list(anomaly_ids)),
                AnomalyRow.notified_at.is_(None),
            )
            .values(notified_at=at)
        )
        return result.rowcount or 0

    async def resolve(self, anomaly_ids: Sequence[UUID], event_id: UUID) -> list[Anomaly]:
        """Mark rows resolved by the event that cleared them (ticket 24's correction)."""
        if not anomaly_ids:
            return []
        rows = await self._session.scalars(
            update(AnomalyRow)
            .where(
                AnomalyRow.id.in_(list(anomaly_ids)),
                AnomalyRow.resolved_by_event_id.is_(None),
            )
            .values(resolved_by_event_id=event_id)
            .returning(AnomalyRow)
        )
        return [_to_anomaly(row) for row in rows]

    async def commit(self) -> None:
        await self._session.commit()

    async def _events(
        self, employee_id: UUID, from_date: date, to_date: date
    ) -> dict[date, list[AttendanceEvent]]:
        return await _events_by_date(self._session, employee_id, from_date, to_date)


async def _events_by_date(
    session: AsyncSession, employee_id: UUID, from_date: date, to_date: date
) -> dict[date, list[AttendanceEvent]]:
    """A day's events, or a range's, grouped by the day each counts against.

    The join is what makes a correction belong to the day it *corrects* rather
    than to whatever `business_date` the correction row carries. The write path
    sets both to the same date; a read that depended on that would break the
    first time somebody wrote a correction by hand, and the two disagreeing is
    precisely the class of mistake this module exists to make impossible.
    """
    target = aliased(EventRow)
    statement = (
        select(EventRow, target.business_date)
        .outerjoin(target, EventRow.correction_of_event_id == target.id)
        .where(
            EventRow.employee_id == employee_id,
            or_(
                EventRow.business_date.between(from_date, to_date),
                target.business_date.between(from_date, to_date),
            ),
        )
        .order_by(EventRow.occurred_at, EventRow.created_at, EventRow.id)
    )

    grouped: dict[date, list[AttendanceEvent]] = {}
    for row, target_date in (await session.execute(statement)).all():
        grouped.setdefault(target_date or row.business_date, []).append(_to_event(row))
    return grouped


def _to_anomaly(row: AnomalyRow) -> Anomaly:
    return Anomaly(
        id=row.id,
        employee_id=row.employee_id,
        business_date=row.business_date,
        type=AnomalyType(row.type),
        detected_at=row.detected_at,
        notified_at=row.notified_at,
        resolved_by_event_id=row.resolved_by_event_id,
    )


__all__ = ["PostgresAnomalyRepository", "PostgresAttendanceRepository"]
