"""Position catalogue rules.

Two rules carry the weight, and both exist to keep assignment validation
meaningful later:

* A code is unique within its department rather than globally. Codes describe a
  role inside a team, so `manager` legitimately exists under several
  departments.
* A position that is in use cannot be deleted, only deactivated. Deleting it
  would leave historical assignments pointing at nothing, which is the same
  reasoning that keeps ended assignments readable.

`departments` is injected as the organisation repository, so this module never
imports another domain's implementation.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditAction, record
from app.domain.errors import DomainError
from app.domain.position.errors import PositionErrorCode
from app.domain.position.models import Position, PositionInput, PositionPatch
from app.domain.position.repository import PositionRepository


class PositionService:
    def __init__(
        self,
        repository: PositionRepository,
        departments,  # noqa: ANN001
        session: AsyncSession | None = None,
    ) -> None:
        self._repository = repository
        self._departments = departments
        # Optional for unit tests of the catalogue rules; every write path in the
        # application passes one, because every write is audited.
        self._session = session

    async def _audit(
        self,
        action: AuditAction,
        position: Position,
        *,
        before: dict[str, object] | None = None,
        after: dict[str, object] | None = None,
    ) -> None:
        if self._session is None:
            return
        await record(
            self._session,
            action=action,
            entity_type="job_position",
            entity_id=position.id,
            before=before,
            after=after,
        )

    async def get(self, position_id: UUID) -> Position:
        position = await self._repository.get(position_id)
        if position is None:
            raise DomainError(
                PositionErrorCode.POSITION_NOT_FOUND, detail=f"unknown position {position_id}"
            )
        return position

    async def list_positions(
        self, *, department_id: UUID | None = None, include_inactive: bool = True
    ) -> list[Position]:
        return await self._repository.list_positions(
            department_id=department_id, include_inactive=include_inactive
        )

    async def create(self, data: PositionInput) -> Position:
        department = await self._departments.get(data.department_id)
        if department is None or not department.is_active:
            raise DomainError(
                PositionErrorCode.POSITION_DEPARTMENT_INVALID,
                detail=f"department {data.department_id} does not exist or is inactive",
            )
        if await self._repository.code_exists(department_id=data.department_id, code=data.code):
            raise DomainError(
                PositionErrorCode.POSITION_CODE_TAKEN,
                detail=f"code {data.code} already exists in that department",
            )

        position = await self._repository.save(data)
        await self._audit(
            AuditAction.POSITION_CREATED,
            position,
            after={
                "code": position.code,
                "department_id": str(position.department_id),
                "is_managerial": position.is_managerial,
            },
        )
        await self._repository.commit()
        return position

    async def update(self, position_id: UUID, patch: PositionPatch) -> Position:
        before = await self.get(position_id)
        position = await self._repository.update(position_id, patch)
        changes = patch.changes()
        await self._audit(
            AuditAction.POSITION_UPDATED,
            position,
            before={name: getattr(before, name) for name in changes},
            after={name: getattr(position, name) for name in changes},
        )
        await self._repository.commit()
        return position

    async def deactivate(self, position_id: UUID) -> Position:
        """Soft removal: existing assignments keep resolving, new ones are refused."""
        await self.get(position_id)
        position = await self._repository.update(position_id, PositionPatch(is_active=False))
        await self._audit(
            AuditAction.POSITION_DEACTIVATED,
            position,
            before={"is_active": True},
            after={"is_active": False},
        )
        await self._repository.commit()
        return position

    async def delete(self, position_id: UUID) -> None:
        position = await self.get(position_id)
        # Any reference blocks deletion, not just a live one: the assignment
        # table's foreign key is RESTRICT, so an active-only check would let the
        # call through and fail inside the database.
        referenced = await self._repository.count_assignments(position_id, active_only=False)
        if referenced:
            active = await self._repository.count_assignments(position_id, active_only=True)
            raise DomainError(
                PositionErrorCode.POSITION_IN_USE,
                detail=(
                    f"{referenced} assignments reference {position.code} ({active} active); "
                    "deactivate it instead of deleting"
                ),
            )

        await self._repository.delete(position_id)
        await self._audit(
            AuditAction.POSITION_DELETED,
            position,
            before={"code": position.code},
        )
        await self._repository.commit()


__all__ = ["PositionService"]
