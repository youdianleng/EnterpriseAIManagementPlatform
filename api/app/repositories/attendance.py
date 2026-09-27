"""PostgreSQL implementation of the attendance repository.

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
"""

from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.domain.attendance.models import (
    PUNCH_EVENT_TYPES,
    AttendanceEvent,
    DayRecord,
    DayStatus,
    EventSource,
    EventType,
    NewEvent,
)
from app.models.attendance import PUNCH_DEDUPE_PREDICATE
from app.models.attendance import AttendanceDaily as DailyRow
from app.models.attendance import AttendanceEvent as EventRow
from app.models.employee import Employee as EmployeeRow


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

    # --- internals ---------------------------------------------------------

    async def _events(
        self, employee_id: UUID, from_date: date, to_date: date
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
        for row, target_date in (await self._session.execute(statement)).all():
            grouped.setdefault(target_date or row.business_date, []).append(_to_event(row))
        return grouped

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


__all__ = ["PostgresAttendanceRepository"]
