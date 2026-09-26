"""PostgreSQL implementation of the personnel change repository.

Two things about this module are worth reading before its methods:

* **The state a row reports is computed in the query.** One `outerjoin` to
  `approval_requests` and a `CASE` gives a page of fifty changes its state in one
  round trip, instead of asking the engine fifty times. That is a second
  expression of the rule `models.state_of_change` states in Python, which is why
  `tests/test_personnel_changes.py` runs both over the same corpus of rows: two
  expressions of one rule are exactly the kind of pair that drifts.
* **`lock_next_due` locks before it reads.** `FOR UPDATE ... SKIP LOCKED` on the
  row it is about to hand out is what makes two workers take two different
  changes rather than both applying the same one; the lock is released by the
  commit that ends each change.
"""

from collections.abc import Sequence
from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.personnel.models import (
    ChangeInput,
    ChangeQuery,
    ChangeState,
    ChangeStatus,
    ChangeType,
    PersonnelChange,
    parse_changes,
    payload_of,
)
from app.models.approval import ApprovalRequest as RequestRow
from app.models.personnel import PersonnelChange as ChangeRow

#: The six states, in SQL. Literal statuses rather than the engine's enum: the
#: database compares against what the engine writes, and a rename on either side
#: has to fail a test rather than silently make a state unreachable.
STATE_OF_ROW = case(
    (ChangeRow.applied_at.is_not(None), ChangeState.APPLIED.value),
    (ChangeRow.cancelled_at.is_not(None), ChangeState.CANCELLED.value),
    (RequestRow.id.is_(None), ChangeState.DRAFT.value),
    (
        RequestRow.status.in_(("pending_first", "pending_second")),
        ChangeState.IN_APPROVAL.value,
    ),
    (RequestRow.status == "approved", ChangeState.APPROVED_PENDING.value),
    (RequestRow.status == "rejected", ChangeState.REJECTED.value),
    # `draft` (returned for correction) and `withdrawn`: the document is the
    # requester's own again.
    else_=ChangeState.DRAFT.value,
)


def _to_change(row: ChangeRow) -> PersonnelChange:
    change_type = ChangeType(row.change_type)
    return PersonnelChange(
        id=row.id,
        change_type=change_type,
        effective_date=row.effective_date,
        changes=parse_changes(
            change_type,
            (row.payload or {}).get("changes"),
            effective_date=row.effective_date,
        ),
        status=ChangeStatus(row.status),
        created_by_employee_id=row.created_by_employee_id,
        employee_id=row.employee_id,
        approval_request_id=row.approval_request_id,
        applied_values=row.applied_values,
        applied_at=row.applied_at,
        cancelled_at=row.cancelled_at,
        cancelled_by_employee_id=row.cancelled_by_employee_id,
        cancel_reason=row.cancel_reason,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class PostgresPersonnelChangeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- reads -------------------------------------------------------------

    async def get(self, change_id: UUID) -> PersonnelChange | None:
        row = await self._session.get(ChangeRow, change_id)
        return _to_change(row) if row is not None else None

    async def list(self, query: ChangeQuery) -> list[tuple[PersonnelChange, ChangeState]]:
        statement = (
            self._joined()
            .where(*self._conditions(query))
            # Newest first: a list of documents is read from the top, and the
            # applier's own order (by effective date) is not a reader's order.
            .order_by(ChangeRow.created_at.desc(), ChangeRow.id.desc())
            .limit(query.limit)
            .offset(query.offset)
        )
        rows = (await self._session.execute(statement)).all()
        return [(_to_change(row), ChangeState(state)) for row, state in rows]

    async def count(self, query: ChangeQuery) -> int:
        statement = (
            select(func.count())
            .select_from(ChangeRow)
            .outerjoin(RequestRow, RequestRow.id == ChangeRow.approval_request_id)
            .where(*self._conditions(query))
        )
        return await self._session.scalar(statement) or 0

    async def lock_next_due(
        self, *, on_date: date, exclude: frozenset[UUID]
    ) -> PersonnelChange | None:
        statement = (
            select(ChangeRow)
            .where(
                ChangeRow.applied_at.is_(None),
                ChangeRow.cancelled_at.is_(None),
                # A draft that was never filed can never be approved, so it is not
                # a candidate; the applier does not re-read the whole table.
                ChangeRow.approval_request_id.is_not(None),
                ChangeRow.effective_date <= on_date,
            )
            # Effective-date order, oldest first: after a week of downtime the
            # changes are applied in the order they were meant to happen, which is
            # what keeps a transfer-then-promotion chain meaningful.
            .order_by(ChangeRow.effective_date, ChangeRow.created_at, ChangeRow.id)
            .limit(1)
            .with_for_update(skip_locked=True, of=ChangeRow)
        )
        if exclude:
            statement = statement.where(ChangeRow.id.not_in(exclude))
        row = await self._session.scalar(statement)
        return _to_change(row) if row is not None else None

    # --- writes ------------------------------------------------------------

    async def save(self, data: ChangeInput) -> PersonnelChange:
        row = ChangeRow(
            change_type=data.change_type.value,
            employee_id=data.employee_id,
            effective_date=data.effective_date,
            payload=payload_of(data.change_type, data.changes),
            status=ChangeStatus.DRAFT.value,
            created_by_employee_id=data.created_by_employee_id,
        )
        self._session.add(row)
        await self._session.flush()
        return _to_change(row)

    async def mark_submitted(
        self, change_id: UUID, *, request_id: UUID, status: ChangeStatus
    ) -> None:
        await self._write(change_id, status=status.value, approval_request_id=request_id)

    async def mark_cancelled(
        self, change_id: UUID, *, at: datetime, by_employee_id: UUID, reason: str
    ) -> None:
        await self._write(
            change_id,
            status=ChangeStatus.CANCELLED.value,
            cancelled_at=at,
            cancelled_by_employee_id=by_employee_id,
            cancel_reason=reason,
        )

    async def mark_applied(
        self,
        change_id: UUID,
        *,
        at: datetime,
        applied_values: dict[str, Any],
        employee_id: UUID | None = None,
    ) -> None:
        values: dict[str, Any] = {
            "status": ChangeStatus.APPLIED.value,
            "applied_at": at,
            "applied_values": applied_values,
        }
        if employee_id is not None:
            # A join: the employee exists now, and the change is about them.
            values["employee_id"] = employee_id
        await self._write(change_id, **values)

    async def commit(self) -> None:
        await self._session.commit()

    async def rollback(self) -> None:
        await self._session.rollback()

    # --- internals ---------------------------------------------------------

    def _joined(self):
        return select(ChangeRow, STATE_OF_ROW).outerjoin(
            RequestRow, RequestRow.id == ChangeRow.approval_request_id
        )

    def _conditions(self, query: ChangeQuery) -> Sequence[Any]:
        # `Sequence` rather than `list[...]`: this class defines `list`, and an
        # annotation evaluated in the class body would find that method instead of
        # the builtin.
        conditions: list[Any] = []
        if query.employee_id is not None:
            conditions.append(ChangeRow.employee_id == query.employee_id)
        if query.change_type is not None:
            conditions.append(ChangeRow.change_type == query.change_type.value)
        if query.state is not None:
            conditions.append(STATE_OF_ROW == query.state.value)
        return conditions

    async def _write(self, change_id: UUID, **values: Any) -> None:
        await self._session.execute(
            update(ChangeRow)
            .where(ChangeRow.id == change_id)
            .values(**values, updated_at=func.now())
            .execution_options(synchronize_session=False)
        )
        await self._session.flush()


__all__ = ["STATE_OF_ROW", "PostgresPersonnelChangeRepository"]
