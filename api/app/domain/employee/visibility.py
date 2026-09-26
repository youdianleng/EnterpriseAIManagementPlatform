"""Field-level visibility.

The ticket draws one line: a department colleague may read name, photo, position
and email; the address, staff number and contractual details are for the person,
HR, finance and compliance only. That decision lives here as a pure function so
it can be tested exhaustively without a database, and so the transport layer
never has to guess which fields to serialise.

Who the viewer *is* arrives as a `ViewerContext`. Ticket 11 replaces where that
context comes from; the rules below do not change.
"""

from dataclasses import dataclass
from uuid import UUID

from app.domain.employee.models import DirectoryEntry, Employee, EmployeePrivate

# Roles that may read withheld fields for anyone.
PRIVILEGED_ROLES = frozenset({"hr", "finance", "compliance"})

# Roles that may edit anyone's record and reassign the primary position.
MANAGING_ROLES = frozenset({"admin", "hr"})


@dataclass(slots=True, frozen=True)
class ViewerContext:
    """Who is asking.

    `department_ids` is the viewer's own department plus every descendant, which
    is what "a colleague in my department" is evaluated against. Callers pass the
    expanded set; the expansion itself belongs to the organisation module.
    """

    employee_id: UUID | None
    roles: frozenset[str] = frozenset()
    clearance_level: str = "low"
    department_ids: frozenset[UUID] = frozenset()

    @property
    def is_privileged(self) -> bool:
        return bool(self.roles & PRIVILEGED_ROLES)

    @property
    def can_manage(self) -> bool:
        return bool(self.roles & MANAGING_ROLES)


@dataclass(slots=True, frozen=True)
class Projection:
    """What a viewer receives for one employee."""

    employee: Employee
    # None means "withheld", never "unset" — the API omits the field entirely.
    private: EmployeePrivate | None
    allowed_fields: frozenset[str]
    visibility: str
    can_manage: bool

    def allows(self, field: str) -> bool:
        return field in self.allowed_fields


#: Fields every viewer receives, whatever their relationship to the subject.
BASE_FIELDS = frozenset(
    {"id", "first_name", "last_name", "preferred_name", "photo_path", "status"}
)

#: Fields added when the viewer shares a department with the subject.
DIRECTORY_FIELDS = frozenset({"email", "city", "country", "assignments"})

#: Fields added for the subject, HR, finance and compliance.
PRIVATE_FIELDS = frozenset({"address_line", "postal_code", "employee_no", "birth_date",
                            "emergency_contact", "hire_date", "termination_date"})


def resolve_visibility(
    viewer: ViewerContext, record
) -> Projection:  # noqa: ANN001 - EmployeeRecord
    """Decide what a viewer may receive about `record`.

    Ordered from most to least access:
      self       — the person themselves
      privileged — HR, finance and compliance
      directory  — shares a department with the viewer
      minimal    — outside the department: names and position only

    Administrators are not privileged here on purpose. The ticket withholds
    salary and address from everyone but the person, HR, finance and compliance;
    an administrator who needs to correct a record uses the managing endpoints,
    which are a separate decision from reading.
    """
    subject = record.employee
    is_self = viewer.employee_id is not None and viewer.employee_id == subject.id

    if is_self:
        return Projection(
            employee=subject,
            private=record.private,
            allowed_fields=BASE_FIELDS | DIRECTORY_FIELDS | PRIVATE_FIELDS,
            visibility="self",
            can_manage=viewer.can_manage,
        )

    if viewer.is_privileged:
        return Projection(
            employee=subject,
            private=record.private,
            allowed_fields=BASE_FIELDS | DIRECTORY_FIELDS | PRIVATE_FIELDS,
            visibility="privileged",
            can_manage=viewer.can_manage,
        )

    shared = any(
        assignment.department_id in viewer.department_ids
        for assignment in record.assignments
        if assignment.end_date is None
    )
    if shared:
        return Projection(
            employee=subject,
            private=None,
            allowed_fields=BASE_FIELDS | DIRECTORY_FIELDS,
            visibility="directory",
            can_manage=False,
        )

    # Outside the department: the deliberately minimal contact list. Job title and
    # department are on the record's primary assignment, so no extra lookup is
    # needed to render it.
    return Projection(
        employee=subject,
        private=None,
        allowed_fields=BASE_FIELDS
        | {"job_title_es", "job_title_en", "department_name_es", "department_name_en"},
        visibility="minimal",
        can_manage=False,
    )


def project_private(projection: Projection) -> dict[str, object]:
    """Serialise only the withheld fields the viewer is allowed to see."""
    if projection.private is None:
        return {}
    source = {
        "address_line": projection.private.address_line,
        "postal_code": projection.private.postal_code,
        "employee_no": projection.private.employee_no,
        "birth_date": projection.private.birth_date,
        "emergency_contact": projection.private.emergency_contact,
    }
    return {name: value for name, value in source.items() if projection.allows(name)}


def project_directory_row(viewer: ViewerContext, entry: DirectoryEntry) -> dict[str, object]:
    """One row of the contact list.

    Email is withheld for people outside the viewer's departments, and withheld
    means *absent*: the key is dropped rather than set to null, matching the
    profile endpoint so a client cannot read "not allowed" as "not recorded".
    """
    is_self = viewer.employee_id is not None and viewer.employee_id == entry.employee_id
    shared = entry.department_id is not None and entry.department_id in viewer.department_ids

    row: dict[str, object] = {
        "employee_id": entry.employee_id,
        "full_name": entry.full_name,
        "preferred_name": entry.preferred_name,
        "photo_path": entry.photo_path,
        "job_title_es": entry.job_title_es,
        "job_title_en": entry.job_title_en,
        "department_id": entry.department_id,
        "department_name_es": entry.department_name_es,
        "department_name_en": entry.department_name_en,
    }
    if is_self or shared:
        row["email"] = entry.email
    return row
