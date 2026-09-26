"""Employee and assignment rules, exercised without a database."""

from datetime import date
from uuid import uuid4

import pytest

from app.domain.employee.models import (
    AssignmentInput,
    EmployeeInput,
    EmployeePatch,
    EmployeePrivate,
    EmploymentStatus,
)
from app.domain.employee.service import EmployeeService
from app.domain.errors import DomainError, DomainErrorCode
from tests.support.employee import (
    InMemoryDepartments,
    InMemoryEmployeeRepository,
    make_department,
)


@pytest.fixture
def org():
    department = make_department("rrhh")
    other = make_department("finanzas")
    repository = InMemoryEmployeeRepository({department.id: department, other.id: other})
    service = EmployeeService(
        repository=repository, departments=InMemoryDepartments(repository.departments)
    )
    position = repository.seed_position(department)
    other_position = repository.seed_position(other, code="analyst")
    return repository, service, department, other, position, other_position


def make_input(**overrides: object) -> EmployeeInput:
    defaults: dict = {
        "first_name": "Ana",
        "last_name": "Martín",
        "email": "ana@empresa.es",
        "hire_date": date(2024, 1, 15),
    }
    defaults.update(overrides)
    return EmployeeInput(**defaults)


# --- creation --------------------------------------------------------------


async def test_creating_an_employee_stores_the_withheld_details(org) -> None:
    repository, service, *_ = org

    record = await service.create(
        make_input(private=EmployeePrivate(address_line="Calle Mayor 1", employee_no="E-1"))
    )

    assert record.employee.email == "ana@empresa.es"
    assert record.private.employee_no == "E-1"
    assert repository.commits == 1


async def test_duplicate_email_is_refused(org) -> None:
    _, service, *_ = org
    await service.create(make_input())

    with pytest.raises(DomainError) as excinfo:
        await service.create(make_input(first_name="Luis"))

    assert excinfo.value.code is DomainErrorCode.EMPLOYEE_EMAIL_TAKEN
    assert excinfo.value.http_status == 409


async def test_duplicate_staff_number_is_refused(org) -> None:
    _, service, *_ = org
    await service.create(make_input(private=EmployeePrivate(employee_no="E-1")))

    with pytest.raises(DomainError) as excinfo:
        await service.create(
            make_input(email="other@empresa.es", private=EmployeePrivate(employee_no="E-1"))
        )

    assert excinfo.value.code is DomainErrorCode.EMPLOYEE_NUMBER_TAKEN


async def test_termination_before_hire_is_refused(org) -> None:
    _, service, *_ = org

    with pytest.raises(DomainError) as excinfo:
        await service.create(
            make_input(termination_date=date(2023, 1, 1), status=EmploymentStatus.TERMINATED)
        )

    assert excinfo.value.code is DomainErrorCode.EMPLOYEE_DATES_INVALID


async def test_patching_into_an_invalid_date_range_is_refused(org) -> None:
    _, service, *_ = org
    record = await service.create(make_input(hire_date=date(2024, 6, 1)))

    with pytest.raises(DomainError) as excinfo:
        await service.update(record.employee.id, EmployeePatch(termination_date=date(2024, 1, 1)))

    assert excinfo.value.code is DomainErrorCode.EMPLOYEE_DATES_INVALID


async def test_patch_leaves_unsupplied_fields_alone(org) -> None:
    _, service, *_ = org
    record = await service.create(make_input(city="Madrid"))

    updated = await service.update(record.employee.id, EmployeePatch(city="Valencia"))

    assert updated.employee.city == "Valencia"
    assert updated.employee.first_name == "Ana"


# --- assignments -----------------------------------------------------------


async def test_the_first_assignment_becomes_the_primary_one(org) -> None:
    repository, service, department, other, position, other_position = org
    record = await service.create(make_input())
    employee = record.employee

    await service.assign_position(
        employee.id,
        AssignmentInput(
            department_id=department.id,
            job_position_id=position.id,
            start_date=date(2024, 1, 15),
        ),
    )

    assignments = await service.list_assignments(employee.id)
    assert len(assignments) == 1
    assert assignments[0].is_primary is True


