"""Attendance endpoints: your own clock, your own day, your own range.

Three endpoints, and every one of them answers about the caller. There is no
endpoint that lists somebody's punches and none that recomputes a colleague's day,
because those are ticket 24's questions and they need their own permission rather
than a wider version of this one.

**Clocking out notifies somebody else, and the caller does not choose who.** The
service is wrapped in `AttendanceNotifier` (ticket 23), which sends the manager of
the caller's primary position — or the assignment's notification override — a
notification about the finished day. It is not an endpoint and has no request of its
own: it is a consequence of the punch, raised in the same request, and a client
cannot address it anywhere.

**Clocking for somebody else is expressible, and refused.** `employee_id` defaults
to the caller and may be given explicitly — the surface could have left the field
out entirely (the notification centre does exactly that), but then "HR punches for
a colleague" could not be refused, only made unsayable, and the ticket asks for the
refusal *and* the audit record. Naming somebody else is a 403 decided by the kernel
(`attendance.clock_own` and `attendance.read_own` are self-only actions) and
recorded as `access.refused` in its own transaction, the way the `require()`
dependency records one.

**What the caller may not choose.** The source of a punch is always `web`: a client
claiming its punch arrived as a correction would be writing a different kind of row
than the one this endpoint appends. `at` may be given — an offline punch synced
later is real — and the module refuses an instant in the future with its own code.

**Dates are dates.** The day endpoint's `business_date` defaults to *today in
Madrid*, which is the module's answer rather than the browser's, and both reads
take only calendar dates. There is no way to ask this surface for a day computed
from a timestamp.
"""

from datetime import UTC, date, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import current_principal, db_session, require, require_own
from app.domain.access import Action, Principal, ResourceKind
from app.domain.attendance.business_day import madrid_today
from app.domain.attendance.models import (
    AttendanceEvent,
    DayRecord,
    DayStatus,
    EventSource,
    EventType,
)
from app.domain.attendance.notify import AttendanceNotifier
from app.domain.attendance.service import AttendanceService
from app.domain.notification.service import NotificationService
from app.domain.schedule.service import ScheduleService
from app.repositories.attendance import PostgresAttendanceRepository
from app.repositories.notification import PostgresNotificationRepository
from app.repositories.schedule import PostgresScheduleRepository

router = APIRouter(prefix="/attendance", tags=["attendance"])

#: Everyone, about themselves. The role check is the catalogue's; what makes either
#: action self-only is the kernel's `SELF_ONLY_ACTIONS`, which is why naming a
#: colleague passes this dependency and is refused a line later.
clock_own = require(Action.ATTENDANCE_CLOCK_OWN, ResourceKind.EMPLOYEE)
read_own_attendance = require(Action.ATTENDANCE_READ_OWN, ResourceKind.EMPLOYEE)


class ClockRequest(BaseModel):
    """One punch.

    `kind` accepts the stream's whole vocabulary and the service refuses
    `correction` with its own catalogued code: a correction is not a punch, and a
    client that sends one should be told why rather than handed a validation error
    that does not mention the correction flow.
    """

    kind: EventType
    #: When it happened. Absent means now. Must carry a timezone: a naive instant
    #: cannot be attributed to a business day, and guessing one is how an
    #: application records the wrong day twice a year.
    at: datetime | None = Field(
        default=None, description="ISO 8601 instant with an offset; defaults to now"
    )
    #: Who is clocking in. Defaults to the caller; anybody else is a 403.
    employee_id: UUID | None = Field(
        default=None, description="The employee the punch is for; defaults to the caller"
    )


class EventRead(BaseModel):
    """The row that was appended, or the one an identical request already wrote."""

    id: UUID
    event_type: EventType
    occurred_at: datetime
    #: The Madrid business day this punch counts against. Returned so the client
    #: never has to work it out — the same reason the interface takes dates.
    business_date: date
    source: EventSource


class DayRead(BaseModel):
    """One day, as derived.

    `recomputed_at` is null for a day that was derived to answer this read and has
    never been stored: nobody worked, or nothing has recomputed it. A client that
    wants the distinction can see it.
    """

    business_date: date
    status: DayStatus
    first_in: datetime | None
    last_out: datetime | None
    worked_minutes: int
    expected_minutes: int | None
    overtime_minutes: int | None
    recomputed_at: datetime | None


class RangeRead(BaseModel):
    """Every day in the range, gaps included and marked `absent`."""

    from_date: date
    to_date: date
    days: list[DayRead]


