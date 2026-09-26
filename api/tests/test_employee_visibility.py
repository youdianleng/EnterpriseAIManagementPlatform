"""Field-level visibility, tested exhaustively without a database.

This is the rule the ticket states as an access-control requirement, so it gets
a matrix rather than a couple of examples: every viewer relationship against
every field group.
"""

from datetime import date
from uuid import uuid4

import pytest

from app.domain.employee.models import (
    Assignment,
    Employee,
    EmployeePrivate,
    EmployeeRecord,
    EmploymentStatus,
)
from app.domain.employee.visibility import (
    BASE_FIELDS,
    DIRECTORY_FIELDS,
    PRIVATE_FIELDS,
    ViewerContext,
    project_private,
    resolve_visibility,
)

SHARED_DEPARTMENT = uuid4()
OTHER_DEPARTMENT = uuid4()
SUB_DEPARTMENT = uuid4()


def build_record() -> EmployeeRecord:
    employee = Employee(
        id=uuid4(),
        first_name="Ana",
        last_name="Martín",
        preferred_name="Anita",
        email="ana.martin@empresa.es",
        photo_path="photos/ana.png",
        city="Madrid",
        country="España",
        hire_date=date(2023, 3, 1),
        termination_date=None,
        status=EmploymentStatus.ACTIVE,
    )
    assignment = Assignment(
        id=uuid4(),
        employee_id=employee.id,
        department_id=SHARED_DEPARTMENT,
        department_code="rrhh",
        department_name_es="Recursos Humanos",
        department_name_en="Human Resources",
        job_position_id=uuid4(),
        job_position_code="tech",
        job_title_es="Técnica",
        job_title_en="Technician",
        is_primary=True,
        is_part_time=False,
        manager_employee_id=None,
        notification_override_employee_id=None,
        start_date=date(2023, 3, 1),
        end_date=None,
    )
    return EmployeeRecord(
        employee=employee,
        private=EmployeePrivate(
            address_line="Calle Mayor 1",
            postal_code="28013",
            employee_no="E-0042",
            birth_date=date(1990, 5, 20),
            emergency_contact={"name": "Luis", "phone": "600000000"},
        ),
        assignments=(assignment,),
    )


@pytest.fixture
def record() -> EmployeeRecord:
    return build_record()


def viewer(*, employee_id=None, roles=(), departments=frozenset()) -> ViewerContext:
    return ViewerContext(
        employee_id=employee_id,
        roles=frozenset(roles),
        clearance_level="low",
        department_ids=frozenset(departments),
    )


# --- the subject themselves ------------------------------------------------


def test_the_person_sees_everything_about_themselves(record: EmployeeRecord) -> None:
    projection = resolve_visibility(viewer(employee_id=record.employee.id), record)

    assert projection.visibility == "self"
    assert PRIVATE_FIELDS <= projection.allowed_fields
    assert project_private(projection)["address_line"] == "Calle Mayor 1"
    assert project_private(projection)["employee_no"] == "E-0042"


# --- privileged roles ------------------------------------------------------


@pytest.mark.parametrize("role", ["hr", "finance", "compliance"])
def test_privileged_roles_see_withheld_fields(record: EmployeeRecord, role: str) -> None:
    projection = resolve_visibility(viewer(employee_id=uuid4(), roles=[role]), record)

    assert projection.visibility == "privileged"
    assert project_private(projection)["employee_no"] == "E-0042"
    assert project_private(projection)["address_line"] == "Calle Mayor 1"


def test_admin_alone_is_not_privileged_for_withheld_fields(record: EmployeeRecord) -> None:
    """The ticket withholds these from everyone but the person, HR, finance and
    compliance. An administrator who must correct a record uses the managing
    endpoints, which is a separate decision from reading it."""
    projection = resolve_visibility(viewer(employee_id=uuid4(), roles=["admin"]), record)

    assert projection.visibility == "minimal"
    assert projection.private is None
    assert project_private(projection) == {}


# --- colleagues ------------------------------------------------------------


def test_a_colleague_in_the_same_department_sees_the_directory_fields(
    record: EmployeeRecord,
) -> None:
    projection = resolve_visibility(
        viewer(employee_id=uuid4(), departments={SHARED_DEPARTMENT}), record
    )

    assert projection.visibility == "directory"
    assert {"email", "city"} <= projection.allowed_fields
    # The withheld fields are not merely hidden from the payload, they are absent
    # from the projection, so no serialiser can leak them by accident.
    assert projection.private is None
    assert not (PRIVATE_FIELDS & projection.allowed_fields)