async def test_a_later_assignment_is_not_primary(org) -> None:
    _, service, department, other, position, other_position = org
    employee = (await service.create(make_input())).employee

    await service.assign_position(
        employee.id,
        AssignmentInput(
            department_id=department.id,
            job_position_id=position.id,
            start_date=date(2024, 1, 15),
        ),
    )
    await service.assign_position(
        employee.id,
        AssignmentInput(
            department_id=other.id,
            job_position_id=other_position.id,
            start_date=date(2025, 1, 1),
        ),
    )

    assignments = await service.list_assignments(employee.id)
    primary = [a for a in assignments if a.is_primary]
    assert len(primary) == 1
    assert primary[0].department_id == department.id


async def test_an_employee_can_hold_positions_in_several_departments(org) -> None:
    _, service, department, other, position, other_position = org
    employee = (await service.create(make_input())).employee

    await service.assign_position(
        employee.id,
        AssignmentInput(
            department_id=department.id,
            job_position_id=position.id,
            start_date=date(2024, 1, 15),
        ),
    )
    record = await service.assign_position(
        employee.id,
        AssignmentInput(
            department_id=other.id,
            job_position_id=other_position.id,
            start_date=date(2025, 1, 1),
        ),
    )

    assert {a.department_id for a in record.assignments} == {department.id, other.id}


async def test_assigning_an_unknown_position_is_refused(org) -> None:
    _, service, department, *_rest = org
    employee = (await service.create(make_input())).employee

    with pytest.raises(DomainError) as excinfo:
        await service.assign_position(
            employee.id,
            AssignmentInput(
                department_id=department.id,
                job_position_id=uuid4(),
                start_date=date(2024, 1, 15),
            ),
        )

    assert excinfo.value.code is DomainErrorCode.EMPLOYEE_POSITION_NOT_FOUND


async def test_assigning_an_inactive_position_is_refused(org) -> None:
    repository, service, department, *_rest = org
    employee = (await service.create(make_input())).employee
    retired = repository.seed_position(department, code="retired", is_active=False)

    with pytest.raises(DomainError) as excinfo:
        await service.assign_position(
            employee.id,
            AssignmentInput(
                department_id=department.id,
                job_position_id=retired.id,
                start_date=date(2024, 1, 15),
            ),
        )

    assert excinfo.value.code is DomainErrorCode.EMPLOYEE_POSITION_INACTIVE


async def test_an_unknown_approver_is_refused_before_it_is_stored(org) -> None:
    """An approval route pointing at nobody is worse than no route: the failure
    would surface days later, during an approval."""
    _, service, department, _other, position, _other_position = org
    employee = (await service.create(make_input())).employee

    with pytest.raises(DomainError) as excinfo:
        await service.assign_position(
            employee.id,
            AssignmentInput(
                department_id=department.id,
                job_position_id=position.id,
                start_date=date(2024, 1, 15),
                manager_employee_id=uuid4(),
            ),
        )

    assert excinfo.value.code is DomainErrorCode.EMPLOYEE_MANAGER_NOT_FOUND


async def test_an_employee_cannot_be_their_own_approver(org) -> None:
    _, service, department, _other, position, _other_position = org
    employee = (await service.create(make_input())).employee

    with pytest.raises(DomainError) as excinfo:
        await service.assign_position(
            employee.id,
            AssignmentInput(
                department_id=department.id,
                job_position_id=position.id,
                start_date=date(2024, 1, 15),
                manager_employee_id=employee.id,
            ),
        )

    assert excinfo.value.code is DomainErrorCode.EMPLOYEE_MANAGER_NOT_FOUND


