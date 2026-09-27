"""PostgreSQL implementation of the overtime repository.

Four things here are worth reading before the SQL:

* **`STATE_OF_ROW` is the derived state in SQL**, the same rule
  `models.state_of_request` states in Python, and expressed against the *engine's*
  status rather than against a column of this table — which is why there is no column
  to disagree with it. Literal statuses on both sides, so a rename in either module
  fails a test rather than making a state unreachable.

* **The settlement and the confirmation are two `UPDATE`s with disjoint column sets.**
  Not one "write the minutes" method with a flag: "HR's figure is stored beside the
  computed one" is a property of the statements, and a single writer would make it a
  property of remembering to pass the right arguments. `write_confirmation` never names
  `computed_minutes`, `worked_minutes` or `approved_minutes` at all.

* **`month_totals` and `day_minutes` fold the three figures in SQL**, with
  `COALESCE(confirmed_minutes, computed_minutes, approved_minutes)` — the same fold
  `OvertimeRecord.effective_minutes` performs in Python, and expressed once here so a
  month's total and a day's figure cannot be summed differently. `SUM` over no rows is
  null, which is exactly the answer `day_minutes` owes: no overtime approved is not zero
  minutes of it.

* **`export_rows` joins the withheld block on purpose.** The staff number is the
  ticket's first column and it lives in `employee_private`, which carries a policy: a
  caller whose principal is not privileged reads no rows there, so the column comes back
  empty rather than the file failing. The route's own action is what keeps such a caller
  away in the first place; the policy is the second line.

Nothing commits: the service commits once, so a request, the record an approval wrote
and the ledger row that records the movement land together or not at all.
"""

from collections.abc import Sequence
from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import case, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.approval.models import ApprovalStatus
from app.domain.overtime.models import (
    MonthlyTotal,
    NewOvertimeRecord,
    OvertimeEntry,
    OvertimeEntryType,
    OvertimeExportRow,
    OvertimeRecord,
    OvertimeRecordQuery,
    OvertimeRequest,
    OvertimeRequestInput,
    OvertimeRequestPatch,
    OvertimeRequestQuery,
    OvertimeRequestState,
)
from app.models.approval import ApprovalRequest as RequestRow
from app.models.employee import Employee as EmployeeRow
from app.models.employee import EmployeeAssignment as AssignmentRow
from app.models.employee import EmployeePrivate as PrivateRow
from app.models.org import Department as DepartmentRow
from app.models.overtime import OvertimeEntry as EntryRow
from app.models.overtime import OvertimeRecord as RecordRow
from app.models.overtime import OvertimeRequest as OvertimeRequestRow

#: The five states, in SQL. The order is the order of the facts: a withdrawal is the
#: requester's own act and closes the document whatever the engine says, an approval
#: that has been resolved is in force, and a request nobody filed is a draft without
#: asking the engine.
STATE_OF_ROW = case(
    (OvertimeRequestRow.withdrawn_at.is_not(None), OvertimeRequestState.WITHDRAWN.value),
    (OvertimeRequestRow.approved_at.is_not(None), OvertimeRequestState.APPROVED.value),
    (OvertimeRequestRow.approval_request_id.is_(None), OvertimeRequestState.DRAFT.value),
    # Filed, and the request row cannot be read: in flight is the honest answer.
    (RequestRow.id.is_(None), OvertimeRequestState.IN_APPROVAL.value),
    (
        RequestRow.status.in_(("pending_first", "pending_second")),
        OvertimeRequestState.IN_APPROVAL.value,
    ),
    (RequestRow.status == "approved", OvertimeRequestState.APPROVED.value),
    (RequestRow.status == "rejected", OvertimeRequestState.REJECTED.value),
    (RequestRow.status == "withdrawn", OvertimeRequestState.WITHDRAWN.value),
    # `draft` (returned for correction) and anything unrecognised: back with the
    # requester, which is where a returned document sits.
    else_=OvertimeRequestState.DRAFT.value,
)

