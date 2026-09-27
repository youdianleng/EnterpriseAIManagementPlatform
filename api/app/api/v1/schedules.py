"""Schedule endpoints: the patterns, the overrides, and what a month was worth.

Four surfaces in one router because they are one subject, and they have three
different audiences:

* **Maintaining patterns and overrides** is HR and administration
  (`schedule.manage`). A schedule is the input to what the company owes somebody,
  so it is not a thing other roles write.
* **Reading your own week**, and your own expected hours, is self-service and
  self-only (`schedule.read_own` plus the kernel's `SELF_ONLY_ACTIONS`): naming a
  colleague in `employee_id` is a 403 decided by the kernel and recorded, exactly
  as ticket 21 does for a punch. There is deliberately no endpoint here that lists
  the company's expected hours — that is HR's question and belongs with the actions
  ticket 24 adds for reading somebody else's attendance.
* **Snapshotting a month** is the deliberate act that freezes a figure with the
  rules that produced it. It is HR's, and the month-end pass
  (`app/jobs/snapshot_expected_hours.py`) is the same operation without a caller.

**Every date here is a date.** `on_date` decides which pattern applies, the year and
month decide which figure is asked for, and there is no way to ask any of these
endpoints a question about an instant — the same rule the attendance module states,
for the same reason.
"""

from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import db_session, require, require_own
from app.api.v1.schemas.base import StrictModel
from app.core.errors import AppError, ErrorCode
from app.domain.access import Action, Principal, ResourceKind
from app.domain.attendance.business_day import madrid_today
from app.domain.schedule.models import (
    DayExpectation,
    Holiday,
    MonthExpectation,
    OverrideInput,
    ScheduleDayInput,
    ScheduleInput,
    ScheduleOverride,
    SchedulePatch,
    ScheduleSource,
    WorkSchedule,
)
from app.domain.schedule.service import ScheduleService
from app.repositories.schedule import PostgresScheduleRepository

router = APIRouter(prefix="/schedules", tags=["schedule"])

#: HR and administration.
manage_schedules = require(Action.SCHEDULE_MANAGE, ResourceKind.SCHEDULE)

#: Everyone, about themselves. What makes it self-only is the kernel's
#: `SELF_ONLY_ACTIONS`, which is why naming a colleague passes this dependency and
#: is refused a line later.
read_own_schedule = require(Action.SCHEDULE_READ_OWN, ResourceKind.EMPLOYEE)


class ScheduleDayPayload(StrictModel):
    """One weekday. `weekday` is 0 for Monday, as everywhere in this module."""

    weekday: int = Field(ge=0, le=6)
    expected_minutes: int = Field(ge=0, le=1440)
    start_time: str | None = Field(default=None, description="HH:MM, local time")
    end_time: str | None = Field(default=None, description="HH:MM, on the same day")
    break_minutes: int = Field(default=0, ge=0)


class ScheduleCreate(StrictModel):
    code: str = Field(min_length=1, max_length=64)
    name_es: str = Field(min_length=1, max_length=160)
    name_en: str = Field(min_length=1, max_length=160)
    days: list[ScheduleDayPayload] = Field(min_length=1)
    department_id: UUID | None = Field(
        default=None, description="Omit for the company default"
    )
    is_default: bool = False


class ScheduleChange(StrictModel):
    """Every field optional; absent means "leave it as it was".

    `days`, when given, replaces the whole week rather than merging into it: a week
    is read as a whole, and a merge could not express removing a day.
    """

    name_es: str | None = Field(default=None, min_length=1, max_length=160)
    name_en: str | None = Field(default=None, min_length=1, max_length=160)
    days: list[ScheduleDayPayload] | None = Field(default=None, min_length=1)
    is_default: bool | None = None
    is_active: bool | None = None


class ScheduleDayRead(BaseModel):
    weekday: int
    expected_minutes: int
    start_time: str | None
    end_time: str | None
    break_minutes: int


class ScheduleRead(BaseModel):
    id: UUID
    code: str
    name_es: str
    name_en: str
    department_id: UUID | None
    weekly_hours: Decimal
    is_default: bool
    is_active: bool
    days: list[ScheduleDayRead]


class OverrideCreate(StrictModel):
    employee_id: UUID
    schedule_id: UUID
    effective_from: date
    effective_to: date | None = None
    reason: str = Field(min_length=1, max_length=500)


class OverrideChange(StrictModel):
    schedule_id: UUID | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    reason: str | None = Field(default=None, min_length=1, max_length=500)


