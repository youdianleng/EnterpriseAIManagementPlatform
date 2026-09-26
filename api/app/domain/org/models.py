"""Organisation value objects.

Plain dataclasses rather than ORM rows: the domain layer must be usable without
a database, and a test for "which departments are descendants" should not need
one either.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID


class ClearanceLevel(StrEnum):
    """Document classification, and a department's default for its staff."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def rank(self) -> int:
        return {"low": 0, "medium": 1, "high": 2}[self.value]


@dataclass(slots=True, frozen=True)
class Department:
    id: UUID
    code: str
    name_es: str
    name_en: str
    parent_id: UUID | None
    path: str
    depth: int
    clearance_level: ClearanceLevel
    cost_center: str | None
    manager_employee_id: UUID | None
    description_es: str | None
    description_en: str | None
    is_active: bool


@dataclass(slots=True, frozen=True)
class DepartmentNode:
    """A department plus its children, for rendering the tree."""

    department: Department
    children: tuple["DepartmentNode", ...] = ()


@dataclass(slots=True, frozen=True)
class DepartmentTree:
    roots: tuple[DepartmentNode, ...] = ()
    # Total number of departments in the tree, including every nested level.
    total: int = 0

    @property
    def max_depth(self) -> int:
        """Deepest level present, with roots at 0."""

        def walk(node: DepartmentNode) -> int:
            if not node.children:
                return node.department.depth
            return max(walk(child) for child in node.children)

        return max((walk(root) for root in self.roots), default=-1)


@dataclass(slots=True)
class DepartmentInput:
    """Fields accepted when creating a department.

    `parent_id` is the only structural input; `path` and `depth` are derived by
    the service and are deliberately not settable by a caller.
    """

    code: str
    name_es: str
    name_en: str
    parent_id: UUID | None = None
    clearance_level: ClearanceLevel = ClearanceLevel.LOW
    cost_center: str | None = None
    description_es: str | None = None
    description_en: str | None = None


@dataclass(slots=True)
class DepartmentPatch:
    """Fields accepted when updating a department. Absent means "leave alone"."""

    name_es: str | None = field(default=None)
    name_en: str | None = field(default=None)
    clearance_level: ClearanceLevel | None = field(default=None)
    cost_center: str | None = field(default=None)
    description_es: str | None = field(default=None)
    description_en: str | None = field(default=None)
    is_active: bool | None = field(default=None)

    def changes(self) -> dict[str, object]:
        """Only the fields that were actually supplied.

        Explicit `None` is meaningful — it clears an optional field — so the
        decision cannot be based on the value being falsy.
        """
        return {
            name: value
            for name in self.__slots__
            if (value := getattr(self, name)) is not None
        }
