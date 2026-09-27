"""PostgreSQL implementation of the leave repository.

Four things here are worth reading before the code:

* **`ensure_balance` is an `INSERT ... ON CONFLICT DO NOTHING RETURNING`.** The
  statement waits for a conflicting transaction and returns a row only when *this*
  statement is the one that inserted it, which is what makes "did I just grant this
  allowance" answerable under concurrency. Two requests filed in the same second must
  not both write a `grant` entry, and a check-then-insert would let them.

* **`lock_balance` is `FOR UPDATE`, and it is the first line of the allowance rule.**
  The service takes the lock before it reads the remainder, so two submissions cannot
  both see three remaining days and both take them. The migration's CHECK is the
  second line, for a path that somehow skipped this one.

* **`lock_next_unsettled` carries `SKIP LOCKED`.** The settle sweep is a pass over
  filed requests; two workers running it take different documents instead of blocking
  on the same one.

* **`STATE_OF_ROW` is the derived state in SQL**, the same rule
  `models.state_of_request` states in Python, and expressed against the *engine's*
  status rather than against a column of this table — which is why there is no column
  to disagree with it. Literal statuses on both sides, so a rename in either module
  fails a test rather than making a state unreachable.

Nothing commits: the service commits once, so a request, the days it reserves and the
ledger row that records the movement land together or not at all.
"""

from collections.abc import Sequence
from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import case, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.approval.models import ApprovalStatus
from app.domain.leave.models import (
    LeaveBalance,
    LeaveBalanceEntry,
    LeaveEntryType,
    LeaveRequest,
    LeaveRequestInput,
    LeaveRequestQuery,
    LeaveRequestState,
    LeaveType,
    LeaveTypeInput,
    LeaveTypePatch,
)
from app.models.approval import ApprovalRequest as RequestRow
from app.models.employee import Employee as EmployeeRow
from app.models.leave import LeaveBalance as BalanceRow
from app.models.leave import LeaveBalanceEntry as EntryRow
from app.models.leave import LeaveRequest as LeaveRequestRow
from app.models.leave import LeaveType as TypeRow


def _to_type(row: TypeRow) -> LeaveType:
    return LeaveType(
        id=row.id,
        code=row.code,
        name_es=row.name_es,
        name_en=row.name_en,
        is_paid=row.is_paid,
        requires_attachment=row.requires_attachment,
        counts_against_annual=row.counts_against_annual,
        is_active=row.is_active,
    )