class OverrideRead(BaseModel):
    id: UUID
    employee_id: UUID
    schedule_id: UUID
    effective_from: date
    effective_to: date | None
    reason: str


class HolidayRead(BaseModel):
    id: UUID
    date: date
    name_es: str
    name_en: str
    scope: str
    region_code: str | None
    year: int


class MyScheduleRead(BaseModel):
    """One of the caller's days: the pattern, and what it expects of them.

    `source` is returned because it is the question people actually ask — "why is
    Friday six hours" — and the answer is in the fallback chain rather than in the
    numbers.
    """

    business_date: date
    weekday: int
    expected_minutes: int
    source: ScheduleSource
    region_code: str | None
    schedule: ScheduleRead | None
    holiday: HolidayRead | None


class ExpectedHoursRead(BaseModel):
    """A month: the figure, and whether it is a stored snapshot or a live answer.

    `is_snapshot` false means the number came from today's rules and nobody has
    frozen it; a client that needs the frozen version asks the snapshot endpoint.
    The days and the holidays travel with the figure because they are what makes it
    checkable — the same document the snapshot stores.
    """

    employee_id: UUID
    year: int
    month: int
    expected_minutes: int
    is_snapshot: bool
    snapshot_id: UUID | None
    revision: int | None
    region_codes: list[str]
    days: list[dict[str, Any]]
    holidays: list[dict[str, Any]]


class SnapshotRequest(StrictModel):
    year: int = Field(ge=2000, le=2200)
    month: int = Field(ge=1, le=12)
    employee_id: UUID | None = Field(
        default=None, description="Omit to snapshot everybody still employed"
    )


def _service(session: AsyncSession) -> ScheduleService:
    return ScheduleService(PostgresScheduleRepository(session), session)


def _at(value: str | None) -> time | None:
    """`HH:MM` into a time, refusing anything finer.

    A window is stated in minutes and `expected_minutes` has to equal it exactly;
    seconds would make the two agree only by rounding, and the table's own check
    constraint rounds where Python truncates.
    """
    if value is None:
        return None
    try:
        return time.fromisoformat(value)
    except ValueError as error:
        raise AppError(
            ErrorCode.INVALID_REQUEST, detail=f"{value!r} is not a time of day (HH:MM)"
        ) from error


def _day_input(day: ScheduleDayPayload) -> ScheduleDayInput:
    return ScheduleDayInput(
        weekday=day.weekday,
        expected_minutes=day.expected_minutes,
        start_time=_at(day.start_time),
        end_time=_at(day.end_time),
        break_minutes=day.break_minutes,
    )


def _schedule_read(schedule: WorkSchedule) -> ScheduleRead:
    return ScheduleRead(
        id=schedule.id,
        code=schedule.code,
        name_es=schedule.name_es,
        name_en=schedule.name_en,
        department_id=schedule.department_id,
        weekly_hours=schedule.weekly_hours,
        is_default=schedule.is_default,
        is_active=schedule.is_active,
        days=[
            ScheduleDayRead(
                weekday=day.weekday,
                expected_minutes=day.expected_minutes,
                start_time=(
                    day.start_time.isoformat(timespec="minutes") if day.start_time else None
                ),
                end_time=day.end_time.isoformat(timespec="minutes") if day.end_time else None,
                break_minutes=day.break_minutes,
            )
            for day in schedule.days
        ],
    )


def _holiday_read(holiday: Holiday | None) -> HolidayRead | None:
    if holiday is None:
        return None
    return HolidayRead(
        id=holiday.id,
        date=holiday.date,
        name_es=holiday.name_es,
        name_en=holiday.name_en,
        scope=holiday.scope.value,
        region_code=holiday.region_code,
        year=holiday.year,
    )


def _override_read(override: ScheduleOverride) -> OverrideRead:
    return OverrideRead(
        id=override.id,
        employee_id=override.employee_id,
        schedule_id=override.schedule_id,
        effective_from=override.effective_from,
        effective_to=override.effective_to,
        reason=override.reason,
    )


def _month_read(month: MonthExpectation) -> ExpectedHoursRead:
    snapshot = month.snapshot
    return ExpectedHoursRead(
        employee_id=month.employee_id,
        year=month.year,
        month=month.month,
        expected_minutes=month.expected_minutes,
        is_snapshot=month.is_snapshot,
        snapshot_id=snapshot.id if snapshot is not None else None,
        revision=snapshot.revision if snapshot is not None else None,
        region_codes=list(month.inputs.get("region_codes", [])),
        days=list(month.inputs.get("days", [])),
        holidays=list(month.inputs.get("holidays", [])),
    )


