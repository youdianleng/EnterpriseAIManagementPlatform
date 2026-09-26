"""Organisation rules, exercised without a database.

The service depends on a Protocol, so these tests state the rules directly:
where a department lands in the tree, what a move does, and what may not be
deleted. No fixtures, no containers.
"""

import pytest

from app.domain.errors import DomainError, DomainErrorCode
from app.domain.org.models import ClearanceLevel, DepartmentPatch
from app.domain.org.paths import child_path, depth_of, is_descendant_path, to_label
from app.domain.org.service import MAX_DEPTH, DepartmentService, build_tree
from tests.support.org import InMemoryDepartmentRepository, make_input


def service(repository: InMemoryDepartmentRepository) -> DepartmentService:
    async def noop() -> None:
        return None

    return DepartmentService(repository=repository, invalidate=noop)


@pytest.fixture
def repository() -> InMemoryDepartmentRepository:
    return InMemoryDepartmentRepository()


# --- paths -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("engineering", "engineering"),
        # A hyphen is not valid in an ltree label, so it becomes an underscore.
        ("r-and-d", "r_and_d"),
        # A literal underscore is doubled, which keeps the two codes distinct.
        ("r_and_d", "r__and__d"),
        ("RRHH", "RRHH"),
    ],
)
def test_codes_become_valid_ltree_labels(code: str, expected: str) -> None:
    """ltree labels allow only [A-Za-z0-9_], so anything else must be mapped."""
    assert to_label(code) == expected


def test_escaped_underscore_keeps_codes_distinct() -> None:
    """`r-d` and `r_d` must not collide on the same label."""
    assert to_label("r-d") != to_label("r_d")


def test_child_path_and_depth() -> None:
    assert child_path(None, "company") == "company"
    assert child_path("company", "engineering") == "company.engineering"
    assert depth_of("company") == 0
    assert depth_of("company.engineering.backend") == 2


def test_is_descendant_path_matches_sql_semantics() -> None:
    """Mirrors ltree's `<@` so the guard and the query cannot drift apart."""
    assert is_descendant_path("company.engineering", "company")
    assert is_descendant_path("company.engineering", "company.engineering")
    assert not is_descendant_path("company", "company.engineering")
    # A shared prefix is not ancestry.
    assert not is_descendant_path("companyx", "company")


# --- create ----------------------------------------------------------------


async def test_root_department_gets_its_own_path(repository) -> None:
    created = await service(repository).create(make_input("company"))

    assert created.path == "company"
    assert created.depth == 0
    assert repository.commits == 1


async def test_child_path_is_derived_from_the_parent(repository) -> None:
    svc = service(repository)
    root = await svc.create(make_input("company"))
    child = await svc.create(make_input("engineering", root.id))

    assert child.path == "company.engineering"
    assert child.depth == 1


async def test_duplicate_code_is_rejected(repository) -> None:
    svc = service(repository)
    await svc.create(make_input("company"))

    with pytest.raises(DomainError) as excinfo:
        await svc.create(make_input("company"))

    assert excinfo.value.code is DomainErrorCode.DEPARTMENT_CODE_TAKEN
    assert excinfo.value.http_status == 409


async def test_unknown_parent_is_rejected(repository) -> None:
    from uuid import uuid4

    with pytest.raises(DomainError) as excinfo:
        await service(repository).create(make_input("orphan", uuid4()))

    assert excinfo.value.code is DomainErrorCode.DEPARTMENT_PARENT_INVALID


async def test_inactive_parent_is_rejected(repository) -> None:
    svc = service(repository)
    root = await svc.create(make_input("company"))
    await svc.update(root.id, DepartmentPatch(is_active=False))

    with pytest.raises(DomainError) as excinfo:
        await svc.create(make_input("engineering", root.id))

    assert excinfo.value.code is DomainErrorCode.DEPARTMENT_PARENT_INVALID


async def test_depth_limit_is_enforced(repository) -> None:
    """Depth indexes 0..MAX_DEPTH are allowed; one below that is not."""
    svc = service(repository)
    parent = await svc.create(make_input("l0"))
    for level in range(1, MAX_DEPTH + 1):
        parent = await svc.create(make_input(f"l{level}", parent.id))
    assert parent.depth == MAX_DEPTH

    with pytest.raises(DomainError) as excinfo:
        await svc.create(make_input("too-deep", parent.id))

    assert excinfo.value.code is DomainErrorCode.DEPARTMENT_DEPTH_EXCEEDED


# --- tree ------------------------------------------------------------------


async def test_tree_nests_children_and_counts_every_department(repository) -> None:
    svc = service(repository)
    company = await svc.create(make_input("company"))
    engineering = await svc.create(make_input("engineering", company.id))
    await svc.create(make_input("backend", engineering.id))
    await svc.create(make_input("finance", company.id))

    tree = await svc.list_tree()

    assert tree.total == 4
    assert tree.max_depth == 2
    assert [root.department.code for root in tree.roots] == ["company"]
    root = tree.roots[0]
    # Children are ordered by code, so the UI does not have to sort.
    assert [child.department.code for child in root.children] == ["engineering", "finance"]
    engineering_node = next(c for c in root.children if c.department.code == "engineering")
    assert [child.department.code for child in engineering_node.children] == ["backend"]


async def test_tree_includes_inactive_only_when_asked(repository) -> None:
    svc = service(repository)
    company = await svc.create(make_input("company"))
    old = await svc.create(make_input("legacy", company.id))
    await svc.update(old.id, DepartmentPatch(is_active=False))

    assert (await svc.list_tree()).total == 2
    assert (await svc.list_tree(include_inactive=False)).total == 1