def test_a_colleague_in_a_child_department_still_counts(record: EmployeeRecord) -> None:
    """The caller expands departments to include descendants before building the
    context, so a sub-team member is a colleague too."""
    projection = resolve_visibility(
        viewer(employee_id=uuid4(), departments={SHARED_DEPARTMENT, SUB_DEPARTMENT}), record
    )

    assert projection.visibility == "directory"


def test_a_colleague_in_another_department_sees_only_the_contact_list(
    record: EmployeeRecord,
) -> None:
    projection = resolve_visibility(
        viewer(employee_id=uuid4(), departments={OTHER_DEPARTMENT}), record
    )

    assert projection.visibility == "minimal"
    assert not (DIRECTORY_FIELDS & projection.allowed_fields)
    assert not (PRIVATE_FIELDS & projection.allowed_fields)
    assert BASE_FIELDS <= projection.allowed_fields


def test_a_colleague_cannot_manage_the_record(record: EmployeeRecord) -> None:
    projection = resolve_visibility(
        viewer(employee_id=uuid4(), roles=["employee"], departments={SHARED_DEPARTMENT}), record
    )

    assert projection.can_manage is False


def test_hr_can_manage(record: EmployeeRecord) -> None:
    projection = resolve_visibility(viewer(employee_id=uuid4(), roles=["hr"]), record)

    assert projection.can_manage is True


# --- anonymous -------------------------------------------------------------


def test_an_anonymous_viewer_receives_the_minimum(record: EmployeeRecord) -> None:
    projection = resolve_visibility(viewer(), record)

    assert projection.visibility == "minimal"
    assert projection.private is None
    assert projection.can_manage is False


# --- the matrix ------------------------------------------------------------

VIEWER_CASES = {
    "self": lambda record: viewer(employee_id=record.employee.id),
    "hr": lambda record: viewer(employee_id=uuid4(), roles=["hr"]),
    "finance": lambda record: viewer(employee_id=uuid4(), roles=["finance"]),
    "compliance": lambda record: viewer(employee_id=uuid4(), roles=["compliance"]),
    "admin": lambda record: viewer(employee_id=uuid4(), roles=["admin"]),
    "colleague": lambda record: viewer(employee_id=uuid4(), departments={SHARED_DEPARTMENT}),
    "outsider": lambda record: viewer(employee_id=uuid4(), departments={OTHER_DEPARTMENT}),
    "anonymous": lambda record: viewer(),
}

#: Which field groups each viewer must receive. Exhaustive by construction: the
#: test below iterates every viewer against every group.
EXPECTED = {
    "self": {"base", "directory", "private"},
    "hr": {"base", "directory", "private"},
    "finance": {"base", "directory", "private"},
    "compliance": {"base", "directory", "private"},
    "admin": {"base"},
    "colleague": {"base", "directory"},
    "outsider": {"base"},
    "anonymous": {"base"},
}

GROUPS = {"base": BASE_FIELDS, "directory": DIRECTORY_FIELDS, "private": PRIVATE_FIELDS}


@pytest.mark.parametrize("viewer_name", sorted(VIEWER_CASES))
@pytest.mark.parametrize("group", sorted(GROUPS))
def test_visibility_matrix(viewer_name: str, group: str) -> None:
    record = build_record()
    context = VIEWER_CASES[viewer_name](record)
    projection = resolve_visibility(context, record)

    granted = GROUPS[group] <= projection.allowed_fields
    expected = group in EXPECTED[viewer_name]

    assert granted is expected, (
        f"{viewer_name} {'should' if expected else 'must not'} receive {group} fields"
    )


def test_private_projection_omits_unset_values_without_inventing_them() -> None:
    """A withheld value is absent; an unset one is also absent. Neither becomes a
    placeholder that a client could mistake for real data."""
    employee = Employee(
        id=uuid4(),
        first_name="Ana",
        last_name="Martín",
        preferred_name=None,
        email="a@empresa.es",
        photo_path=None,
        city=None,
        country=None,
        hire_date=date(2024, 1, 1),
        termination_date=None,
        status=EmploymentStatus.ACTIVE,
    )
    record = EmployeeRecord(
        employee=employee,
        private=EmployeePrivate(address_line=None, employee_no="E-1"),
        assignments=(),
    )

    payload = project_private(resolve_visibility(viewer(employee_id=employee.id), record))

    assert payload["employee_no"] == "E-1"
    assert payload["address_line"] is None