#: The figure in force for a record, folded where the rows are read. The same rule as
#: `models.OvertimeRecord.effective_minutes`, stated in SQL because the month's totals
#: and the day's figure are summed by the database.
EFFECTIVE_MINUTES = func.coalesce(
    RecordRow.confirmed_minutes, RecordRow.computed_minutes, RecordRow.approved_minutes
)

#: What the exported *confirmed* column states: the figure the day came to — HR's, or
#: the settled smaller-of — and nothing at all while the day is still open. Deliberately
#: not `EFFECTIVE_MINUTES`: the file already carries the approved minutes in their own
#: column, and repeating them here would leave a reader unable to tell a day that has
#: been computed from one that has not. The day's own `overtime_minutes` *is*
#: `EFFECTIVE_MINUTES`, because a day's overtime before settlement is what was approved.
SETTLED_MINUTES = func.coalesce(RecordRow.confirmed_minutes, RecordRow.computed_minutes)


def _to_request(row: OvertimeRequestRow) -> OvertimeRequest:
    return OvertimeRequest(
        id=row.id,
        employee_id=row.employee_id,
        business_date=row.business_date,
        expected_minutes=row.expected_minutes,
        reason=row.reason,
        approval_request_id=row.approval_request_id,
        submitted_at=row.submitted_at,
        approved_at=row.approved_at,
        withdrawn_at=row.withdrawn_at,
        settled_at=row.settled_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_record(row: RecordRow) -> OvertimeRecord:
    return OvertimeRecord(
        id=row.id,
        request_id=row.request_id,
        employee_id=row.employee_id,
        business_date=row.business_date,
        month_bucket=row.month_bucket,
        approved_minutes=row.approved_minutes,
        worked_minutes=row.worked_minutes,
        computed_minutes=row.computed_minutes,
        needs_confirmation=row.needs_confirmation,
        confirmed_minutes=row.confirmed_minutes,
        confirmed_by_employee_id=row.confirmed_by_employee_id,
        confirmed_at=row.confirmed_at,
        confirmation_note=row.confirmation_note,
        settled_at=row.settled_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_entry(row: EntryRow) -> OvertimeEntry:
    return OvertimeEntry(
        id=row.id,
        record_id=row.record_id,
        entry_type=OvertimeEntryType(row.entry_type),
        approved_minutes=row.approved_minutes,
        computed_minutes=row.computed_minutes,
        confirmed_minutes=row.confirmed_minutes,
        note=row.note,
        created_by_employee_id=row.created_by_employee_id,
        created_at=row.created_at,
    )


def _name(last_name: str, first_name: str) -> str:
    """`"Apellidos, Nombre"`, the way a Spanish official listing writes a person.

    The same form the attendance export uses, and for the same reason: both files are
    read by somebody matching a row against a payroll list.
    """
    return f"{last_name}, {first_name}"


class PostgresOvertimeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- requests -----------------------------------------------------------

    async def save_request(self, data: OvertimeRequestInput) -> OvertimeRequest:
        row = OvertimeRequestRow(
            id=uuid4(),
            employee_id=data.employee_id,
            business_date=data.business_date,
            expected_minutes=data.expected_minutes,
            reason=data.reason,
        )
        self._session.add(row)
        await self._session.flush()
        return _to_request(row)

    async def get_request(self, request_id: UUID) -> OvertimeRequest | None:
        row = await self._session.get(OvertimeRequestRow, request_id)
        return _to_request(row) if row is not None else None

    async def list_requests(
        self, query: OvertimeRequestQuery
    ) -> list[tuple[OvertimeRequest, OvertimeRequestState]]:
        statement = (
            select(OvertimeRequestRow, STATE_OF_ROW)
            .outerjoin(RequestRow, RequestRow.id == OvertimeRequestRow.approval_request_id)
            .where(*self._request_conditions(query))
            # Newest first: a list of requests is read from the top, and the order they
            # were written in is the order somebody remembers them in.
            .order_by(OvertimeRequestRow.created_at.desc(), OvertimeRequestRow.id.desc())
            .limit(query.limit)
            .offset(query.offset)
        )
        rows = (await self._session.execute(statement)).all()
        return [(_to_request(row), OvertimeRequestState(state)) for row, state in rows]

    async def count_requests(self, query: OvertimeRequestQuery) -> int:
        statement = (
            select(func.count())
            .select_from(OvertimeRequestRow)
            .outerjoin(RequestRow, RequestRow.id == OvertimeRequestRow.approval_request_id)
            .where(*self._request_conditions(query))
        )
        return await self._session.scalar(statement) or 0

    async def write_draft(
        self, request_id: UUID, *, patch: OvertimeRequestPatch
    ) -> OvertimeRequest:
        await self._write_request(
            request_id,
            business_date=patch.business_date,
            expected_minutes=patch.expected_minutes,
            reason=patch.reason,
        )
        return await self._require_request(request_id)

    async def mark_filed(
        self, request_id: UUID, *, approval_request_id: UUID, at: datetime
    ) -> OvertimeRequest:
        await self._write_request(
            request_id, approval_request_id=approval_request_id, submitted_at=at
        )
        return await self._require_request(request_id)

    async def mark_approved(self, request_id: UUID, at: datetime) -> OvertimeRequest:
        await self._write_request(request_id, approved_at=at)
        return await self._require_request(request_id)

    async def mark_withdrawn(self, request_id: UUID, at: datetime) -> OvertimeRequest:
        await self._write_request(request_id, withdrawn_at=at)
        return await self._require_request(request_id)

    async def mark_settled(self, request_id: UUID, at: datetime) -> OvertimeRequest:
        await self._write_request(request_id, settled_at=at)
        return await self._require_request(request_id)

    async def lock_next_unresolved(
        self, *, exclude: frozenset[UUID] = frozenset(), only: UUID | None = None
    ) -> OvertimeRequest | None:
        statement = (
            select(OvertimeRequestRow)
            .where(
                # Filed, and this module has not finished with the document. The
                # engine's answer is *not* part of this predicate: whether a request was
                # approved is the engine's rule, and SQL would have to re-express it.
                OvertimeRequestRow.settled_at.is_(None),
                OvertimeRequestRow.approval_request_id.is_not(None),
            )
            # Oldest first, so a crash is caught up in the order things were filed.
            .order_by(OvertimeRequestRow.submitted_at, OvertimeRequestRow.id)
            .limit(1)
            .with_for_update(skip_locked=True, of=OvertimeRequestRow)
        )
        if only is not None:
            statement = statement.where(OvertimeRequestRow.id == only)
        if exclude:
            statement = statement.where(OvertimeRequestRow.id.not_in(exclude))
        row = await self._session.scalar(statement)
        return _to_request(row) if row is not None else None

    async def approval_status_of(self, request_id: UUID) -> ApprovalStatus | None:
        status = await self._session.scalar(
            select(RequestRow.status)
            .join(OvertimeRequestRow, OvertimeRequestRow.approval_request_id == RequestRow.id)
            .where(OvertimeRequestRow.id == request_id)
        )
        return ApprovalStatus(status) if status is not None else None

    async def live_request_for_day(
        self, employee_id: UUID, business_date: date, *, excluding: UUID | None = None
    ) -> OvertimeRequest | None:
        statement = (
            select(OvertimeRequestRow)
            .where(
                OvertimeRequestRow.employee_id == employee_id,
                OvertimeRequestRow.business_date == business_date,
                # Not yet finished with: a rejected or withdrawn request releases the
                # day, and the way forward for it is a new request.
                OvertimeRequestRow.settled_at.is_(None),
            )
            .order_by(OvertimeRequestRow.created_at)
            .limit(1)
        )
        if excluding is not None:
            statement = statement.where(OvertimeRequestRow.id != excluding)
        row = await self._session.scalar(statement)
        return _to_request(row) if row is not None else None

    # --- records ------------------------------------------------------------

    async def save_record(self, record: NewOvertimeRecord) -> OvertimeRecord:
        """The record an approval produced.

        `ON CONFLICT DO NOTHING` against `uq_overtime_records_request` is what makes a
        second resolve pass harmless: the colliding insert reports no row and the one
        already there is read back, exactly as the punch stream answers a retry.
        """
        statement = (
            insert(RecordRow)
            .values(
                id=uuid4(),
                request_id=record.request_id,
                employee_id=record.employee_id,
                business_date=record.business_date,
                month_bucket=record.month_bucket,
                approved_minutes=record.approved_minutes,
                needs_confirmation=False,
            )
            .on_conflict_do_nothing(constraint="uq_overtime_records_request")
            .returning(RecordRow.id)
        )
        await self._session.scalar(statement)
        await self._session.flush()
        existing = await self.record_for_request(record.request_id)
        if existing is None:  # pragma: no cover - the insert or the row it collided with
            raise LookupError(f"overtime record for request {record.request_id} vanished")
        return existing

    async def get_record(self, record_id: UUID) -> OvertimeRecord | None:
        row = await self._session.get(RecordRow, record_id)
        return _to_record(row) if row is not None else None

    async def record_for_request(self, request_id: UUID) -> OvertimeRecord | None:
        row = await self._session.scalar(
            select(RecordRow).where(RecordRow.request_id == request_id)
        )
        return _to_record(row) if row is not None else None

    async def record_for_day(
        self, employee_id: UUID, business_date: date
    ) -> OvertimeRecord | None:
        row = await self._session.scalar(
            select(RecordRow).where(
                RecordRow.employee_id == employee_id,
                RecordRow.business_date == business_date,
            )
        )
        return _to_record(row) if row is not None else None

    async def list_records(self, query: OvertimeRecordQuery) -> list[OvertimeRecord]:
        statement = (
            select(RecordRow)
            .where(*self._record_conditions(query))
            # Newest day first: a ledger is read from the top, and the days somebody
            # remembers are the ones that just happened.
            .order_by(RecordRow.business_date.desc(), RecordRow.id.desc())
            .limit(query.limit)
            .offset(query.offset)
        )
        return [_to_record(row) for row in await self._session.scalars(statement)]

    async def count_records(self, query: OvertimeRecordQuery) -> int:
        statement = (
            select(func.count()).select_from(RecordRow).where(*self._record_conditions(query))
        )
        return await self._session.scalar(statement) or 0

    async def lock_next_unsettled(
        self,
        *,
        month: str | None = None,
        exclude: frozenset[UUID] = frozenset(),
        only: UUID | None = None,
    ) -> OvertimeRecord | None:
        statement = (
            select(RecordRow)
            .where(RecordRow.settled_at.is_(None))
            # Oldest business date first: the day that has been waiting longest is the
            # one whose figure somebody is most likely to be looking for.
            .order_by(RecordRow.business_date, RecordRow.id)
            .limit(1)
            .with_for_update(skip_locked=True, of=RecordRow)
        )
        if month is not None:
            statement = statement.where(RecordRow.month_bucket == month)
        if only is not None:
            statement = statement.where(RecordRow.id == only)
        if exclude:
            statement = statement.where(RecordRow.id.not_in(exclude))
        row = await self._session.scalar(statement)
        return _to_record(row) if row is not None else None

    async def write_settlement(
        self,
        record_id: UUID,
        *,
        worked_minutes: int,
        computed_minutes: int,
        needs_confirmation: bool,
        at: datetime,
    ) -> OvertimeRecord:
        """Write the day's arithmetic. `confirmed_minutes` is deliberately absent."""
        await self._write_record(
            record_id,
            worked_minutes=worked_minutes,
            computed_minutes=computed_minutes,
            needs_confirmation=needs_confirmation,
            settled_at=at,
        )
        return await self._require_record(record_id)

    async def write_confirmation(
        self,
        record_id: UUID,
        *,
        confirmed_minutes: int,
        note: str,
        confirmed_by_employee_id: UUID | None,
        at: datetime,
    ) -> OvertimeRecord:
        """Write HR's figure beside the computed one.

        The column list is the whole guarantee: `approved_minutes`, `worked_minutes` and
        `computed_minutes` are not in it, so no caller can overwrite what the day came
        to — and the check constraint on the record says the computed figure is the
        smaller of the other two, so a write that tried would be refused as well. The
        flag is cleared because the queue is answered; the ledger keeps that it was set.
        """
        await self._write_record(
            record_id,
            confirmed_minutes=confirmed_minutes,
            confirmed_by_employee_id=confirmed_by_employee_id,
            confirmed_at=at,
            confirmation_note=note,
            needs_confirmation=False,
        )
        return await self._require_record(record_id)

    async def append_entry(
        self,
        *,
        record_id: UUID,
        entry_type: OvertimeEntryType,
        approved_minutes: int,
        computed_minutes: int | None,
        confirmed_minutes: int | None,
        note: str | None = None,
        created_by_employee_id: UUID | None = None,
    ) -> OvertimeEntry:
        row = EntryRow(
            id=uuid4(),
            record_id=record_id,
            entry_type=entry_type.value,
            approved_minutes=approved_minutes,
            computed_minutes=computed_minutes,
            confirmed_minutes=confirmed_minutes,
            note=note,
            created_by_employee_id=created_by_employee_id,
        )
        self._session.add(row)
        await self._session.flush()
        return _to_entry(row)

    async def entries_for_record(self, record_id: UUID) -> list[OvertimeEntry]:
        rows = await self._session.scalars(
            select(EntryRow)
            .where(EntryRow.record_id == record_id)
            # By the database's own sequence, which is the order the movements were
            # appended in: `created_at` is the transaction's start time, so every entry
            # one settlement writes shares it.
            .order_by(EntryRow.seq)
        )
        return [_to_entry(row) for row in rows]

    async def day_minutes(self, employee_id: UUID, business_date: date) -> int | None:
        """The day's overtime, folded in SQL. No rows is null, not zero."""
        return await self._session.scalar(
            select(func.sum(EFFECTIVE_MINUTES)).where(
                RecordRow.employee_id == employee_id,
                RecordRow.business_date == business_date,
            )
        )

    async def minutes_by_date(
        self, employee_id: UUID, from_date: date, to_date: date
    ) -> dict[date, int]:
        statement = (
            select(RecordRow.business_date, func.sum(EFFECTIVE_MINUTES))
            .where(
                RecordRow.employee_id == employee_id,
                RecordRow.business_date.between(from_date, to_date),
            )
            .group_by(RecordRow.business_date)
        )
        rows = (await self._session.execute(statement)).all()
        return {row[0]: row[1] or 0 for row in rows}

    async def month_totals(self, month: str) -> list[MonthlyTotal]:
        statement = (
            select(
                RecordRow.employee_id,
                EmployeeRow.last_name,
                EmployeeRow.first_name,
                func.count().label("records"),
                func.sum(RecordRow.approved_minutes).label("approved_minutes"),
                func.sum(EFFECTIVE_MINUTES).label("effective_minutes"),
                func.count()
                .filter(RecordRow.needs_confirmation)
                .label("awaiting_confirmation"),
            )
            .join(EmployeeRow, EmployeeRow.id == RecordRow.employee_id)
            .where(RecordRow.month_bucket == month)
            .group_by(RecordRow.employee_id, EmployeeRow.last_name, EmployeeRow.first_name)
            # By name: a summary is read by a person looking for somebody, and the ids
            # are what the query groups by rather than what a reader scans.
            .order_by(EmployeeRow.last_name, EmployeeRow.first_name, RecordRow.employee_id)
        )
        rows = (await self._session.execute(statement)).all()
        return [
            MonthlyTotal(
                employee_id=row[0],
                employee_name=_name(row[1], row[2]),
                records=row[3],
                approved_minutes=row[4] or 0,
                effective_minutes=row[5] or 0,
                awaiting_confirmation=row[6] or 0,
            )
            for row in rows
        ]

    async def export_rows(self, month: str) -> list[OvertimeExportRow]:
        """The month's lines, ordered by staff number and then date.

        `employee_no` comes from `employee_private`, which carries a read policy: a
        caller whose principal is not privileged gets no row there and therefore a blank
        column rather than an error. The primary assignment is the department a person is
        principally in, which is what a payroll list states; somebody with no open
        assignment gets a blank department rather than being dropped from the file —
        their hours were still worked.
        """
        statement = (
            select(
                PrivateRow.employee_no,
                EmployeeRow.last_name,
                EmployeeRow.first_name,
                DepartmentRow.name_es,
                DepartmentRow.name_en,
                RecordRow.business_date,
                RecordRow.approved_minutes,
                SETTLED_MINUTES,
            )
            .join(EmployeeRow, EmployeeRow.id == RecordRow.employee_id)
            .outerjoin(PrivateRow, PrivateRow.employee_id == RecordRow.employee_id)
            .outerjoin(
                AssignmentRow,
                (AssignmentRow.employee_id == RecordRow.employee_id)
                & AssignmentRow.is_primary.is_(True)
                & AssignmentRow.end_date.is_(None),
            )
            .outerjoin(DepartmentRow, DepartmentRow.id == AssignmentRow.department_id)
            .where(RecordRow.month_bucket == month)
            .order_by(
                PrivateRow.employee_no.asc().nulls_last(),
                EmployeeRow.last_name,
                EmployeeRow.first_name,
                RecordRow.business_date,
                RecordRow.id,
            )
        )
        rows = (await self._session.execute(statement)).all()
        return [
            OvertimeExportRow(
                employee_no=row[0],
                employee_name=_name(row[1], row[2]),
                # The Spanish name is what the file states, and the English one is the
                # fallback for a department an installation never translated.
                department=row[3] or row[4],
                business_date=row[5],
                approved_minutes=row[6],
                confirmed_minutes=row[7],
            )
            for row in rows
        ]

    # --- plumbing -----------------------------------------------------------

    async def employee_exists(self, employee_id: UUID) -> bool:
        return bool(
            await self._session.scalar(
                select(func.count()).select_from(EmployeeRow).where(EmployeeRow.id == employee_id)
            )
        )

    async def commit(self) -> None:
        await self._session.commit()

    async def rollback(self) -> None:
        await self._session.rollback()

    # --- internals ----------------------------------------------------------

    def _request_conditions(self, query: OvertimeRequestQuery) -> Sequence[object]:
        # `Sequence` rather than `list[...]`: this class defines `list`, and an
        # annotation evaluated in the class body would find that method rather than the
        # builtin.
        conditions: list[object] = []
        if query.employee_id is not None:
            conditions.append(OvertimeRequestRow.employee_id == query.employee_id)
        if query.state is not None:
            conditions.append(STATE_OF_ROW == query.state.value)
        return conditions

    def _record_conditions(self, query: OvertimeRecordQuery) -> Sequence[object]:
        conditions: list[object] = []
        if query.employee_id is not None:
            conditions.append(RecordRow.employee_id == query.employee_id)
        if query.month is not None:
            conditions.append(RecordRow.month_bucket == query.month)
        if query.needs_confirmation is not None:
            conditions.append(RecordRow.needs_confirmation.is_(query.needs_confirmation))
        return conditions

    async def _require_request(self, request_id: UUID) -> OvertimeRequest:
        request = await self.get_request(request_id)
        if request is None:  # pragma: no cover - the row was just written
            raise LookupError(f"overtime request {request_id} vanished mid-transaction")
        return request

    async def _require_record(self, record_id: UUID) -> OvertimeRecord:
        record = await self.get_record(record_id)
        if record is None:  # pragma: no cover - the row was just written
            raise LookupError(f"overtime record {record_id} vanished mid-transaction")
        return record

    async def _write_request(self, request_id: UUID, **values: object) -> None:
        present = {key: value for key, value in values.items() if value is not None}
        if not present:
            return
        await self._session.execute(
            update(OvertimeRequestRow)
            .where(OvertimeRequestRow.id == request_id)
            .values(**present, updated_at=func.now())
            .execution_options(synchronize_session=False)
        )
        await self._session.flush()

    async def _write_record(self, record_id: UUID, **values: object) -> None:
        await self._session.execute(
            update(RecordRow)
            .where(RecordRow.id == record_id)
            .values(**values, updated_at=func.now())
            .execution_options(synchronize_session=False)
        )
        await self._session.flush()


__all__ = [
    "EFFECTIVE_MINUTES",
    "SETTLED_MINUTES",
    "STATE_OF_ROW",
    "PostgresOvertimeRepository",
]
