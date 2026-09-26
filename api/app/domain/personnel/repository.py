"""Persistence contract for personnel changes.

Implementations never commit on their own: a change and the audit record of what
it did belong to one transaction, and the *service* decides where that
transaction ends — `apply_due` commits once per change, so a failure applying one
document cannot take another with it. `commit` and `rollback` are on this
interface for exactly that caller.

`lock_next_due` is the interface's one unusual method, and it carries the reason
in its name: the row is locked `FOR UPDATE SKIP LOCKED` before anything is read
from it, so two workers running at the same time take different changes rather
than racing to apply one twice.
"""

from datetime import date, datetime
from typing import Any, Protocol
from uuid import UUID

from app.domain.personnel.models import (
    ChangeInput,
    ChangeQuery,
    ChangeState,
    ChangeStatus,
    PersonnelChange,
)


class PersonnelChangeRepository(Protocol):
    async def get(self, change_id: UUID) -> PersonnelChange | None: ...

    async def list(self, query: ChangeQuery) -> list[tuple[PersonnelChange, ChangeState]]:
        """The page, each row with the state the UI reads.

        The state is computed in the query rather than by asking the engine once
        per row: a page of fifty changes is one round trip, and the mapping is
        pinned to `models.state_of_change` by a test that runs both over the same
        corpus.
        """
        ...

    async def count(self, query: ChangeQuery) -> int: ...

    async def save(self, data: ChangeInput) -> PersonnelChange: ...

    async def mark_submitted(
        self, change_id: UUID, *, request_id: UUID, status: ChangeStatus
    ) -> None: ...

    async def mark_cancelled(
        self, change_id: UUID, *, at: datetime, by_employee_id: UUID, reason: str
    ) -> None: ...

    async def lock_next_due(
        self, *, on_date: date, exclude: frozenset[UUID]
    ) -> PersonnelChange | None:
        """The earliest change that is due and not yet applied, locked, or nothing.

        `exclude` is what the caller has already looked at in this run: a row that
        is due but not approved must not be handed back in a loop, and the caller
        is the only party that knows which those are.
        """
        ...

    async def mark_applied(
        self,
        change_id: UUID,
        *,
        at: datetime,
        applied_values: dict[str, Any],
        employee_id: UUID | None = None,
    ) -> None:
        """Record what was applied. `employee_id` is the one a join created."""
        ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


__all__ = ["PersonnelChangeRepository"]
