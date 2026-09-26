"""Employee API schemas.

Two shapes for one resource: the directory projection, which omits withheld
fields entirely, and the full profile. Because withheld values are omitted rather
than nulled, a client cannot mistake "not recorded" for "not allowed to know".
"""

from datetime import date
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from app.api.v1.schemas.base import StrictModel
from app.domain.employee.models import EmploymentStatus


class EmergencyContact(StrictModel):
    name: str = Field(min_length=1, max_length=160)
    relationship: str | None = Field(default=None, max_length=80)
    phone: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=200)


class EmployeePrivateIn(StrictModel):
    """Only these fields are accepted. Anything else is rejected at the edge."""

    address_line: str | None = None
    postal_code: str | None = Field(default=None, max_length=16)
    employee_no: str | None = Field(default=None, max_length=32)
    birth_date: date | None = None
    emergency_contact: EmergencyContact | None = None


class EmployeePrivateRead(BaseModel):
    address_line: str | None = None
    postal_code: str | None = None
    employee_no: str | None = None
    birth_date: date | None = None
    emergency_contact: EmergencyContact | None = None


class EmployeeCreate(StrictModel):
    first_name: str = Field(min_length=1, max_length=80)
    last_name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    hire_date: date
    preferred_name: str | None = Field(default=None, max_length=80)
    photo_path: str | None = Field(default=None, max_length=400)
    city: str | None = Field(default=None, max_length=120)
    country: str | None = Field(default=None, max_length=80)
    status: EmploymentStatus = EmploymentStatus.ACTIVE
    termination_date: date | None = None
    private: EmployeePrivateIn | None = None


class EmployeeUpdate(StrictModel):
    first_name: str | None = Field(default=None, min_length=1, max_length=80)
    last_name: str | None = Field(default=None, min_length=1, max_length=120)
    email: EmailStr | None = None
    preferred_name: str | None = Field(default=None, max_length=80)
    photo_path: str | None = Field(default=None, max_length=400)
    city: str | None = Field(default=None, max_length=120)
    country: str | None = Field(default=None, max_length=80)
    hire_date: date | None = None
    termination_date: date | None = None
    status: EmploymentStatus | None = None


class AssignmentCreate(StrictModel):
    """`is_primary` is absent by design: the first position is primary, and
    promoting a different one is an administrative action."""

    department_id: UUID
    job_position_id: UUID
    start_date: date
    is_part_time: bool = False
    manager_employee_id: UUID | None = None
    notification_override_employee_id: UUID | None = None
    end_date: date | None = None


class AssignmentRead(BaseModel):
    id: UUID
    department_id: UUID
    department_code: str
    department_name_es: str
    department_name_en: str
    job_position_id: UUID
    job_position_code: str
    job_title_es: str
    job_title_en: str
    is_primary: bool
    is_part_time: bool
    # The effective approver: the explicit one, or the department's manager.
    manager_employee_id: UUID | None
    notification_override_employee_id: UUID | None
    effective_approver_employee_id: UUID | None
    start_date: date
    end_date: date | None


class DirectoryEntryRead(BaseModel):
    employee_id: UUID
    full_name: str
    preferred_name: str | None = None
    photo_path: str | None = None
    job_title_es: str | None = None
    job_title_en: str | None = None
    department_id: UUID | None = None
    department_name_es: str | None = None
    department_name_en: str | None = None
    # Present only for the person themselves and for colleagues in the viewer's
    # departments.
    email: str | None = None


class EmployeeRead(BaseModel):
    """Profile as the viewer is allowed to see it.

    Withheld fields are omitted, so the response shape varies with the viewer.
    `visibility` states which projection was applied, which makes an unexpected
    value obvious in a log rather than silent.
    """

    id: UUID
    first_name: str
    last_name: str
    preferred_name: str | None = None
    photo_path: str | None = None
    status: EmploymentStatus
    visibility: str
    can_manage: bool
    city: str | None = None
    country: str | None = None
    email: str | None = None
    hire_date: date | None = None
    termination_date: date | None = None
    private: EmployeePrivateRead | None = None
    assignments: list[AssignmentRead] | None = None
