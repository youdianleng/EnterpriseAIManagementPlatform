"""Persistence contract for the approval engine.

Three things about this interface are load-bearing:

* **The step is the request's progress.** A request does not carry "current
  level"; the level is the one whose step is still pending in the current round.
  Storing both would be two answers to one question, and the second one is the
  one that goes stale.
* **What resolves an approver is fetched as primitives.** The engine reads the
  requester's primary position and its department's manager and applies the
  fallback itself, because the fallback order is a rule and rules live in
  `domain/` (`docs/architecture/codebase-design.md` §1).
* **Nothing commits.** The engine calls `commit` once, so a decision, its step and
  its audit record land together or not at all.
"""

from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.domain.approval.models import (
    ApprovalState,
    ApprovalStatus,
    PrimaryPosition,
    StepStatus,
)


class ApprovalRepository(Protocol):
    async def find_open(self, entity_type: str, entity_id: UUID) -> ApprovalState | None: ...

    async def latest_for(self, entity_type: str, entity_id: UUID) -> ApprovalState | None: ...

    async def get(self, request_id: UUID) -> ApprovalState | None: ...

    async def employee_exists(self, employee_id: UUID) -> bool: ...

    async def primary_position(self, employee_id: UUID) -> PrimaryPosition | None: ...

    async def department_manager(self, department_id: UUID) -> UUID | None: ...

    async def hr_employee_ids(self) -> list[UUID]: ...

    async def holds_hr_role(self, employee_id: UUID) -> bool: ...

    async def create_request(
        self,
        entity_type: str,
        entity_id: UUID,
        requester_employee_id: UUID,
        *,
        initiated_by: str,
        confirmed_by_user_id: UUID | None,
    ) -> UUID: ...

    async def save_submission(
        self,
        request_id: UUID,
        *,
        round_number: int,
        status: ApprovalStatus,
        submitted_at: datetime,
    ) -> None: ...

    async def save_outcome(
        self, request_id: UUID, *, status: ApprovalStatus, decided_at: datetime | None
    ) -> None: ...

    async def add_step(
        self,
        request_id: UUID,
        *,
        level: int,
        round_number: int,
        approver_employee_id: UUID | None,
        status: StepStatus,
        decided_at: datetime | None = None,
    ) -> UUID: ...

    async def decide_step(
        self, step_id: UUID, *, status: StepStatus, decided_at: datetime
    ) -> None: ...

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
    ) -> UUID: ...

    async def commit(self) -> None: ...


__all__ = ["ApprovalRepository"]