def test_build_tree_surfaces_a_node_whose_parent_is_missing() -> None:
    """A department must never silently vanish from the tree."""
    from uuid import uuid4

    from app.domain.org.models import Department

    orphan = Department(
        id=uuid4(),
        code="orphan",
        name_es="x",
        name_en="x",
        parent_id=uuid4(),  # parent is not in the list
        path="gone.orphan",
        depth=1,
        clearance_level=ClearanceLevel.LOW,
        cost_center=None,
        manager_employee_id=None,
        description_es=None,
        description_en=None,
        is_active=True,
    )

    tree = build_tree([orphan])

    assert tree.total == 1
    assert tree.roots[0].department.code == "orphan"


# --- subtree ---------------------------------------------------------------


async def test_subtree_query_returns_self_and_descendants(repository) -> None:
    svc = service(repository)
    company = await svc.create(make_input("company"))
    engineering = await svc.create(make_input("engineering", company.id))
    await svc.create(make_input("backend", engineering.id))
    await svc.create(make_input("finance", company.id))

    under_engineering = await svc.list_subtree(engineering.path)
    without_self = await svc.list_subtree(engineering.path, include_self=False)

    assert {d.code for d in under_engineering} == {"engineering", "backend"}
    assert {d.code for d in without_self} == {"backend"}


# --- move ------------------------------------------------------------------


async def test_move_relocates_the_whole_subtree(repository) -> None:
    svc = service(repository)
    company = await svc.create(make_input("company"))
    ops = await svc.create(make_input("ops", company.id))
    engineering = await svc.create(make_input("engineering", company.id))
    backend = await svc.create(make_input("backend", engineering.id))
    deep = await svc.create(make_input("platform", backend.id))

    moved = await svc.move(engineering.id, ops.id)

    assert moved.path == "company.ops.engineering"
    assert moved.depth == 2
    # The descendants travelled with it and their depths were recomputed.
    assert (await svc.get(backend.id)).path == "company.ops.engineering.backend"
    assert (await svc.get(backend.id)).depth == 3
    assert (await svc.get(deep.id)).path == "company.ops.engineering.backend.platform"
    assert (await svc.get(deep.id)).depth == 4


async def test_move_to_root_is_allowed(repository) -> None:
    svc = service(repository)
    company = await svc.create(make_input("company"))
    engineering = await svc.create(make_input("engineering", company.id))

    moved = await svc.move(engineering.id, None)

    assert moved.path == "engineering"
    assert moved.depth == 0


async def test_move_into_own_descendant_is_rejected(repository) -> None:
    svc = service(repository)
    company = await svc.create(make_input("company"))
    engineering = await svc.create(make_input("engineering", company.id))
    backend = await svc.create(make_input("backend", engineering.id))

    with pytest.raises(DomainError) as excinfo:
        await svc.move(engineering.id, backend.id)

    assert excinfo.value.code is DomainErrorCode.DEPARTMENT_MOVE_INTO_DESCENDANT


async def test_move_into_itself_is_rejected(repository) -> None:
    svc = service(repository)
    company = await svc.create(make_input("company"))

    with pytest.raises(DomainError) as excinfo:
        await svc.move(company.id, company.id)

    assert excinfo.value.code is DomainErrorCode.DEPARTMENT_MOVE_INTO_DESCENDANT


async def test_move_that_would_exceed_the_depth_limit_is_rejected(repository) -> None:
    svc = service(repository)
    parent = await svc.create(make_input("l0"))
    for level in range(1, MAX_DEPTH + 1):
        parent = await svc.create(make_input(f"l{level}", parent.id))

    # `parent` sits at the deepest allowed level, so a subtree cannot move under it.
    company = await svc.create(make_input("company"))
    engineering = await svc.create(make_input("engineering", company.id))
    await svc.create(make_input("backend", engineering.id))

    with pytest.raises(DomainError) as excinfo:
        await svc.move(engineering.id, parent.id)

    assert excinfo.value.code is DomainErrorCode.DEPARTMENT_DEPTH_EXCEEDED


# --- delete ----------------------------------------------------------------


async def test_delete_removes_an_empty_department(repository) -> None:
    svc = service(repository)
    company = await svc.create(make_input("company"))

    await svc.delete(company.id)

    tree = await svc.list_tree()
    assert tree.total == 0
    assert tree.roots == ()


async def test_delete_with_children_is_rejected(repository) -> None:
    svc = service(repository)
    company = await svc.create(make_input("company"))
    await svc.create(make_input("engineering", company.id))

    with pytest.raises(DomainError) as excinfo:
        await svc.delete(company.id)

    assert excinfo.value.code is DomainErrorCode.DEPARTMENT_HAS_CHILDREN


async def test_delete_with_staff_is_rejected_before_the_children_check(repository) -> None:
    """"Has staff" is the message a human can act on, so it wins."""
    svc = service(repository)
    company = await svc.create(make_input("company"))
    await svc.create(make_input("engineering", company.id))
    repository.employees[company.id] = 3

    with pytest.raises(DomainError) as excinfo:
        await svc.delete(company.id)

    assert excinfo.value.code is DomainErrorCode.DEPARTMENT_NOT_EMPTY
    assert excinfo.value.http_status == 409


async def test_unknown_department_lookups_are_reported(repository) -> None:
    from uuid import uuid4

    with pytest.raises(DomainError) as excinfo:
        await service(repository).get(uuid4())

    assert excinfo.value.code is DomainErrorCode.DEPARTMENT_NOT_FOUND
    assert excinfo.value.http_status == 404
