"""Employee domain models.

An employee is not one record: it is a basic profile the directory may read, a
separate set of private details, and a list of position assignments. Keeping
those apart here is what lets `visibility.py` say what a viewer receives without
the rules leaking into the queries.
"""

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from uuid import UUID


class EmploymentStatus(StrEnum):
    ACTIVE = "active"
    ON_LEAVE = "on_leave"
    TERMINATED = "terminated"


#: The one status that removes somebody from every list of people: the directory,
#: a department's headcount and the pool of possible approvers. Named once, in
#: SQL as well as in Python, so the three exclusions cannot drift apart.
TERMINATED_STATUS = EmploymentStatus.TERMINATED.value


@dataclass(slots=True, frozen=True)
class Employee:
    id: UUID
    first_name: str
    last_name: str
    preferred_name: str | None
    email: str
    photo_path: str | None
    city: str | None
    country: str | None
    hire_date: date
    termination_date: date | None
    status: EmploymentStatus

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


@dataclass(slots=True, frozen=True)
class EmployeePrivate:
    """Withheld from the directory.

    Deliberately narrow. ID numbers, bank details, health data and biometrics do
    not exist anywhere in this system, and a database constraint enforces that.
    """

    address_line: str | None = None
    postal_code: str | None = None
    employee_no: str | None = None
    birth_date: date | None = None
    emergency_contact: dict | None = None


@dataclass(slots=True, frozen=True)
class JobPosition:
    """The catalogue entry a person holds, not the position a person *is*."""

    id: UUID
    code: str
    title_es: str
    title_en: str
    department_id: UUID
    is_managerial: bool
    is_active: bool


@dataclass(slots=True, frozen=True)
class Assignment:
    id: UUID
    employee_id: UUID
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
    # Explicit approver for this position, when it differs from the manager.
    manager_employee_id: UUID | None
    notification_override_employee_id: UUID | None
    start_date: date
    end_date: date | None


@dataclass(slots=True, frozen=True)
class EmployeeRecord:
    """Everything known about one employee; the viewer decides what is returned."""

    employee: Employee
    private: EmployeePrivate
    assignments: tuple[Assignment, ...] = ()

    @property
    def primary_assignment(self) -> Assignment | None:
        """The active primary position, or the most recent active one.

        Roots or historical rows may leave the primary flag on an ended
        assignment, so ended rows are skipped before falling back.
        """
        active = [a for a in self.assignments if a.end_date is None]
        for assignment in active:
            if assignment.is_primary:
                return assignment
        return active[0] if active else None


@dataclass(slots=True)
class EmployeeInput:
    first_name: str
    last_name: str
    email: str
    hire_date: date
    preferred_name: str | None = None
    photo_path: str | None = None
    city: str | None = None
    country: str | None = None
    status: EmploymentStatus = EmploymentStatus.ACTIVE
    termination_date: date | None = None
    private: EmployeePrivate = field(default_factory=EmployeePrivate)


@dataclass(slots=True)
class EmployeePatch:
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    preferred_name: str | None = None
    photo_path: str | None = None
    city: str | None = None
    country: str | None = None
    hire_date: date | None = None
    termination_date: date | None = None
    status: EmploymentStatus | None = None

    def changes(self) -> dict[str, object]:
        return {
            name: value for name in self.__slots__ if (value := getattr(self, name)) is not None
        }


@dataclass(slots=True)
class AssignmentInput:
    """A caller's request to attach a position.

    `is_primary` is deliberately absent: the first assignment becomes primary and
    only ticket 09's admin path may change that, so a client cannot promote
    itself.
    """

    department_id: UUID
    job_position_id: UUID
    start_date: date
    is_part_time: bool = False
    manager_employee_id: UUID | None = None
    notification_override_employee_id: UUID | None = None
    end_date: date | None = None


@dataclass(slots=True)
class DirectoryEntry:
    """A single row of the organisation directory."""

    employee_id: UUID
    full_name: str
    preferred_name: str | None
    photo_path: str | None
    email: str | None
    job_title_es: str | None
    job_title_en: str | None
    department_id: UUID | None
    department_name_es: str | None
    department_name_en: str | None
