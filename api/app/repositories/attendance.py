"""PostgreSQL implementation of the attendance repositories.

Three things here are worth reading before the SQL:

* **`append_event` cannot rewrite anything.** The runtime role has INSERT and
  SELECT on `attendance_events` and nothing else (migration 0012), so the absence
  of an update or a delete is a property of the connection rather than a promise
  this module makes. Ticket 24's correction flow appends through this same method —
  there is no second write path to the stream for a correction to arrive by.
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

`PostgresCorrectionRepository` (ticket 24) is the third. It holds the correction
*documents* — the requests, not the events — and one recursive query over the
stream, because "the chain that starts at this punch" is the question the whole
flow turns on: whether to restate a punch or make one up, which row a new
correction points at, and whether the day and kind a document names identify one
punch or two.
"""

from collections.abc import Sequence
from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import case, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.domain.approval.models import ApprovalStatus
from app.domain.attendance.anomalies import Anomaly, AnomalyType, NewAnomaly
from app.domain.attendance.corrections import (
    Correction,
    CorrectionInput,
    CorrectionPatch,
    CorrectionQuery,
    CorrectionState,
)
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
from app.models.approval import ApprovalRequest as RequestRow
from app.models.attendance import PUNCH_DEDUPE_PREDICATE
from app.models.attendance import AttendanceAnomaly as AnomalyRow
from app.models.attendance import AttendanceCorrection as CorrectionRow
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

    async def employee_name(self, employee_id: UUID) -> str | None:
        """`"Apellidos, Nombre"`, for the export an inspector reads (ticket 24).

        Written the way a Spanish official listing writes a person, and read here
        rather than from the employee module's repository because the export needs
        a name and nothing else — the visibility projection, the withheld fields and
        the whole directory are questions this module is not asking.
        """
        row = (
            await self._session.execute(
                select(EmployeeRow.last_name, EmployeeRow.first_name).where(
                    EmployeeRow.id == employee_id
                )
            )
        ).first()
        return None if row is None else f"{row[0]}, {row[1]}"

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

    async def anomalies_by_date(
        self, employee_id: UUID, from_date: date, to_date: date
    ) -> dict[date, list[Anomaly]]:
        """Every recorded anomaly in a range, grouped by day, resolved ones included.

        Ticket 24's day read wants the open ones; the export's reader wants to know
        which days were ever flagged. Both are this query with a filter, so it
        returns everything and lets the caller say which question it is asking —
        the same day and the same rows the nightly pass wrote, rather than a second
        reading of the stream that could contradict it.
        """
        rows = await self._session.scalars(
            select(AnomalyRow)
            .where(
                AnomalyRow.employee_id == employee_id,
                AnomalyRow.business_date.between(from_date, to_date),
            )
            .order_by(AnomalyRow.business_date, AnomalyRow.type)
        )
        grouped: dict[date, list[Anomaly]] = {}
        for row in rows:
            grouped.setdefault(row.business_date, []).append(_to_anomaly(row))
        return grouped

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


