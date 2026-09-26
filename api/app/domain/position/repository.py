"""Persistence contract for the position catalogue.

Implementations never commit; the service owns the transaction.
"""

from typing import Protocol
from uuid import UUID

from app.domain.position.models import Position, PositionInput, PositionPatch


class PositionRepository(Protocol):
    async def get(self, position_id: UUID) -> Position | None: ...

    async def list_positions(
        self, *, department_id: UUID | None = None, include_inactive: bool = True
    ) -> list[Position]: ...

    async def code_exists(
        self, *, department_id: UUID, code: str, exclude_id: UUID | None = None
    ) -> bool: ...

    async def count_assignments(self, position_id: UUID, *, active_only: bool) -> int: ...

    async def save(self, data: PositionInput) -> Position: ...

    async def update(self, position_id: UUID, patch: PositionPatch) -> Position: ...

    async def delete(self, position_id: UUID) -> None: ...

    async def commit(self) -> None: ...
