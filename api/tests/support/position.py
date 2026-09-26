"""In-memory position repository for the catalogue rule tests."""

from uuid import UUID, uuid4

from app.domain.org.models import Department
from app.domain.position.models import Position, PositionInput, PositionPatch


class InMemoryPositionRepository:
    def __init__(self, departments: dict[UUID, Department] | None = None) -> None:
        self.positions: dict[UUID, Position] = {}
        self.departments = departments or {}
        self.assignment_counts: dict[UUID, int] = {}
        self.ended_counts: dict[UUID, int] = {}
        self.commits = 0

    def seed(
        self,
        department: Department,
        *,
        code: str = "tech",
        is_active: bool = True,
        is_managerial: bool = False,
        assignment_count: int = 0,
        ended_assignment_count: int = 0,
    ) -> Position:
        position = Position(
            id=uuid4(),
            code=code,
            title_es=f"{code} es",
            title_en=f"{code} en",
            department_id=department.id,
            department_code=department.code,
            department_name_es=department.name_es,
            department_name_en=department.name_en,
            is_managerial=is_managerial,
            is_active=is_active,
            active_assignment_count=assignment_count,
            total_assignment_count=assignment_count + ended_assignment_count,
        )
        self.positions[position.id] = position
        self.assignment_counts[position.id] = assignment_count
        self.ended_counts[position.id] = ended_assignment_count
        return position

    def _replace(self, position: Position) -> None:
        self.positions[position.id] = position

    async def get(self, position_id: UUID) -> Position | None:
        return self.positions.get(position_id)

    async def list_positions(
        self, *, department_id: UUID | None = None, include_inactive: bool = True
    ) -> list[Position]:
        rows = [
            position
            for position in self.positions.values()
            if include_inactive or position.is_active
        ]
        if department_id is not None:
            rows = [position for position in rows if position.department_id == department_id]
        return sorted(rows, key=lambda position: (position.department_code, position.code))

    async def code_exists(
        self, *, department_id: UUID, code: str, exclude_id: UUID | None = None
    ) -> bool:
        return any(
            position.department_id == department_id
            and position.code == code
            and position.id != exclude_id
            for position in self.positions.values()
        )

    async def count_assignments(self, position_id: UUID, *, active_only: bool) -> int:
        if active_only:
            return self.assignment_counts.get(position_id, 0)
        return self.assignment_counts.get(position_id, 0) + self.ended_counts.get(position_id, 0)

    async def save(self, data: PositionInput) -> Position:
        department = self.departments[data.department_id]
        position = Position(
            id=uuid4(),
            code=data.code,
            title_es=data.title_es,
            title_en=data.title_en,
            department_id=data.department_id,
            department_code=department.code,
            department_name_es=department.name_es,
            department_name_en=department.name_en,
            is_managerial=data.is_managerial,
            is_active=True,
            active_assignment_count=0,
            total_assignment_count=0,
        )
        self._replace(position)
        return position

    async def update(self, position_id: UUID, patch: PositionPatch) -> Position:
        current = self.positions[position_id]
        updated = Position(
            **{
                **{
                    field: getattr(current, field)
                    for field in (
                        "id",
                        "code",
                        "title_es",
                        "title_en",
                        "department_id",
                        "department_code",
                        "department_name_es",
                        "department_name_en",
                        "is_managerial",
                        "is_active",
                        "active_assignment_count",
                        "total_assignment_count",
                    )
                },
                **patch.changes(),
            }
        )
        self._replace(updated)
        return updated

    async def delete(self, position_id: UUID) -> None:
        del self.positions[position_id]

    async def commit(self) -> None:
        self.commits += 1


class StubDepartments:
    """The organisation lookup the service needs: one method."""

    def __init__(self, departments: dict[UUID, Department]) -> None:
        self._departments = departments

    async def get(self, department_id: UUID) -> Department | None:
        return self._departments.get(department_id)


__all__ = ["InMemoryPositionRepository", "StubDepartments"]
