"""Department API schemas.

These are the transport shapes, kept separate from the domain dataclasses so a
rule change does not silently alter the public contract.
"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.api.v1.schemas.base import StrictModel
from app.domain.org.models import ClearanceLevel


class DepartmentCreate(StrictModel):
    code: str = Field(min_length=1, max_length=64)
    name_es: str = Field(min_length=1, max_length=160)
    name_en: str = Field(min_length=1, max_length=160)
    parent_id: UUID | None = None
    clearance_level: ClearanceLevel = ClearanceLevel.LOW
    cost_center: str | None = Field(default=None, max_length=64)
    #: ISO 3166-2 (`ES-MD`). Decides which regional and local holidays apply to
    #: the people in this department (ticket 22); null means national only.
    region_code: str | None = Field(default=None, max_length=16)
    description_es: str | None = None
    description_en: str | None = None


class DepartmentUpdate(StrictModel):
    """Every field optional; absent means "leave unchanged"."""

    name_es: str | None = Field(default=None, min_length=1, max_length=160)
    name_en: str | None = Field(default=None, min_length=1, max_length=160)
    clearance_level: ClearanceLevel | None = None
    cost_center: str | None = Field(default=None, max_length=64)
    region_code: str | None = Field(default=None, max_length=16)
    description_es: str | None = None
    description_en: str | None = None
    is_active: bool | None = None


class DepartmentManager(StrictModel):
    """Who approves for the department. Explicitly nullable, because removing a
    manager is a real operation and the patch convention cannot express it."""

    employee_id: UUID | None = None


class DepartmentMove(StrictModel):
    # Explicitly nullable: moving a department to the root is a real operation.
    parent_id: UUID | None = None


class DepartmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    code: str
    name_es: str
    name_en: str
    parent_id: UUID | None
    path: str
    depth: int
    clearance_level: ClearanceLevel
    cost_center: str | None
    region_code: str | None
    manager_employee_id: UUID | None
    description_es: str | None
    description_en: str | None
    is_active: bool


class DepartmentNodeRead(BaseModel):
    department: DepartmentRead
    children: list["DepartmentNodeRead"] = []


class DepartmentTreeRead(BaseModel):
    roots: list[DepartmentNodeRead]
    total: int
    max_depth: int


DepartmentNodeRead.model_rebuild()
