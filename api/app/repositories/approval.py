"""PostgreSQL implementation of the approval repository.

The interesting parts are the two reads that resolve an approver and the one
write that must not be a read:

* `primary_position` and `department_manager` are separate on purpose. The
  fallback between them is a rule, so it lives in the service; this module only
  fetches the two facts it is applied to.
* `hr_employee_ids` counts enabled accounts, because a deactivated account cannot
  sign in and therefore cannot decide anything. Counting one would let a request
  be filed that nobody can finish.
* Reads that build an `ApprovalState` load the request, its steps and its
  decisions in full. An approval request has a handful of rows, and a caller that
  asked for "the state" should not have to ask twice for the history that makes
  it readable.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.approval.models import (
    OPEN_STATUSES,
    ApprovalDecision,
    ApprovalRequest,
    ApprovalState,
    ApprovalStatus,
    ApprovalStep,
    PrimaryPosition,
    StepStatus,
)
from app.models.account import User as UserRow
from app.models.approval import ApprovalDecision as DecisionRow
from app.models.approval import ApprovalRequest as RequestRow
from app.models.approval import ApprovalStep as StepRow
from app.models.employee import Employee as EmployeeRow
from app.models.employee import EmployeeAssignment as AssignmentRow
from app.models.org import Department as DepartmentRow


def _to_request(row: RequestRow) -> ApprovalRequest:
    return ApprovalRequest(
        id=row.id,
        entity_type=row.entity_type,
        entity_id=row.entity_id,
        requester_employee_id=row.requester_employee_id,
        status=ApprovalStatus(row.status),
        round=row.round,
        submitted_at=row.submitted_at,
        decided_at=row.decided_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        initiated_by=row.initiated_by,
        confirmed_by_user_id=row.confirmed_by_user_id,
    )


def _to_step(row: StepRow) -> ApprovalStep:
    return ApprovalStep(
        id=row.id,
        request_id=row.request_id,
        level=row.level,
        round=row.round,
        approver_employee_id=row.approver_employee_id,
        status=StepStatus(row.status),
        decided_at=row.decided_at,
        created_at=row.created_at,
    )


def _to_decision(row: DecisionRow) -> ApprovalDecision:
    return ApprovalDecision(
        id=row.id,
        request_id=row.request_id,
        level=row.level,
        round=row.round,
        approver_employee_id=row.approver_employee_id,
        decision=StepStatus(row.decision),
        comment=row.comment,
        decided_at=row.decided_at,
    )


class PostgresApprovalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- reads -------------------------------------------------------------

    async def find_open(self, entity_type: str, entity_id: UUID) -> ApprovalState | None:
        row = await self._session.scalar(
            select(RequestRow).where(
                RequestRow.entity_type == entity_type,
                RequestRow.entity_id == entity_id,
                RequestRow.status.in_([status.value for status in OPEN_STATUSES]),
            )
        )
        return await self._state(row) if row is not None else None

    async def latest_for(self, entity_type: str, entity_id: UUID) -> ApprovalState | None:
        row = await self._session.scalar(
            select(RequestRow)
            .where(RequestRow.entity_type == entity_type, RequestRow.entity_id == entity_id)
            # Newest request wins. `id` breaks a tie that a same-transaction pair
            # could in principle produce; the engine never creates two.
            .order_by(RequestRow.created_at.desc(), RequestRow.id.desc())
            .limit(1)
        )
        return await self._state(row) if row is not None else None

    async def get(self, request_id: UUID) -> ApprovalState | None:
        row = await self._session.get(RequestRow, request_id)
        return await self._state(row) if row is not None else None

    async def employee_exists(self, employee_id: UUID) -> bool:
        return bool(
            await self._session.scalar(
                select(func.count()).select_from(EmployeeRow).where(EmployeeRow.id == employee_id)
            )
        )

    async def primary_position(self, employee_id: UUID) -> PrimaryPosition | None:
        """The active assignment flagged primary, if there is one.

        "Active" is `end_date IS NULL`, which is what the rest of the system means
        by it (`employee_assignments` carries a partial unique index on exactly
        that). The engine has no `on_date` in its interface, so it resolves the
        route against the organisation as it stands.
        """
        row = (
            await self._session.execute(
                select(AssignmentRow.department_id, AssignmentRow.manager_employee_id).where(
                    AssignmentRow.employee_id == employee_id,
                    AssignmentRow.is_primary.is_(True),
                    AssignmentRow.end_date.is_(None),
                )
            )
        ).first()
        if row is None:
            return None
        return PrimaryPosition(department_id=row[0], manager_employee_id=row[1])

    async def department_manager(self, department_id: UUID) -> UUID | None:
        return await self._session.scalar(
            select(DepartmentRow.manager_employee_id).where(DepartmentRow.id == department_id)
        )

    async def hr_employee_ids(self) -> list[UUID]:
        rows = await self._session.scalars(
            select(UserRow.employee_id).where(
                UserRow.is_active.is_(True), UserRow.roles.contains(["hr"])
            )
        )
        return list(rows)

    async def holds_hr_role(self, employee_id: UUID) -> bool:
        return bool(
            await self._session.scalar(
                select(func.count())
                .select_from(UserRow)
                .where(
                    UserRow.employee_id == employee_id,
                    UserRow.is_active.is_(True),
                    UserRow.roles.contains(["hr"]),
                )
            )
        )

    # --- writes ------------------------------------------------------------

    async def create_request(
        self,
        entity_type: str,
        entity_id: UUID,
        requester_employee_id: UUID,
        *,
        initiated_by: str,
        confirmed_by_user_id: UUID | None,
    ) -> UUID:
        row = RequestRow(
            entity_type=entity_type,
            entity_id=entity_id,
            requester_employee_id=requester_employee_id,
            status=ApprovalStatus.DRAFT.value,
            round=1,
            initiated_by=initiated_by,
            confirmed_by_user_id=confirmed_by_user_id,
        )
        self._session.add(row)
        await self._session.flush()
        return row.id

    async def save_submission(
        self,
        request_id: UUID,
        *,
        round_number: int,
        status: ApprovalStatus,
        submitted_at: datetime,
    ) -> None:
        await self._session.execute(
            update(RequestRow)
            .where(RequestRow.id == request_id)
            .values(
                round=round_number,
                status=status.value,
                submitted_at=submitted_at,
                # A fresh attempt is undecided by definition.
                decided_at=None,
                updated_at=func.now(),
            )
            .execution_options(synchronize_session=False)
        )

    async def save_outcome(
        self, request_id: UUID, *, status: ApprovalStatus, decided_at: datetime | None
    ) -> None:
        await self._session.execute(
            update(RequestRow)
            .where(RequestRow.id == request_id)
            .values(status=status.value, decided_at=decided_at, updated_at=func.now())
            .execution_options(synchronize_session=False)
        )

    async def add_step(
        self,
        request_id: UUID,
        *,
        level: int,
        round_number: int,
        approver_employee_id: UUID | None,
        status: StepStatus,
        decided_at: datetime | None = None,
    ) -> UUID:
        row = StepRow(
            request_id=request_id,
            level=level,
            round=round_number,
            approver_employee_id=approver_employee_id,
            status=status.value,
            decided_at=decided_at,
        )
        self._session.add(row)
        await self._session.flush()
        return row.id

    async def decide_step(
        self, step_id: UUID, *, status: StepStatus, decided_at: datetime
    ) -> None:
        await self._session.execute(
            update(StepRow)
            .where(StepRow.id == step_id)
            .values(status=status.value, decided_at=decided_at)
            .execution_options(synchronize_session=False)
        )

    async def add_decision(
        self,
        *,
        request_id: UUID,
        level: int,
        round_number: int,
        approver_employee_id: UUID,
        decision: StepStatus,
        comment: str | None,
        decided_at: datetime,
    ) -> UUID:
        row = DecisionRow(
            request_id=request_id,
            level=level,
            round=round_number,
            approver_employee_id=approver_employee_id,
            decision=decision.value,
            comment=comment,
            decided_at=decided_at,
        )
        self._session.add(row)
        await self._session.flush()
        return row.id

    async def commit(self) -> None:
        await self._session.commit()

    # --- internals ---------------------------------------------------------

    async def _state(self, row: RequestRow) -> ApprovalState:
        steps = await self._session.scalars(
            select(StepRow)
            .where(StepRow.request_id == row.id)
            .order_by(StepRow.round, StepRow.level)
        )
        decisions = await self._session.scalars(
            select(DecisionRow)
            .where(DecisionRow.request_id == row.id)
            .order_by(DecisionRow.round, DecisionRow.level, DecisionRow.decided_at)
        )
        return ApprovalState(
            request=_to_request(row),
            steps=tuple(_to_step(step) for step in steps),
            decisions=tuple(_to_decision(decision) for decision in decisions),
        )


__all__ = ["PostgresApprovalRepository"]