async def _day_read(service: ScheduleService, expectation: DayExpectation) -> MyScheduleRead:
    schedule = (
        await service.get_schedule(expectation.schedule_id)
        if expectation.schedule_id is not None
        else None
    )
    return MyScheduleRead(
        business_date=expectation.business_date,
        weekday=expectation.weekday,
        expected_minutes=expectation.expected_minutes,
        source=expectation.source,
        region_code=expectation.region_code,
        schedule=_schedule_read(schedule) if schedule is not None else None,
        holiday=_holiday_read(expectation.holiday),
    )


@router.get("", response_model=list[ScheduleRead], summary="List the patterns")
async def list_schedules(
    include_inactive: bool = Query(default=False),
    _: Principal = Depends(manage_schedules),
    session: AsyncSession = Depends(db_session),
) -> list[ScheduleRead]:
    schedules = await _service(session).list_schedules(include_inactive=include_inactive)
    return [_schedule_read(schedule) for schedule in schedules]


@router.post(
    "", response_model=ScheduleRead, status_code=201, summary="Create a weekly pattern"
)
async def create_schedule(
    payload: ScheduleCreate,
    principal: Principal = Depends(manage_schedules),
    session: AsyncSession = Depends(db_session),
) -> ScheduleRead:
    """A pattern for a department, or the company default.

    `weekly_hours` is not a field: it is the sum of the days, and the service
    computes it. A day whose window and minutes disagree is refused here with a
    catalogued 422 rather than by the table's constraint.
    """
    schedule = await _service(session).create_schedule(
        ScheduleInput(
            code=payload.code,
            name_es=payload.name_es,
            name_en=payload.name_en,
            days=tuple(_day_input(day) for day in payload.days),
            department_id=payload.department_id,
            is_default=payload.is_default,
        ),
        actor_user_id=principal.user_id,
        actor_roles=principal.roles,
    )
    return _schedule_read(schedule)


@router.get(
    "/mine", response_model=MyScheduleRead, summary="Read your own schedule for a date"
)
async def read_my_schedule(
    request: Request,
    on_date: date | None = Query(
        default=None, description="Madrid business date; defaults to today"
    ),
    employee_id: UUID | None = Query(
        default=None, description="Whose schedule; defaults to the caller"
    ),
    principal: Principal = Depends(read_own_schedule),
    session: AsyncSession = Depends(db_session),
) -> MyScheduleRead:
    """Which pattern governs your day, and why it is that one."""
    subject = employee_id or principal.employee_id
    await require_own(
        request, principal, Action.SCHEDULE_READ_OWN, ResourceKind.EMPLOYEE, subject
    )

    service = _service(session)
    day = on_date or madrid_today(datetime.now(UTC))
    return await _day_read(service, await service.day_expectation(subject, day))


@router.get(
    "/expected-hours",
    response_model=ExpectedHoursRead,
    summary="Read your own expected hours for a month",
)
async def read_expected_hours(
    request: Request,
    year: int | None = Query(default=None, ge=2000, le=2200),
    month: int | None = Query(default=None, ge=1, le=12),
    employee_id: UUID | None = Query(
        default=None, description="Whose month; defaults to the caller"
    ),
    principal: Principal = Depends(read_own_schedule),
    session: AsyncSession = Depends(db_session),
) -> ExpectedHoursRead:
    """The month's figure, with the days and holidays it was computed from.

    A month that has been snapshotted answers with the stored figure — frozen with
    the rules that produced it — and a month that has not is computed from today's
    rules. `is_snapshot` says which of the two arrived, so a client never has to
    guess whether a schedule edited since is reflected in the number.

    With no year and month given, the answer is for the month the company is in —
    Madrid's month, not the browser's, as everywhere else in these two modules.
    """
    subject = employee_id or principal.employee_id
    await require_own(
        request, principal, Action.SCHEDULE_READ_OWN, ResourceKind.EMPLOYEE, subject
    )
    today = madrid_today(datetime.now(UTC))
    return _month_read(
        await _service(session).expected_minutes(
            subject, year or today.year, month or today.month
        )
    )


