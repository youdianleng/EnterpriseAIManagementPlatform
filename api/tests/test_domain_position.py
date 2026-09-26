"""Position catalogue rules, exercised without a database."""

from uuid import uuid4

import pytest

from app.domain.errors import DomainError
from app.domain.position.errors import PositionErrorCode
from app.domain.position.models import PositionInput, PositionPatch
from app.domain.position.service import PositionService
from tests.support.employee import make_department
from tests.support.position import InMemoryPositionRepository, StubDepartments


@pytest.fixture
def catalogue():
    first = make_department("rrhh")
    second = make_department("finanzas")
    repository = InMemoryPositionRepository({first.id: first, second.id: second})
    service = PositionService(
        repository=repository, departments=StubDepartments(repository.departments)
    )
    return repository, service, first, second


def make_input(department_id, **overrides: object) -> PositionInput:
    defaults: dict = {
        "code": "tech",
        "title_es": "Técnica",
        "title_en": "Technician",
        "department_id": department_id,
    }
    defaults.update(overrides)
    return PositionInput(**defaults)


async def test_a_position_is_created_under_its_department(catalogue) -> None:
    repository, service, first, _ = catalogue

    position = await service.create(make_input(first.id))

    assert position.department_id == first.id
    assert position.department_code == "rrhh"
    assert position.is_active is True
    assert repository.commits == 1


async def test_the_same_code_may_exist_in_two_departments(catalogue) -> None:
    """Codes describe a role inside a team, so `manager` belongs under several."""
    _, service, first, second = catalogue
    await service.create(make_input(first.id, code="manager"))

    position = await service.create(make_input(second.id, code="manager"))

    assert position.code == "manager"
    assert position.department_id == second.id


async def test_the_same_code_in_one_department_is_refused(catalogue) -> None:
    _, service, first, _ = catalogue
    await service.create(make_input(first.id))

    with pytest.raises(DomainError) as excinfo:
        await service.create(make_input(first.id))

    assert excinfo.value.code is PositionErrorCode.POSITION_CODE_TAKEN
    assert excinfo.value.http_status == 409


async def test_an_unknown_department_is_refused(catalogue) -> None:
    _, service, _, _ = catalogue

    with pytest.raises(DomainError) as excinfo:
        await service.create(make_input(uuid4()))

    assert excinfo.value.code is PositionErrorCode.POSITION_DEPARTMENT_INVALID


async def test_an_inactive_department_is_refused(catalogue) -> None:
    repository, service, first, _ = catalogue
    repository.departments[first.id] = type(first)(
        **{**{f: getattr(first, f) for f in first.__slots__}, "is_active": False}
    )

    with pytest.raises(DomainError) as excinfo:
        await service.create(make_input(first.id))

    assert excinfo.value.code is PositionErrorCode.POSITION_DEPARTMENT_INVALID


async def test_managerial_flag_round_trips(catalogue) -> None:
    _, service, first, _ = catalogue

    position = await service.create(make_input(first.id, is_managerial=True))

    assert position.is_managerial is True


async def test_deactivating_keeps_the_position_readable(catalogue) -> None:
    """Retiring a role must not break the assignments that reference it."""
    _, service, first, _ = catalogue
    position = await service.create(make_input(first.id))

    deactivated = await service.deactivate(position.id)

    assert deactivated.is_active is False
    # Still retrievable, which is what an ended assignment needs.
    assert (await service.get(position.id)).is_active is False


async def test_a_position_in_use_cannot_be_deleted(catalogue) -> None:
    repository, service, first, _ = catalogue
    position = repository.seed(first, assignment_count=3)

    with pytest.raises(DomainError) as excinfo:
        await service.delete(position.id)

    assert excinfo.value.code is PositionErrorCode.POSITION_IN_USE
    assert excinfo.value.http_status == 409
    # The message tells the operator what to do instead.
    assert "deactivate" in (excinfo.value.detail or "")


async def test_a_position_referenced_only_by_history_cannot_be_deleted(catalogue) -> None:
    """The foreign key from assignments is RESTRICT.

    An active-only check would let this through and then fail inside the
    database, turning a clear refusal into a 500.
    """
    repository, service, first, _ = catalogue
    position = repository.seed(first, assignment_count=0, ended_assignment_count=1)

    with pytest.raises(DomainError) as excinfo:
        await service.delete(position.id)

    assert excinfo.value.code is PositionErrorCode.POSITION_IN_USE


async def test_an_unused_position_can_be_deleted(catalogue) -> None:
    repository, service, first, _ = catalogue
    position = repository.seed(first, assignment_count=0)

    await service.delete(position.id)

    assert await repository.get(position.id) is None


async def test_unknown_position_is_a_not_found(catalogue) -> None:
    _, service, _, _ = catalogue

    with pytest.raises(DomainError) as excinfo:
        await service.get(uuid4())

    assert excinfo.value.code is PositionErrorCode.POSITION_NOT_FOUND
    assert excinfo.value.http_status == 404


async def test_the_catalogue_can_be_filtered_by_department(catalogue) -> None:
    _, service, first, second = catalogue
    await service.create(make_input(first.id, code="a"))
    await service.create(make_input(second.id, code="b"))

    only_first = await service.list_positions(department_id=first.id)

    assert [position.code for position in only_first] == ["a"]


async def test_inactive_positions_can_be_excluded(catalogue) -> None:
    _, service, first, _ = catalogue
    live = await service.create(make_input(first.id, code="live"))
    retired = await service.create(make_input(first.id, code="retired"))
    await service.deactivate(retired.id)

    assert len(await service.list_positions()) == 2
    active_only = await service.list_positions(include_inactive=False)
    assert [position.id for position in active_only] == [live.id]


async def test_titles_can_be_changed_without_touching_the_flag(catalogue) -> None:
    _, service, first, _ = catalogue
    position = await service.create(make_input(first.id, is_managerial=True))

    updated = await service.update(
        position.id, PositionPatch(title_es="Técnica sénior")
    )

    assert updated.title_es == "Técnica sénior"
    assert updated.title_en == "Technician"
    assert updated.is_managerial is True
