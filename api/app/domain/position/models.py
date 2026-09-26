"""Job position value objects."""

from dataclasses import dataclass
from uuid import UUID


@dataclass(slots=True, frozen=True)
class Position:
    id: UUID
    code: str
    title_es: str
    title_en: str
    department_id: UUID
    department_code: str
    department_name_es: str
    department_name_en: str
    is_managerial: bool
    is_active: bool
    #: Assignments currently in force; what an operator sees in the catalogue.
    active_assignment_count: int = 0
    #: Assignments ever made, including ended ones.
    #
    # Deletion is refused while this is non-zero, because the foreign key from
    # `employee_assignments` is RESTRICT: a position referenced only by history
    # would pass an active-only check and then fail inside the database, turning
    # a clear refusal into a 500.
    total_assignment_count: int = 0


@dataclass(slots=True)
class PositionInput:
    code: str
    title_es: str
    title_en: str
    department_id: UUID
    is_managerial: bool = False


@dataclass(slots=True)
class PositionPatch:
    title_es: str | None = None
    title_en: str | None = None
    is_managerial: bool | None = None
    is_active: bool | None = None

    def changes(self) -> dict[str, object]:
        return {
            name: value for name in self.__slots__ if (value := getattr(self, name)) is not None
        }
