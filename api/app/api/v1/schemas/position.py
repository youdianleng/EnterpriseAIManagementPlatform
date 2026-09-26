"""Position catalogue schemas."""

from uuid import UUID

from pydantic import BaseModel, Field

from app.api.v1.schemas.base import StrictModel


class PositionCreate(StrictModel):
    code: str = Field(min_length=1, max_length=64)
    title_es: str = Field(min_length=1, max_length=160)
    title_en: str = Field(min_length=1, max_length=160)
    department_id: UUID
    is_managerial: bool = False


class PositionUpdate(StrictModel):
    title_es: str | None = Field(default=None, min_length=1, max_length=160)
    title_en: str | None = Field(default=None, min_length=1, max_length=160)
    is_managerial: bool | None = None
    is_active: bool | None = None


class PositionRead(BaseModel):
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
    #: Live assignments, so an operator can see current usage at a glance.
    active_assignment_count: int
    #: Every assignment ever made. Non-zero is why a delete is refused.
    total_assignment_count: int