def _to_balance(row: BalanceRow) -> LeaveBalance:
    return LeaveBalance(
        employee_id=row.employee_id,
        year=row.year,
        leave_type_id=row.leave_type_id,
        entitled_days=row.entitled_days,
        carried_over_days=row.carried_over_days,
        used_days=row.used_days,
        pending_days=row.pending_days,
        id=row.id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_entry(row: EntryRow) -> LeaveBalanceEntry:
    return LeaveBalanceEntry(
        id=row.id,
        balance_id=row.balance_id,
        entry_type=LeaveEntryType(row.entry_type),
        days=row.days,
        entitled_days=row.entitled_days,
        carried_over_days=row.carried_over_days,
        used_days=row.used_days,
        pending_days=row.pending_days,
        leave_request_id=row.leave_request_id,
        note=row.note,
        created_by_employee_id=row.created_by_employee_id,
        created_at=row.created_at,
    )


def _to_request(row: LeaveRequestRow) -> LeaveRequest:
    return LeaveRequest(
        id=row.id,
        employee_id=row.employee_id,
        leave_type_id=row.leave_type_id,
        start_date=row.start_date,
        end_date=row.end_date,
        business_days_count=row.business_days_count,
        approval_request_id=row.approval_request_id,
        attachment_reference=row.attachment_reference,
        submitted_at=row.submitted_at,
        approved_at=row.approved_at,
        withdrawn_at=row.withdrawn_at,
        settled_at=row.settled_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


#: The five states, in SQL. The order is the order of the facts: a withdrawal is the
#: requester's own act and closes the document whatever the engine says, a settled
#: approval is in force, and a request nobody filed is a draft without asking the
#: engine.
STATE_OF_ROW = case(
    (LeaveRequestRow.withdrawn_at.is_not(None), LeaveRequestState.WITHDRAWN.value),
    (LeaveRequestRow.approved_at.is_not(None), LeaveRequestState.APPROVED.value),
    (LeaveRequestRow.approval_request_id.is_(None), LeaveRequestState.DRAFT.value),
    # Filed, and the request row cannot be read: in flight is the honest answer.
    (RequestRow.id.is_(None), LeaveRequestState.IN_APPROVAL.value),
    (
        RequestRow.status.in_(("pending_first", "pending_second")),
        LeaveRequestState.IN_APPROVAL.value,
    ),
    (RequestRow.status == "approved", LeaveRequestState.APPROVED.value),
    (RequestRow.status == "rejected", LeaveRequestState.REJECTED.value),
    (RequestRow.status == "withdrawn", LeaveRequestState.WITHDRAWN.value),
    # `draft` (returned for correction) and anything unrecognised: back with the
    # requester, which is where a returned document sits.
    else_=LeaveRequestState.DRAFT.value,
)


class PostgresLeaveRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- the type catalogue -------------------------------------------------

    async def list_types(self, *, include_inactive: bool = False) -> list[LeaveType]:
        statement = select(TypeRow).order_by(TypeRow.is_active.desc(), TypeRow.code)
        if not include_inactive:
            statement = statement.where(TypeRow.is_active.is_(True))
        return [_to_type(row) for row in await self._session.scalars(statement)]

    async def get_type_by_code(self, code: str) -> LeaveType | None:
        row = await self._session.scalar(select(TypeRow).where(TypeRow.code == code))
        return _to_type(row) if row is not None else None

    async def get_type(self, leave_type_id: UUID) -> LeaveType | None:
        row = await self._session.get(TypeRow, leave_type_id)
        return _to_type(row) if row is not None else None

    async def save_type(
        self, data: LeaveTypeInput, *, type_id: UUID | None = None
    ) -> LeaveType:
        row = TypeRow(
            id=type_id or uuid4(),
            code=data.code,
            name_es=data.name_es,
            name_en=data.name_en,
            is_paid=data.is_paid,
            requires_attachment=data.requires_attachment,
            counts_against_annual=data.counts_against_annual,
            is_active=True,
        )
        self._session.add(row)
        await self._session.flush()
        return _to_type(row)

    async def update_type(self, type_id: UUID, patch: LeaveTypePatch) -> LeaveType:
        values: dict[str, object] = {}
        for field in (
            "name_es",
            "name_en",
            "is_paid",
            "requires_attachment",
            "counts_against_annual",
            "is_active",
        ):
            value = getattr(patch, field)
            if value is not None:
                values[field] = value
        if values:
            await self._session.execute(
                update(TypeRow)
                .where(TypeRow.id == type_id)
                .values(**values, updated_at=func.now())
                .execution_options(synchronize_session=False)
            )
            await self._session.flush()
        row = await self._session.get(TypeRow, type_id)
        if row is None:  # pragma: no cover - the service reads the row first
            raise LookupError(f"leave type {type_id} disappeared between two reads")
        return _to_type(row)

    # --- balances -----------------------------------------------------------

    async def get_balance(
        self, employee_id: UUID, year: int, leave_type_id: UUID
    ) -> LeaveBalance | None:
        row = await self._session.scalar(
            select(BalanceRow).where(
                BalanceRow.employee_id == employee_id,
                BalanceRow.year == year,
                BalanceRow.leave_type_id == leave_type_id,
            )
        )
        return _to_balance(row) if row is not None else None

    async def get_balance_by_id(self, balance_id: UUID) -> LeaveBalance | None:
        row = await self._session.get(BalanceRow, balance_id)
        return _to_balance(row) if row is not None else None

    async def ensure_balance(
        self,
        employee_id: UUID,
        year: int,
        leave_type_id: UUID,
        *,
        entitled_days: int,
    ) -> tuple[LeaveBalance, bool]:
        """The row, inserted if missing. `created` is true only for the inserter.

        `ON CONFLICT DO NOTHING` waits for a conflicting transaction and reports no
        row when that transaction's row is the one that survived, which is what stops
        two concurrent first-needs from both writing a `grant` entry.
        """
        statement = (
            insert(BalanceRow)
            .values(
                id=uuid4(),
                employee_id=employee_id,
                year=year,
                leave_type_id=leave_type_id,
                entitled_days=entitled_days,
                carried_over_days=0,
                used_days=0,
                pending_days=0,
            )
            .on_conflict_do_nothing(
                index_elements=["employee_id", "year", "leave_type_id"]
            )
            .returning(BalanceRow.id)
        )
        created_id = await self._session.scalar(statement)
        await self._session.flush()

        balance = await self.get_balance(employee_id, year, leave_type_id)
        if balance is None:  # pragma: no cover - the insert or the existing row
            raise LookupError(f"leave balance for {employee_id} {year} vanished mid-write")
        return balance, created_id is not None

    async def lock_balance(self, balance_id: UUID) -> LeaveBalance:
        row = await self._session.scalar(
            select(BalanceRow).where(BalanceRow.id == balance_id).with_for_update()
        )
        if row is None:  # pragma: no cover - the service reads the row first
            raise LookupError(f"leave balance {balance_id} disappeared before its lock")
        return _to_balance(row)

    async def list_balances(
        self, employee_id: UUID, *, year: int | None = None
    ) -> list[LeaveBalance]:
        statement = (
            select(BalanceRow)
            .where(BalanceRow.employee_id == employee_id)
            .order_by(BalanceRow.year.desc(), BalanceRow.created_at)
        )
        if year is not None:
            statement = statement.where(BalanceRow.year == year)
        return [_to_balance(row) for row in await self._session.scalars(statement)]

    async def list_balances_for_year(self, year: int) -> list[LeaveBalance]:
        rows = await self._session.scalars(
            select(BalanceRow)
            .where(BalanceRow.year == year)
            .order_by(BalanceRow.employee_id, BalanceRow.created_at)
        )
        return [_to_balance(row) for row in rows]

    async def write_balance(
        self,
        balance_id: UUID,
        *,
        entitled_days: int,
        carried_over_days: int,
        used_days: int,
        pending_days: int,
    ) -> LeaveBalance:
        await self._session.execute(
            update(BalanceRow)
            .where(BalanceRow.id == balance_id)
            .values(
                entitled_days=entitled_days,
                carried_over_days=carried_over_days,
                used_days=used_days,
                pending_days=pending_days,
                updated_at=func.now(),
            )
            .execution_options(synchronize_session=False)
        )
        await self._session.flush()
        row = await self._session.get(BalanceRow, balance_id)
        if row is None:  # pragma: no cover - the caller holds the row's lock
            raise LookupError(f"leave balance {balance_id} disappeared between two reads")
        return _to_balance(row)

    async def append_entry(
        self,
        *,
        balance_id: UUID,
        entry_type: LeaveEntryType,
        days: int,
        entitled_days: int,
        carried_over_days: int,
        used_days: int,
        pending_days: int,
        leave_request_id: UUID | None = None,
        note: str | None = None,
        created_by_employee_id: UUID | None = None,
    ) -> LeaveBalanceEntry:
        row = EntryRow(
            id=uuid4(),
            balance_id=balance_id,
            entry_type=entry_type.value,
            days=days,
            entitled_days=entitled_days,
            carried_over_days=carried_over_days,
            used_days=used_days,
            pending_days=pending_days,
            leave_request_id=leave_request_id,
            note=note,
            created_by_employee_id=created_by_employee_id,
        )
        self._session.add(row)
        await self._session.flush()
        return _to_entry(row)

    async def entries_for_balance(self, balance_id: UUID) -> list[LeaveBalanceEntry]:
        rows = await self._session.scalars(
            select(EntryRow)
            .where(EntryRow.balance_id == balance_id)
            # By the database's own sequence, which is the order the movements were
            # appended in: `created_at` is the transaction's start time, so every entry
            # one request writes shares it.
            .order_by(EntryRow.seq)
        )
        return [_to_entry(row) for row in rows]

    async def entries_for_request(self, request_id: UUID) -> list[LeaveBalanceEntry]:
        rows = await self._session.scalars(
            select(EntryRow)
            .where(EntryRow.leave_request_id == request_id)
            .order_by(EntryRow.seq)
        )
        return [_to_entry(row) for row in rows]

    # --- requests -----------------------------------------------------------

    async def save_request(self, data: LeaveRequestInput) -> LeaveRequest:
        row = LeaveRequestRow(
            id=uuid4(),
            employee_id=data.employee_id,
            leave_type_id=data.leave_type_id,
            start_date=data.start_date,
            end_date=data.end_date,
            business_days_count=data.business_days_count,
            attachment_reference=data.attachment_reference,
        )
        self._session.add(row)
        await self._session.flush()
        return _to_request(row)

    async def get_request(self, request_id: UUID) -> LeaveRequest | None:
        row = await self._session.get(LeaveRequestRow, request_id)
        return _to_request(row) if row is not None else None

    async def list_requests(
        self, query: LeaveRequestQuery
    ) -> list[tuple[LeaveRequest, LeaveRequestState]]:
        statement = (
            select(LeaveRequestRow, STATE_OF_ROW)
            .outerjoin(RequestRow, RequestRow.id == LeaveRequestRow.approval_request_id)
            .where(*self._conditions(query))
            # Newest first: a list of requests is read from the top, and the order
            # they were written in is the order somebody remembers them in.
            .order_by(LeaveRequestRow.created_at.desc(), LeaveRequestRow.id.desc())
            .limit(query.limit)
            .offset(query.offset)
        )
        rows = (await self._session.execute(statement)).all()
        return [(_to_request(row), LeaveRequestState(state)) for row, state in rows]

    async def count_requests(self, query: LeaveRequestQuery) -> int:
        statement = (
            select(func.count())
            .select_from(LeaveRequestRow)
            .outerjoin(RequestRow, RequestRow.id == LeaveRequestRow.approval_request_id)
            .where(*self._conditions(query))
        )
        return await self._session.scalar(statement) or 0

    async def mark_filed(
        self,
        request_id: UUID,
        *,
        approval_request_id: UUID,
        business_days_count: int,
        at: datetime,
    ) -> LeaveRequest:
        """Stamp the filing, and restate the days that were actually reserved.

        The count travels with the filing because the reservation recomputes it: a
        calendar edited between drafting and filing changes what the leave costs, and
        the row must state what was charged rather than what was previewed.
        """
        await self._write(
            request_id,
            approval_request_id=approval_request_id,
            business_days_count=business_days_count,
            submitted_at=at,
        )
        return await self._require(request_id)

    async def mark_approved(self, request_id: UUID, at: datetime) -> LeaveRequest:
        await self._write(request_id, approved_at=at)
        return await self._require(request_id)

    async def mark_withdrawn(self, request_id: UUID, at: datetime) -> LeaveRequest:
        await self._write(request_id, withdrawn_at=at)
        return await self._require(request_id)

    async def mark_settled(self, request_id: UUID, at: datetime) -> LeaveRequest:
        await self._write(request_id, settled_at=at)
        return await self._require(request_id)

    async def lock_next_unsettled(
        self, *, exclude: frozenset[UUID] = frozenset(), only: UUID | None = None
    ) -> LeaveRequest | None:
        statement = (
            select(LeaveRequestRow)
            .where(
                # Filed, and the balance not yet resolved. The engine's answer is *not*
                # part of this predicate: whether a request was approved is the
                # engine's rule, and SQL would have to re-express it.
                LeaveRequestRow.settled_at.is_(None),
                LeaveRequestRow.approval_request_id.is_not(None),
            )
            # Oldest first, so a crash is caught up in the order things were filed.
            .order_by(LeaveRequestRow.submitted_at, LeaveRequestRow.id)
            .limit(1)
            .with_for_update(skip_locked=True, of=LeaveRequestRow)
        )
        if only is not None:
            statement = statement.where(LeaveRequestRow.id == only)
        if exclude:
            statement = statement.where(LeaveRequestRow.id.not_in(exclude))
        row = await self._session.scalar(statement)
        return _to_request(row) if row is not None else None

    async def approval_status_of(self, request_id: UUID) -> ApprovalStatus | None:
        status = await self._session.scalar(
            select(RequestRow.status)
            .join(LeaveRequestRow, LeaveRequestRow.approval_request_id == RequestRow.id)
            .where(LeaveRequestRow.id == request_id)
        )
        return ApprovalStatus(status) if status is not None else None

    async def live_request_overlapping(
        self,
        employee_id: UUID,
        start_date: date,
        end_date: date,
        *,
        excluding: UUID | None = None,
    ) -> LeaveRequest | None:
        """An open request sharing a date with the range.

        "Open" is anything the engine has not closed and nobody has withdrawn: a
        rejected request releases its days and must not block a second attempt, while
        a draft — which holds nothing — is still the same intention and would become
        two overlapping leaves if both were filed.
        """
        statement = (
            select(LeaveRequestRow)
            .outerjoin(RequestRow, RequestRow.id == LeaveRequestRow.approval_request_id)
            .where(
                LeaveRequestRow.employee_id == employee_id,
                LeaveRequestRow.withdrawn_at.is_(None),
                LeaveRequestRow.start_date <= end_date,
                LeaveRequestRow.end_date >= start_date,
                # Not rejected: a rejection is final for that document and the way
                # forward is a new one, which is exactly what this check must allow.
                (RequestRow.id.is_(None)) | (RequestRow.status != "rejected"),
            )
            .order_by(LeaveRequestRow.start_date)
            .limit(1)
        )
        if excluding is not None:
            statement = statement.where(LeaveRequestRow.id != excluding)
        row = await self._session.scalar(statement)
        return _to_request(row) if row is not None else None

    async def approved_requests_covering(
        self, employee_id: UUID, from_date: date, to_date: date
    ) -> list[LeaveRequest]:
        rows = await self._session.scalars(
            select(LeaveRequestRow)
            .where(
                LeaveRequestRow.employee_id == employee_id,
                LeaveRequestRow.approved_at.is_not(None),
                LeaveRequestRow.withdrawn_at.is_(None),
                LeaveRequestRow.start_date <= to_date,
                LeaveRequestRow.end_date >= from_date,
            )
            .order_by(LeaveRequestRow.start_date)
        )
        return [_to_request(row) for row in rows]

    # --- plumbing -----------------------------------------------------------

    async def employee_exists(self, employee_id: UUID) -> bool:
        return bool(
            await self._session.scalar(
                select(func.count())
                .select_from(EmployeeRow)
                .where(EmployeeRow.id == employee_id)
            )
        )

    async def commit(self) -> None:
        await self._session.commit()

    async def rollback(self) -> None:
        await self._session.rollback()

    # --- internals ----------------------------------------------------------

    def _conditions(self, query: LeaveRequestQuery) -> Sequence[object]:
        # `Sequence` rather than `list[...]`: this class defines `list`, and an
        # annotation evaluated in the class body would find that method rather than
        # the builtin.
        conditions: list[object] = []
        if query.employee_id is not None:
            conditions.append(LeaveRequestRow.employee_id == query.employee_id)
        if query.state is not None:
            conditions.append(STATE_OF_ROW == query.state.value)
        return conditions

    async def _require(self, request_id: UUID) -> LeaveRequest:
        request = await self.get_request(request_id)
        if request is None:  # pragma: no cover - the row was just written
            raise LookupError(f"leave request {request_id} vanished mid-transaction")
        return request

    async def _write(self, request_id: UUID, **values: object) -> None:
        await self._session.execute(
            update(LeaveRequestRow)
            .where(LeaveRequestRow.id == request_id)
            .values(**values, updated_at=func.now())
            .execution_options(synchronize_session=False)
        )
        await self._session.flush()


__all__ = ["STATE_OF_ROW", "PostgresLeaveRepository"]
