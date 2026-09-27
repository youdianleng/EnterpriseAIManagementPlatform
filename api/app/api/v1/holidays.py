"""Holiday endpoints: the calendar HR maintains, and everybody reads.

`docs/DESIGN.md` Q22 in three sentences: **holidays are data**. They are imported
from a file and edited one at a time, never hardcoded, and a year nobody has
written code for is a file somebody uploads. Reading the calendar is open to
everyone signed in — it is a fact about the country, and everybody plans around it —
while writing it is HR and administration, the same authority that writes the
schedules a holiday zeroes.

**A holiday change reaches the next request.** The year's rows are cached
(`docs/DESIGN.md` §4.4, 24 hours) under a key that carries a stamp derived from the
rows themselves, so an edit does not invalidate anything — the entry the edit
superseded is simply never looked up again. That is also why an edit made outside
this API, by the import command in another process or by hand in `psql`, takes
effect just as immediately: the stamp is read from the table, not maintained by
whoever remembered to bump it.

**Deleting one does not rewrite history.** A month whose expected hours were
snapshotted carries the holiday rows it applied, so correcting the calendar changes
what happens next and never what was already recorded.
"""

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import db_session, require
from app.api.v1.schemas.base import StrictModel
from app.domain.access import Action, Principal, ResourceKind
from app.domain.schedule.models import Holiday, HolidayInput, HolidayScope
from app.domain.schedule.service import ScheduleService
from app.repositories.schedule import PostgresScheduleRepository

router = APIRouter(prefix="/holidays", tags=["schedule"])

#: Everyone: it is a public calendar, like the organisation tree.
read_holidays = require(Action.HOLIDAY_READ, ResourceKind.HOLIDAY)

#: HR and administration. A holiday moves a month's expected hours.
manage_holidays = require(Action.HOLIDAY_MANAGE, ResourceKind.HOLIDAY)


class HolidayWrite(StrictModel):
    """A holiday as HR states it.

    No `year`: it is the year of `date`, and a year a caller can type is a year
    that can disagree with the date beside it. The database refuses the
    disagreement as well, because a hand-written row is always possible.
    """

    date: date
    name_es: str = Field(min_length=1, max_length=160)
    name_en: str = Field(min_length=1, max_length=160)
    scope: HolidayScope
    region_code: str | None = Field(
        default=None, max_length=16, description="ISO 3166-2, e.g. ES-MD"
    )


class HolidayRead(BaseModel):
    id: UUID
    date: date
    name_es: str
    name_en: str
    scope: HolidayScope
    region_code: str | None
    year: int


def _service(session: AsyncSession) -> ScheduleService:
    return ScheduleService(PostgresScheduleRepository(session), session)


def _read(holiday: Holiday) -> HolidayRead:
    return HolidayRead(
        id=holiday.id,
        date=holiday.date,
        name_es=holiday.name_es,
        name_en=holiday.name_en,
        scope=holiday.scope,
        region_code=holiday.region_code,
        year=holiday.year,
    )


def _input(payload: HolidayWrite) -> HolidayInput:
    return HolidayInput(
        date=payload.date,
        name_es=payload.name_es,
        name_en=payload.name_en,
        scope=payload.scope,
        region_code=payload.region_code or None,
    )


@router.get("", response_model=list[HolidayRead], summary="Read a year's calendar")
async def list_holidays(
    year: int | None = Query(default=None, ge=2000, le=2200),
    scope: HolidayScope | None = Query(default=None),
    region_code: str | None = Query(default=None),
    _: Principal = Depends(read_holidays),
    session: AsyncSession = Depends(db_session),
) -> list[HolidayRead]:
    """The year's holidays, all of them unless a scope or a region is named.

    Unfiltered on purpose: HR needs to see a calendar that includes regions nobody
    in the company works in, because that is how a wrong region code is noticed.
    """
    holidays = await _service(session).list_holidays(
        year=year, scope=scope, region_code=region_code
    )
    return [_read(holiday) for holiday in holidays]


@router.post("", response_model=HolidayRead, status_code=201, summary="Add a holiday")
async def add_holiday(
    payload: HolidayWrite,
    principal: Principal = Depends(manage_holidays),
    session: AsyncSession = Depends(db_session),
) -> HolidayRead:
    holiday = await _service(session).add_holiday(
        _input(payload),
        actor_user_id=principal.user_id,
        actor_roles=principal.roles,
    )
    return _read(holiday)


@router.patch(
    "/{holiday_id}", response_model=HolidayRead, summary="Correct a holiday"
)
async def change_holiday(
    holiday_id: UUID,
    payload: HolidayWrite,
    principal: Principal = Depends(manage_holidays),
    session: AsyncSession = Depends(db_session),
) -> HolidayRead:
    """A full statement rather than a patch: a holiday is four fields plus its
    date, and the four of them are what decides who observes it."""
    holiday = await _service(session).update_holiday(
        holiday_id,
        _input(payload),
        actor_user_id=principal.user_id,
        actor_roles=principal.roles,
    )
    return _read(holiday)


@router.delete(
    "/{holiday_id}", status_code=204, summary="Remove a holiday that should not be there"
)
async def delete_holiday(
    holiday_id: UUID,
    principal: Principal = Depends(manage_holidays),
    session: AsyncSession = Depends(db_session),
) -> None:
    await _service(session).delete_holiday(
        holiday_id, actor_user_id=principal.user_id, actor_roles=principal.roles
    )
