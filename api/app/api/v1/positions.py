"""Job position catalogue endpoints.

Positions are the second half of the organisation model: a department says where
someone sits, a position says what they are. Assignment validation in the
employee module reads from here, so "may this position be taken" has one answer.

Authorisation is named per route via the kernel; no role comparison appears here.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import db_session, require
from app.api.v1.schemas.position import PositionCreate, PositionRead, PositionUpdate
from app.domain.access import Action, ResourceKind
from app.domain.position.models import Position, PositionInput, PositionPatch
from app.domain.position.service import PositionService
from app.repositories.org import PostgresDepartmentRepository
from app.repositories.position import PostgresPositionRepository

router = APIRouter(prefix="/positions", tags=["organisation"])

read_catalogue = require(Action.POSITION_READ, ResourceKind.POSITION)
manage_catalogue = require(Action.POSITION_MANAGE, ResourceKind.POSITION)


def _service(session: AsyncSession) -> PositionService:
    return PositionService(
        repository=PostgresPositionRepository(session),
        departments=PostgresDepartmentRepository(session),
    )


def _read(position: Position) -> PositionRead:
    return PositionRead.model_validate(position, from_attributes=True)


@router.get(
    "",
    response_model=list[PositionRead],
    summary="Position catalogue",
    dependencies=[Depends(read_catalogue)],
)
async def list_positions(
    department_id: UUID | None = Query(default=None),
    include_inactive: bool = Query(default=True),
    session: AsyncSession = Depends(db_session),
) -> list[PositionRead]:
    positions = await _service(session).list_positions(
        department_id=department_id, include_inactive=include_inactive
    )
    return [_read(position) for position in positions]


@router.post(
    "",
    response_model=PositionRead,
    status_code=201,
    summary="Create a position",
    dependencies=[Depends(manage_catalogue)],
)
async def create_position(
    payload: PositionCreate,
    session: AsyncSession = Depends(db_session),
) -> PositionRead:
    position = await _service(session).create(
        PositionInput(
            code=payload.code,
            title_es=payload.title_es,
            title_en=payload.title_en,
            department_id=payload.department_id,
            is_managerial=payload.is_managerial,
        )
    )
    return _read(position)


@router.get(
    "/{position_id}",
    response_model=PositionRead,
    summary="Read a position",
    dependencies=[Depends(read_catalogue)],
)
async def get_position(
    position_id: UUID,
    session: AsyncSession = Depends(db_session),
) -> PositionRead:
    return _read(await _service(session).get(position_id))


@router.patch(
    "/{position_id}",
    response_model=PositionRead,
    summary="Update a position",
    dependencies=[Depends(manage_catalogue)],
)
async def update_position(
    position_id: UUID,
    payload: PositionUpdate,
    session: AsyncSession = Depends(db_session),
) -> PositionRead:
    patch = PositionPatch(**payload.model_dump(exclude_unset=True))
    return _read(await _service(session).update(position_id, patch))


@router.post(
    "/{position_id}/deactivate",
    response_model=PositionRead,
    summary="Deactivate a position",
    dependencies=[Depends(manage_catalogue)],
)
async def deactivate_position(
    position_id: UUID,
    session: AsyncSession = Depends(db_session),
) -> PositionRead:
    """The correct way to retire a position that is still referenced.

    Deactivating keeps existing assignments resolving while refusing new ones,
    which is what an organisation actually needs when a role is replaced.
    """
    return _read(await _service(session).deactivate(position_id))


@router.delete(
    "/{position_id}",
    status_code=204,
    summary="Delete an unused position",
    dependencies=[Depends(manage_catalogue)],
)
async def delete_position(
    position_id: UUID,
    session: AsyncSession = Depends(db_session),
) -> None:
    await _service(session).delete(position_id)