def _service(session: AsyncSession) -> AttendanceNotifier:
    """The module, with the schedule behind it and the clock-out notification on top.

    Building the collaborators here rather than in the service is what keeps the
    attendance module's interface at four operations: it is handed something that
    answers "what did the schedule expect", and it never learns what a schedule is.
    The notifier wraps the service the way `ApprovalNotifier` wraps the engine — a
    caller constructs one object where it used to construct the service, so the
    notification is not a step somebody has to remember after clocking out (ticket
    23).

    The repository is built once and passed twice: it is the module's own storage
    and it is also what answers "who is this person's manager" for the notification,
    which is the same shape the approval engine's route resolution has.
    """
    attendance = PostgresAttendanceRepository(session)
    return AttendanceNotifier(
        AttendanceService(
            attendance,
            expectations=ScheduleService(PostgresScheduleRepository(session), session),
        ),
        NotificationService(PostgresNotificationRepository(session), session),
        attendance,
    )


def _event(event: AttendanceEvent) -> EventRead:
    return EventRead(
        id=event.id,
        event_type=event.event_type,
        occurred_at=event.occurred_at,
        business_date=event.business_date,
        source=event.source,
    )


def _day(record: DayRecord) -> DayRead:
    return DayRead(
        business_date=record.business_date,
        status=record.status,
        first_in=record.first_in,
        last_out=record.last_out,
        worked_minutes=record.worked_minutes,
        expected_minutes=record.expected_minutes,
        overtime_minutes=record.overtime_minutes,
        recomputed_at=record.recomputed_at,
    )


async def _require_self(
    request: Request, principal: Principal, action: Action, subject: UUID
) -> None:
    """The caller's own record, or a refusal that is recorded.

    Delegates to the shared helper in `deps`, which is where the convention lives
    now that two routers need it (ticket 22 reads somebody's own week the same way).
    """
    await require_own(request, principal, action, ResourceKind.EMPLOYEE, subject)


@router.post(
    "/clock",
    response_model=EventRead,
    status_code=201,
    summary="Punch your own clock",
    dependencies=[Depends(clock_own)],
)
async def clock(
    request: Request,
    payload: ClockRequest,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> EventRead:
    """Append the punch and rebuild the day, in one transaction.

    A request that is a replay — same employee, same kind, same instant as a punch
    already recorded — answers with the row it wrote the first time, so a network
    retry is idempotent rather than a second shift.
    """
    subject = payload.employee_id or principal.employee_id
    await _require_self(request, principal, Action.ATTENDANCE_CLOCK_OWN, subject)

    event = await _service(session).clock(
        subject,
        payload.kind,
        payload.at or datetime.now(UTC),
        EventSource.WEB,
        ip_address=request.client.host if request.client else None,
        created_by_employee_id=principal.employee_id,
    )
    return _event(event)


@router.get(
    "/day",
    response_model=DayRead,
    summary="Read one of your days",
    dependencies=[Depends(read_own_attendance)],
)
async def read_day(
    request: Request,
    business_date: date | None = Query(
        default=None, description="Madrid business date; defaults to today"
    ),
    employee_id: UUID | None = Query(
        default=None, description="Whose day; defaults to the caller"
    ),
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> DayRead:
    """Your own day, stored snapshot first.

    A day nobody has recomputed and nobody worked comes back derived and marked
    `absent` rather than as a 404: "you did not punch" is an answer, and the client
    renders it as the state of today.
    """
    subject = employee_id or principal.employee_id
    await _require_self(request, principal, Action.ATTENDANCE_READ_OWN, subject)

    day = business_date or madrid_today(datetime.now(UTC))
    return _day(await _service(session).day_view(subject, day))


@router.get(
    "/range",
    response_model=RangeRead,
    summary="Read a range of your days, gaps included",
    dependencies=[Depends(read_own_attendance)],
)
async def read_range(
    request: Request,
    from_date: date | None = Query(
        default=None, description="First Madrid business date; defaults to today"
    ),
    to_date: date | None = Query(
        default=None, description="Last business date, inclusive; defaults to from_date"
    ),
    employee_id: UUID | None = Query(
        default=None, description="Whose days; defaults to the caller"
    ),
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> RangeRead:
    """Every day in the range, including the ones with no events at all.

    Complete on purpose: a month with three days off comes back as a month with
    three `absent` days, not as a list of the days somebody happened to punch.
    """
    subject = employee_id or principal.employee_id
    await _require_self(request, principal, Action.ATTENDANCE_READ_OWN, subject)

    today = madrid_today(datetime.now(UTC))
    start = from_date or today
    end = to_date or start
    days = await _service(session).range_view(subject, start, end)
    return RangeRead(from_date=start, to_date=end, days=[_day(day) for day in days])