async def test_the_last_active_position_cannot_be_ended(org) -> None:
    """Every later feature assumes an active employee has somewhere to be."""
    _, service, department, _other, position, _other_position = org
    employee = (await service.create(make_input())).employee
    record = await service.assign_position(
        employee.id,
        AssignmentInput(
            department_id=department.id,
            job_position_id=position.id,
            start_date=date(2024, 1, 15),
        ),
    )

    with pytest.raises(DomainError) as excinfo:
        await service.end_assignment(employee.id, record.assignments[0].id)

    assert excinfo.value.code is DomainErrorCode.EMPLOYEE_LAST_ASSIGNMENT


async def test_ending_the_primary_promotes_another_position(org) -> None:
    """"The primary one" must always resolve, or approval routing has no answer."""
    _, service, department, other, position, other_position = org
    employee = (await service.create(make_input())).employee
    first = await service.assign_position(
        employee.id,
        AssignmentInput(
            department_id=department.id,
            job_position_id=position.id,
            start_date=date(2024, 1, 15),
        ),
    )
    second = await service.assign_position(
        employee.id,
        AssignmentInput(
            department_id=other.id,
            job_position_id=other_position.id,
            start_date=date(2025, 1, 1),
        ),
    )
    primary_id = next(a.id for a in first.assignments if a.is_primary)

    record = await service.end_assignment(employee.id, primary_id)

    active = [a for a in record.assignments if a.end_date is None]
    assert len(active) == 1
    assert active[0].is_primary is True
    assert active[0].id == next(a.id for a in second.assignments if not a.is_primary)


async def test_an_ended_assignment_cannot_be_promoted(org) -> None:
    _, service, department, other, position, other_position = org
    employee = (await service.create(make_input())).employee
    first = await service.assign_position(
        employee.id,
        AssignmentInput(
            department_id=department.id,
            job_position_id=position.id,
            start_date=date(2024, 1, 15),
        ),
    )
    await service.assign_position(
        employee.id,
        AssignmentInput(
            department_id=other.id,
            job_position_id=other_position.id,
            start_date=date(2025, 1, 1),
        ),
    )
    ended_id = next(a.id for a in first.assignments if a.is_primary)
    await service.end_assignment(employee.id, ended_id)

    with pytest.raises(DomainError) as excinfo:
        await service.set_primary(employee.id, ended_id)

    assert excinfo.value.code is DomainErrorCode.EMPLOYEE_ASSIGNMENT_ENDED


async def test_promoting_a_position_moves_the_primary_flag(org) -> None:
    _, service, department, other, position, other_position = org
    employee = (await service.create(make_input())).employee
    await service.assign_position(
        employee.id,
        AssignmentInput(
            department_id=department.id,
            job_position_id=position.id,
            start_date=date(2024, 1, 15),
        ),
    )
    record = await service.assign_position(
        employee.id,
        AssignmentInput(
            department_id=other.id,
            job_position_id=other_position.id,
            start_date=date(2025, 1, 1),
        ),
    )
    second_id = next(a.id for a in record.assignments if a.department_id == other.id)

    updated = await service.set_primary(employee.id, second_id)

    primary = [a for a in updated.assignments if a.is_primary]
    assert len(primary) == 1
    assert primary[0].department_id == other.id


async def test_assignments_belonging_to_someone_else_are_not_reachable(org) -> None:
    _, service, department, _other, position, _other_position = org
    ana = (await service.create(make_input())).employee
    luis = (
        await service.create(make_input(email="luis@empresa.es", first_name="Luis"))
    ).employee
    record = await service.assign_position(
        ana.id,
        AssignmentInput(
            department_id=department.id,
            job_position_id=position.id,
            start_date=date(2024, 1, 15),
        ),
    )
    ana_assignment = record.assignments[0].id

    with pytest.raises(DomainError) as excinfo:
        await service.end_assignment(luis.id, ana_assignment)

    assert excinfo.value.code is DomainErrorCode.EMPLOYEE_ASSIGNMENT_NOT_FOUND


async def test_unknown_employee_is_a_not_found(org) -> None:
    _, service, *_ = org

    with pytest.raises(DomainError) as excinfo:
        await service.get_record(uuid4())

    assert excinfo.value.code is DomainErrorCode.EMPLOYEE_NOT_FOUND
    assert excinfo.value.http_status == 404