def _to_correction(row: CorrectionRow) -> Correction:
    return Correction(
        id=row.id,
        employee_id=row.employee_id,
        business_date=row.business_date,
        kind=EventType(row.kind),
        corrected_at=row.corrected_at,
        reason=row.reason,
        requested_by_employee_id=row.requested_by_employee_id,
        approval_request_id=row.approval_request_id,
        applied_event_id=row.applied_event_id,
        applied_at=row.applied_at,
        submitted_at=row.submitted_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


#: The six states, in SQL. The same rule `corrections.state_of_correction` states in
#: Python, and expressed against the *engine's* status rather than against a column
#: of this table — which is why there is no column to disagree with it. Literal
#: statuses on both sides: a rename in either module has to fail a test rather than
#: make a state unreachable. The two expressions are run over one corpus of rows by
#: `tests/test_attendance_corrections.py`.
STATE_OF_ROW = case(
    # The append is the fact that matters, and it wins over everything below.
    (CorrectionRow.applied_at.is_not(None), CorrectionState.APPLIED.value),
    # Never filed, so there is no request to read.
    (CorrectionRow.approval_request_id.is_(None), CorrectionState.DRAFT.value),
    # Filed, and the request row cannot be read: in flight is the honest answer.
    (RequestRow.id.is_(None), CorrectionState.IN_APPROVAL.value),
    (
        RequestRow.status.in_(("pending_first", "pending_second")),
        CorrectionState.IN_APPROVAL.value,
    ),
    (RequestRow.status == "approved", CorrectionState.APPROVED.value),
    (RequestRow.status == "rejected", CorrectionState.REJECTED.value),
    (RequestRow.status == "withdrawn", CorrectionState.WITHDRAWN.value),
    # `draft` (returned for correction) and anything unrecognised: back with the
    # requester, which is where a returned document sits.
    else_=CorrectionState.DRAFT.value,
)


class PostgresCorrectionRepository:
    """The correction documents, and the one recursive read they turn on."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- the document ------------------------------------------------------

    async def save_correction(self, correction: CorrectionInput) -> Correction:
        row = CorrectionRow(
            employee_id=correction.employee_id,
            business_date=correction.business_date,
            kind=correction.kind.value,
            corrected_at=correction.corrected_at,
            reason=correction.reason,
            requested_by_employee_id=correction.requested_by_employee_id,
        )
        self._session.add(row)
        await self._session.flush()
        return _to_correction(row)

    async def get_correction(self, correction_id: UUID) -> Correction | None:
        row = await self._session.get(CorrectionRow, correction_id)
        return _to_correction(row) if row is not None else None

    async def list_corrections(
        self, query: CorrectionQuery
    ) -> list[tuple[Correction, CorrectionState]]:
        statement = (
            select(CorrectionRow, STATE_OF_ROW)
            .outerjoin(RequestRow, RequestRow.id == CorrectionRow.approval_request_id)
            .where(*self._conditions(query))
            # Newest first: a list of requests is read from the top, and the order
            # they were written in is the order somebody remembers them in.
            .order_by(CorrectionRow.created_at.desc(), CorrectionRow.id.desc())
            .limit(query.limit)
            .offset(query.offset)
        )
        rows = (await self._session.execute(statement)).all()
        return [(_to_correction(row), CorrectionState(state)) for row, state in rows]

    async def count_corrections(self, query: CorrectionQuery) -> int:
        statement = (
            select(func.count())
            .select_from(CorrectionRow)
            .outerjoin(RequestRow, RequestRow.id == CorrectionRow.approval_request_id)
            .where(*self._conditions(query))
        )
        return await self._session.scalar(statement) or 0

    async def approval_status_of(self, correction_id: UUID) -> ApprovalStatus | None:
        status = await self._session.scalar(
            select(RequestRow.status)
            .join(CorrectionRow, CorrectionRow.approval_request_id == RequestRow.id)
            .where(CorrectionRow.id == correction_id)
        )
        return ApprovalStatus(status) if status is not None else None

    # --- writes ------------------------------------------------------------

    async def write_draft(
        self, correction_id: UUID, *, patch: CorrectionPatch
    ) -> Correction:
        values: dict[str, object] = {}
        if patch.corrected_at is not None:
            values["corrected_at"] = patch.corrected_at
        if patch.reason is not None:
            values["reason"] = patch.reason
        if values:
            await self._write(correction_id, **values)
        return await self._require(correction_id)

    async def mark_submitted(
        self, correction_id: UUID, *, request_id: UUID, at: datetime
    ) -> Correction:
        await self._write(
            correction_id, approval_request_id=request_id, submitted_at=at
        )
        return await self._require(correction_id)

    async def mark_applied(
        self, correction_id: UUID, *, event_id: UUID, at: datetime
    ) -> Correction:
        await self._write(correction_id, applied_event_id=event_id, applied_at=at)
        return await self._require(correction_id)

    async def lock_next_unapplied(
        self,
        *,
        exclude: frozenset[UUID] = frozenset(),
        only: UUID | None = None,
    ) -> Correction | None:
        statement = (
            select(CorrectionRow)
            .where(
                # Filed and not applied. The engine's answer is *not* part of this
                # predicate: whether a request was approved is the engine's rule,
                # and SQL would have to re-express it.
                CorrectionRow.applied_at.is_(None),
                CorrectionRow.approval_request_id.is_not(None),
            )
            # Oldest first: after a crash the documents are applied in the order
            # they were filed, so a chain of corrections to one punch lands in
            # order rather than depending on which row a query returned first.
            .order_by(CorrectionRow.created_at, CorrectionRow.id)
            .limit(1)
            .with_for_update(skip_locked=True, of=CorrectionRow)
        )
        if only is not None:
            statement = statement.where(CorrectionRow.id == only)
        if exclude:
            statement = statement.where(CorrectionRow.id.not_in(exclude))
        row = await self._session.scalar(statement)
        return _to_correction(row) if row is not None else None

    # --- the stream --------------------------------------------------------

    async def punch_lineage(
        self, employee_id: UUID, business_date: date, kind: EventType
    ) -> list[AttendanceEvent]:
        """One punch and everything that restates it, oldest first.

        A recursive walk from the punch down the `correction_of_event_id` edge, and
        it terminates for the reason `derivation.chain_tip` gives: each row points
        at exactly one earlier row, so the chain cannot revisit one. The seed is
        every row of that kind on that day — which is `N` punches, normally one,
        and the flow refuses `N > 1` rather than choosing.

        Ordered by `(created_at, id)`: the order the rows were written, which is
        what `chain_tip` follows and therefore the order the corrections took
        effect in.
        """
        seed = (
            select(EventRow.id)
            .where(
                EventRow.employee_id == employee_id,
                EventRow.business_date == business_date,
                EventRow.event_type == kind.value,
            )
            .cte("lineage", recursive=True)
        )
        child = aliased(EventRow)
        lineage = seed.union_all(
            select(child.id).where(child.correction_of_event_id == seed.c.id)
        )
        rows = await self._session.scalars(
            select(EventRow)
            .where(EventRow.id.in_(select(lineage.c.id)))
            .order_by(EventRow.created_at, EventRow.id)
        )
        return [_to_event(row) for row in rows]

    # --- internals ---------------------------------------------------------

    def _conditions(self, query: CorrectionQuery) -> Sequence[object]:
        # `Sequence` rather than `list[...]`: this class defines `list`, and an
        # annotation evaluated in the class body would find that method instead of
        # the builtin.
        conditions: list[object] = []
        if query.employee_id is not None:
            conditions.append(CorrectionRow.employee_id == query.employee_id)
        if query.state is not None:
            conditions.append(STATE_OF_ROW == query.state.value)
        return conditions

    async def _require(self, correction_id: UUID) -> Correction:
        correction = await self.get_correction(correction_id)
        if correction is None:  # pragma: no cover - the row was just written
            raise RuntimeError(f"correction {correction_id} vanished mid-transaction")
        return correction

    async def _write(self, correction_id: UUID, **values: object) -> None:
        await self._session.execute(
            update(CorrectionRow)
            .where(CorrectionRow.id == correction_id)
            .values(**values, updated_at=func.now())
            .execution_options(synchronize_session=False)
        )
        await self._session.flush()

    async def commit(self) -> None:
        await self._session.commit()


__all__ = [
    "STATE_OF_ROW",
    "PostgresAnomalyRepository",
    "PostgresAttendanceRepository",
    "PostgresCorrectionRepository",
]