@router.post(
    "/expected-hours/snapshots",
    response_model=list[ExpectedHoursRead],
    status_code=201,
    summary="Freeze a month with the rules that produced it",
)
async def snapshot_expected_hours(
    payload: SnapshotRequest,
    principal: Principal = Depends(manage_schedules),
    session: AsyncSession = Depends(db_session),
) -> list[ExpectedHoursRead]:
    """Write the month down, as a new revision when anything has moved.

    Without `employee_id` this is the month-end pass: every employee who still
    works here, each in its own transaction. Running it twice writes nothing the
    second time, because a revision whose inputs are identical is not appended.

    201 for the same reason `POST /attendance/clock` answers 201 to a replay: the
    body is *the record this request stands for* — the revision that now covers the
    month — rather than a claim that this particular call inserted a row.
    """
    service = _service(session)
    if payload.employee_id is not None:
        written = [
            await service.snapshot_month(
                payload.employee_id,
                payload.year,
                payload.month,
                computed_by_employee_id=principal.employee_id,
            )
        ]
    else:
        report = await service.snapshot_all(
            payload.year, payload.month, computed_by_employee_id=principal.employee_id
        )
        written = list(report.snapshotted)
    return [_month_read(month) for month in written]


@router.post(
    "/overrides",
    response_model=OverrideRead,
    status_code=201,
    summary="Give one person a different week, for a window of dates",
)
async def create_override(
    payload: OverrideCreate,
    principal: Principal = Depends(manage_schedules),
    session: AsyncSession = Depends(db_session),
) -> OverrideRead:
    """The part-time case, and every other reason a week differs from a team's."""
    override = await _service(session).set_override(
        OverrideInput(
            employee_id=payload.employee_id,
            schedule_id=payload.schedule_id,
            effective_from=payload.effective_from,
            effective_to=payload.effective_to,
            reason=payload.reason,
            created_by_employee_id=principal.employee_id,
        ),
        actor_user_id=principal.user_id,
        actor_roles=principal.roles,
    )
    return _override_read(override)


@router.patch(
    "/overrides/{override_id}",
    response_model=OverrideRead,
    summary="Correct an override's window or pattern",
)
async def change_override(
    override_id: UUID,
    payload: OverrideChange,
    principal: Principal = Depends(manage_schedules),
    session: AsyncSession = Depends(db_session),
) -> OverrideRead:
    service = _service(session)
    current = await service.get_override(override_id)
    override = await service.set_override(
        OverrideInput(
            employee_id=current.employee_id,
            schedule_id=payload.schedule_id or current.schedule_id,
            effective_from=payload.effective_from or current.effective_from,
            effective_to=(
                payload.effective_to
                if payload.effective_to is not None
                else current.effective_to
            ),
            reason=payload.reason or current.reason,
            created_by_employee_id=principal.employee_id,
        ),
        override_id=override_id,
        actor_user_id=principal.user_id,
        actor_roles=principal.roles,
    )
    return _override_read(override)


@router.delete(
    "/overrides/{override_id}",
    status_code=204,
    summary="Remove an override that was written down wrongly",
)
async def delete_override(
    override_id: UUID,
    principal: Principal = Depends(manage_schedules),
    session: AsyncSession = Depends(db_session),
) -> None:
    await _service(session).delete_override(
        override_id, actor_user_id=principal.user_id, actor_roles=principal.roles
    )


@router.get("/{schedule_id}", response_model=ScheduleRead, summary="Read one pattern")
async def read_schedule(
    schedule_id: UUID,
    _: Principal = Depends(manage_schedules),
    session: AsyncSession = Depends(db_session),
) -> ScheduleRead:
    return _schedule_read(await _service(session).get_schedule(schedule_id))


@router.patch("/{schedule_id}", response_model=ScheduleRead, summary="Change a pattern")
async def change_schedule(
    schedule_id: UUID,
    payload: ScheduleChange,
    principal: Principal = Depends(manage_schedules),
    session: AsyncSession = Depends(db_session),
) -> ScheduleRead:
    """Replace what is stated and leave the rest.

    The `code` is not patchable: it is what a report names a pattern by, so
    changing it would change what an old report meant.
    """
    schedule = await _service(session).update_schedule(
        schedule_id,
        SchedulePatch(
            name_es=payload.name_es,
            name_en=payload.name_en,
            days=(
                tuple(_day_input(day) for day in payload.days)
                if payload.days is not None
                else None
            ),
            is_default=payload.is_default,
            is_active=payload.is_active,
        ),
        actor_user_id=principal.user_id,
        actor_roles=principal.roles,
    )
    return _schedule_read(schedule)
