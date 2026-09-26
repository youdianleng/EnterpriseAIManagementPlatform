"""Department endpoints.

The tree is structure that nearly every request reads and few ever change, so it
is cached and every write bumps a version stamp rather than expiring keys.

Authorisation is named, not tested: each route declares the catalogue action it
performs and the kernel answers. There is no role comparison in this file, and
adding one would be the bug.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import db_session, require
from app.api.v1.schemas.org import (
    DepartmentCreate,
    DepartmentMove,
    DepartmentNodeRead,
    DepartmentRead,
    DepartmentTreeRead,
    DepartmentUpdate,
)
from app.cache import invalidate_org_tree
from app.domain.access import Action, ResourceKind
from app.domain.org.models import (
    Department,
    DepartmentInput,
    DepartmentPatch,
    DepartmentTree,
)
from app.domain.org.service import DepartmentService
from app.repositories.org import PostgresDepartmentRepository

router = APIRouter(prefix="/departments", tags=["organisation"])

read_tree = require(Action.DEPARTMENT_READ, ResourceKind.DEPARTMENT)
manage_tree = require(Action.DEPARTMENT_MANAGE, ResourceKind.DEPARTMENT)


def _service(session: AsyncSession) -> DepartmentService:
    return DepartmentService(
        repository=PostgresDepartmentRepository(session),
        invalidate=invalidate_org_tree,
    )


def _read(department: Department) -> DepartmentRead:
    return DepartmentRead.model_validate(department, from_attributes=True)


def _node(node) -> DepartmentNodeRead:  # noqa: ANN001 - domain node type
    return DepartmentNodeRead(
        department=_read(node.department),
        children=[_node(child) for child in node.children],
    )


def _tree_read(tree: DepartmentTree) -> DepartmentTreeRead:
    return DepartmentTreeRead(
        roots=[_node(root) for root in tree.roots],
        total=tree.total,
        max_depth=tree.max_depth,
    )


@router.get(
    "",
    response_model=DepartmentTreeRead,
    summary="Organisation tree",
    dependencies=[Depends(read_tree)],
)
async def list_departments(
    include_inactive: bool = Query(default=True),
    session: AsyncSession = Depends(db_session),
) -> DepartmentTreeRead:
    tree = await _service(session).list_tree(include_inactive=include_inactive)
    return _tree_read(tree)


@router.post(
    "",
    response_model=DepartmentRead,
    status_code=201,
    summary="Create a department",
    dependencies=[Depends(manage_tree)],
)
async def create_department(
    payload: DepartmentCreate,
    session: AsyncSession = Depends(db_session),
) -> DepartmentRead:
    department = await _service(session).create(
        DepartmentInput(
            code=payload.code,
            name_es=payload.name_es,
            name_en=payload.name_en,
            parent_id=payload.parent_id,
            clearance_level=payload.clearance_level,
            cost_center=payload.cost_center,
            description_es=payload.description_es,
            description_en=payload.description_en,
        )
    )
    return _read(department)


@router.get(
    "/{department_id}",
    response_model=DepartmentRead,
    summary="Read a department",
    dependencies=[Depends(read_tree)],
)
async def get_department(
    department_id: UUID,
    session: AsyncSession = Depends(db_session),
) -> DepartmentRead:
    return _read(await _service(session).get(department_id))


@router.get(
    "/{department_id}/subtree",
    response_model=list[DepartmentRead],
    summary="A department and its descendants",
    dependencies=[Depends(read_tree)],
)
async def list_subtree(
    department_id: UUID,
    include_self: bool = Query(default=True),
    session: AsyncSession = Depends(db_session),
) -> list[DepartmentRead]:
    service = _service(session)
    department = await service.get(department_id)
    descendants = await service.list_subtree(department.path, include_self=include_self)
    return [_read(item) for item in descendants]


@router.patch(
    "/{department_id}",
    response_model=DepartmentRead,
    summary="Update a department",
    dependencies=[Depends(manage_tree)],
)
async def update_department(
    department_id: UUID,
    payload: DepartmentUpdate,
    session: AsyncSession = Depends(db_session),
) -> DepartmentRead:
    # exclude_unset keeps "field omitted" distinct from "field set to null".
    patch = DepartmentPatch(**payload.model_dump(exclude_unset=True))
    return _read(await _service(session).update(department_id, patch))


@router.post(
    "/{department_id}/move",
    response_model=DepartmentRead,
    summary="Move a department and its subtree",
    dependencies=[Depends(manage_tree)],
)
async def move_department(
    department_id: UUID,
    payload: DepartmentMove,
    session: AsyncSession = Depends(db_session),
) -> DepartmentRead:
    return _read(await _service(session).move(department_id, payload.parent_id))


@router.delete(
    "/{department_id}",
    status_code=204,
    summary="Delete an empty department",
    dependencies=[Depends(manage_tree)],
)
async def delete_department(
    department_id: UUID,
    session: AsyncSession = Depends(db_session),
) -> None:
    await _service(session).delete(department_id)
