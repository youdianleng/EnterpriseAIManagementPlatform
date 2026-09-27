"""Attendance endpoints: your own clock, your own day, and the correction flow.

Ticket 21's three endpoints answer about the caller and nothing else. Ticket 24 adds
two surfaces beside them, and both are governed by actions of their own:

* **The correction flow** (`/attendance/corrections`): a document naming a day, a
  punch, the instant it should have been and why. It goes through the unified
  approval engine at two levels — the direct manager, then HR — and the approval is
  what appends the event. There is **no endpoint anywhere that changes a punch in
  place**, and that is not an omission: the only write this surface can produce is
  the append an approved document makes, and the stream itself refuses UPDATE and
  DELETE to the role requests connect as (migration 0012).
* **The record** (`/attendance/punches`, `/attendance/export`): one day's punches
  with the chain each one became and the day they derive, and the same days as a CSV
  an accountant or a labour inspector can read.

**Who reaches somebody else's record is a catalogued action, not a wider version of
the caller's own.** `attendance.read_own` stays self-only, exactly as ticket 21 left
it; a manager reads their reports through `attendance.read_report`, and HR reads the
company through `attendance.read_all`. The kernel refuses the middle case — a
manager and a colleague share a department, and "my department" is not "my report" —
which is why those two are in `ATTENDANCE_CROSS_ACTIONS` and not on the generic
path. Filing is split the same way: `attendance.correction_own` for your own punch
and `attendance.correction_any` for HR's after-the-fact correction, which is the same
chain and the same engine.

**Clocking out notifies somebody else, and the caller does not choose who.** The
service is wrapped in `AttendanceNotifier` (ticket 23), which sends the manager of
the caller's primary position — or the assignment's notification override — a
notification about the finished day. It is not an endpoint and has no request of its
own: it is a consequence of the punch, raised in the same request.

**Clocking for somebody else is expressible, and refused.** `employee_id` defaults
to the caller and may be given explicitly — the surface could have left the field
out entirely (the notification centre does exactly that), but then "HR punches for
a colleague" could not be refused, only made unsayable, and the ticket asks for the
refusal *and* the audit record. Naming somebody else is a 403 decided by the kernel
and recorded as `access.refused` in its own transaction, the way the `require()`
dependency records one.

**What the caller may not choose.** The source of a punch is always `web`: a client
claiming its punch arrived as a correction would be writing a different kind of row
than the one this endpoint appends. `at` may be given — an offline punch synced
later is real — and the module refuses an instant in the future with its own code.
A correction's `corrected_at` is checked the same way; its *source* is the flow's,
not the client's.

**Dates are dates.** The day endpoint's `business_date` defaults to *today in
Madrid*, which is the module's answer rather than the browser's, and every read here
takes only calendar dates. There is no way to ask this surface for a day computed
from a timestamp, and the export refuses a range wider than the retention window
rather than streaming a dump of the table.
"""

from datetime import UTC, date, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import current_principal, db_session, require, require_own
from app.api.v1.schemas.base import StrictModel
from app.domain.access import Action, Principal, Resource, ResourceKind, can
from app.domain.approval.models import ApprovalState, DecisionKind
from app.domain.approval.service import ApprovalService
from app.domain.attendance.anomalies import Anomaly
from app.domain.attendance.anomaly_service import AnomalyService
from app.domain.attendance.business_day import madrid_today
from app.domain.attendance.correction_service import CorrectionService
from app.domain.attendance.corrections import (
    Correction,
    CorrectionPatch,
    CorrectionQuery,
    CorrectionState,
    CorrectionView,
)
from app.domain.attendance.models import (
    AttendanceEvent,
    DayRecord,
    DayStatus,
    EventSource,
    EventType,
)
from app.domain.attendance.notify import AttendanceNotifier
from app.domain.attendance.records import AttendanceRecords, DayDetail, PunchChain
from app.domain.attendance.service import AttendanceService
from app.domain.leave.service import LeaveCalendar
from app.domain.notification.approval import ApprovalNotifier
from app.domain.notification.service import NotificationService
from app.domain.overtime.service import OvertimeLedger
from app.domain.schedule.service import ScheduleService
from app.repositories.approval import PostgresApprovalRepository
from app.repositories.attendance import (
    PostgresAnomalyRepository,
    PostgresAttendanceRepository,
    PostgresCorrectionRepository,
)
from app.repositories.leave import PostgresLeaveRepository
from app.repositories.notification import PostgresNotificationRepository
from app.repositories.overtime import PostgresOvertimeRepository
from app.repositories.schedule import PostgresScheduleRepository

router = APIRouter(prefix="/attendance", tags=["attendance"])

#: Everyone, about themselves. The role check is the catalogue's; what makes either
#: action self-only is the kernel's `SELF_ONLY_ACTIONS`, which is why naming a
#: colleague passes this dependency and is refused a line later.
clock_own = require(Action.ATTENDANCE_CLOCK_OWN, ResourceKind.EMPLOYEE)
read_own_attendance = require(Action.ATTENDANCE_READ_OWN, ResourceKind.EMPLOYEE)

#: Deciding a correction is not a role: the engine answers "who approves this
#: request" — the requester's manager at the first level, any HR member other than
#: the requester at the second — so the endpoint only insists that the caller is
#: somebody, and a caller who is not the approver is refused by the rule that owns
#: the question. The same guard the personnel change surface uses.
signed_in = require(Action.SESSION_READ_OWN, ResourceKind.ACCOUNT)


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


class CorrectionCreate(StrictModel):
    """The request: which day, which punch, what it should say, and why.

    `employee_id` is absent for your own punch and names somebody else for HR's
    after-the-fact correction — the same field the clock endpoint has, refused the
    same way. There is no `source` and no event id: the flow resolves which row the
    request is about, and appends through the stream's own single write path.
    """

    business_date: date = Field(description="The Madrid business day the punch counts against")
    kind: EventType = Field(description="Which punch: clock_in or clock_out")
    corrected_at: datetime = Field(description="The instant it should have been, with an offset")
    reason: str = Field(min_length=1, max_length=1000, description="Why the record is wrong")
    employee_id: UUID | None = Field(
        default=None, description="Whose punch; defaults to the caller, and anybody else is HR's"
    )


class CorrectionUpdate(StrictModel):
    """What a draft may be changed to. Omitted fields are left alone.

    Only a draft is editable. A document that was returned for correction is back
    with its requester and is corrected here; a filed one is not, because two people
    were asked to approve what it says.
    """

    corrected_at: datetime | None = None
    reason: str | None = Field(default=None, min_length=1, max_length=1000)


class CorrectionDecision(StrictModel):
    """What an approver sends. Who may send it is the engine's answer."""

    decision: DecisionKind
    comment: str | None = Field(default=None, max_length=1000)


class CorrectionRead(BaseModel):
    """The document, with `state` as the one field a client reads.

    `state` derives from three facts that are all on the row or in the engine —
    whether it was filed, what the engine said, and whether the append happened —
    so `approved` on its own means "the engine said yes and the punch has not moved
    yet". That state clears by itself on the next attempt to apply it.
    """

    id: UUID
    employee_id: UUID
    business_date: date
    kind: EventType
    corrected_at: datetime
    reason: str
    state: CorrectionState
    requested_by_employee_id: UUID
    applied_event_id: UUID | None
    applied_at: datetime | None
    submitted_at: datetime | None
    created_at: datetime | None


class ApprovalDecisionRead(BaseModel):
    level: int
    round: int
    decision: str
    approver_employee_id: UUID
    comment: str | None = None
    decided_at: datetime


class ApprovalRead(BaseModel):
    """The engine's request, with every round's decisions.

    A correction that was returned, corrected and filed again is two rounds of one
    request, and both stay readable: the history is what explains the current state,
    and nothing mirrors it here.
    """

    request_id: UUID
    status: str
    round: int
    submitted_at: datetime | None = None
    decided_at: datetime | None = None
    decisions: list[ApprovalDecisionRead] = Field(default_factory=list)


class CorrectionDetail(CorrectionRead):
    approval: ApprovalRead | None = None


class CorrectionPage(BaseModel):
    items: list[CorrectionRead]
    total: int
    limit: int
    offset: int


class PunchEventRead(BaseModel):
    """One row of the stream, as the chain read shows it."""

    id: UUID
    event_type: EventType
    occurred_at: datetime
    business_date: date
    #: `web` for a punch somebody made, `correction` for one an approval appended —
    #: which is how the screen knows to say that a shift was made up rather than
    #: clocked.
    source: EventSource
    reason: str | None = None
    correction_of_event_id: UUID | None = None
    created_by_employee_id: UUID | None = None
    created_at: datetime


class PunchChainRead(BaseModel):
    """One punch, the corrections that restated it, and what the day reads.

    The corrections are in the order they were written, and `effective_at` is the
    value the day's own arithmetic uses — so the chain reads as the evolution of one
    punch (`original → correction → correction`) rather than as a set of competing
    values.
    """

    punch: PunchEventRead
    corrections: list[PunchEventRead]
    effective_at: datetime
    is_corrected: bool
    is_made_up: bool


class AnomalyRead(BaseModel):
    """Something the night's pass found wrong with the day, resolved ones included."""

    type: str
    detected_at: datetime
    notified_at: datetime | None = None
    resolved_by_event_id: UUID | None = None


class DayDetailRead(BaseModel):
    """A day's punches, the day they derive, and what was flagged about it."""

    employee_id: UUID
    business_date: date
    day: DayRead
    punches: list[PunchChainRead]
    anomalies: list[AnomalyRead]


def _collaborators(
    session: AsyncSession,
) -> tuple[PostgresAttendanceRepository, AttendanceService]:
    """The stream's repository and the module, sharing one session.

    Built once and handed to whichever surface asked, so the correction flow's
    append and the day it rebuilds are the same transaction — the property `clock`
    has, and the reason the correction service takes the service rather than
    re-deriving a day itself.

    The overtime ledger is the second seam the day is derived with (ticket 26), beside
    the schedule: `attendance_daily.overtime_minutes` is filled from the overtime
    module's records the same way `expected_minutes` is filled from the schedule's, so
    every read of a day — here, on the punches surface, and in the export — carries the
    day's approved overtime without this module knowing what a rate is.
    """
    punches = PostgresAttendanceRepository(session)
    return punches, AttendanceService(
        punches,
        expectations=ScheduleService(PostgresScheduleRepository(session), session),
        overtime=OvertimeLedger(PostgresOvertimeRepository(session)),
    )


def _approvals(session: AsyncSession) -> ApprovalNotifier:
    """The engine *wrapped*, so a decision's notifications cannot be forgotten.

    Every document in this system approves through this object: a caller that
    reached for `ApprovalService` directly would still record decisions and lose the
    notices that were supposed to follow, silently.
    """
    approvals = PostgresApprovalRepository(session)
    return ApprovalNotifier(
        engine=ApprovalService(approvals, session),
        notifications=NotificationService(PostgresNotificationRepository(session), session),
        approvals=approvals,
    )


def _service(session: AsyncSession) -> AttendanceNotifier:
    """The module, with the schedule behind it and the clock-out notification on top.

    Building the collaborators here rather than in the service is what keeps the
    attendance module's interface at four operations: it is handed something that
    answers "what did the schedule expect", and it never learns what a schedule is.
    """
    punches, service = _collaborators(session)
    return AttendanceNotifier(
        service,
        NotificationService(PostgresNotificationRepository(session), session),
        punches,
    )


def _corrections(session: AsyncSession) -> CorrectionService:
    """The correction flow, with the engine, the day and the anomalies behind it.

    The anomaly service is the same wiring the nightly job uses: `resolve_for_
    correction` re-runs the detection the scan runs, against the same schedule and the
    same leave calendar, so a day cleared by a correction and a day examined by the pass
    cannot disagree.
    """
    punches, service = _collaborators(session)
    expectations = ScheduleService(PostgresScheduleRepository(session), session)
    return CorrectionService(
        PostgresCorrectionRepository(session),
        session,
        punches=punches,
        attendance=service,
        anomalies=AnomalyService(
            PostgresAnomalyRepository(session),
            expectations=expectations,
            leave=LeaveCalendar(PostgresLeaveRepository(session)),
        ),
        approvals=_approvals(session),
    )


def _records(session: AsyncSession) -> AttendanceRecords:
    """The day read and the export, over the same repository the service uses."""
    punches, service = _collaborators(session)
    return AttendanceRecords(punches, service, PostgresAnomalyRepository(session))


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


def _row(event: AttendanceEvent) -> PunchEventRead:
    return PunchEventRead(
        id=event.id,
        event_type=event.event_type,
        occurred_at=event.occurred_at,
        business_date=event.business_date,
        source=event.source,
        reason=event.reason,
        correction_of_event_id=event.correction_of_event_id,
        created_by_employee_id=event.created_by_employee_id,
        created_at=event.created_at,
    )


def _chain(chain: PunchChain) -> PunchChainRead:
    return PunchChainRead(
        punch=_row(chain.punch),
        corrections=[_row(event) for event in chain.corrections],
        effective_at=chain.effective_at,
        is_corrected=chain.is_corrected,
        is_made_up=chain.is_made_up,
    )


def _anomaly(anomaly: Anomaly) -> AnomalyRead:
    return AnomalyRead(
        type=str(anomaly.type),
        detected_at=anomaly.detected_at,
        notified_at=anomaly.notified_at,
        resolved_by_event_id=anomaly.resolved_by_event_id,
    )


def _detail(view: DayDetail) -> DayDetailRead:
    return DayDetailRead(
        employee_id=view.employee_id,
        business_date=view.business_date,
        day=_day(view.day),
        punches=[_chain(chain) for chain in view.punches],
        anomalies=[_anomaly(anomaly) for anomaly in view.anomalies],
    )


def _approval(state: ApprovalState | None) -> ApprovalRead | None:
    if state is None:
        return None
    return ApprovalRead(
        request_id=state.id,
        status=str(state.status),
        round=state.round,
        submitted_at=state.request.submitted_at,
        decided_at=state.decided_at,
        decisions=[
            ApprovalDecisionRead(
                level=decision.level,
                round=decision.round,
                decision=str(decision.decision),
                approver_employee_id=decision.approver_employee_id,
                comment=decision.comment,
                decided_at=decision.decided_at,
            )
            for decision in state.decisions
        ],
    )


def _correction(view: CorrectionView) -> CorrectionDetail:
    return CorrectionDetail(
        **_read(view.correction, view.state).model_dump(),
        approval=_approval(view.approval),
    )


def _read(correction: Correction, state: CorrectionState) -> CorrectionRead:
    """One document as a reader sees it: its own row plus the derived state."""
    return CorrectionRead(
        id=correction.id,
        employee_id=correction.employee_id,
        business_date=correction.business_date,
        kind=correction.kind,
        corrected_at=correction.corrected_at,
        reason=correction.reason,
        state=state,
        requested_by_employee_id=correction.requested_by_employee_id,
        applied_event_id=correction.applied_event_id,
        applied_at=correction.applied_at,
        submitted_at=correction.submitted_at,
        created_at=correction.created_at,
    )


async def _require_self(
    request: Request, principal: Principal, action: Action, subject: UUID
) -> None:
    """The caller's own record, or a refusal that is recorded.

    Delegates to the shared helper in `deps`, which is where the convention lives
    now that two routers need it (ticket 22 reads somebody's own week the same way).
    """
    await require_own(request, principal, action, ResourceKind.EMPLOYEE, subject)


def _resource(subject: UUID) -> Resource:
    """Somebody's attendance, as the kernel sees it: a person's record, owned by them."""
    return Resource(ResourceKind.EMPLOYEE, owner_employee_id=subject)


async def _require_reading(request: Request, principal: Principal, subject: UUID) -> None:
    """Whether the caller may read this person's attendance, and as what.

    Three catalogued actions answer this, and which one applies is a fact about the
    caller and the subject rather than about the route: your own record
    (`attendance.read_own`, self-only), a report's (`attendance.read_report`, a
    manager's reach and only their reports') or the company's (`attendance.read_all`,
    HR). So the kernel is asked, and the refusal is recorded against the action that
    was actually attempted — a manager reaching a colleague is refused
    `attendance.read_report`, not the self-only action nobody asked for.
    """
    if subject == principal.employee_id:
        action = Action.ATTENDANCE_READ_OWN
    elif can(principal, Action.ATTENDANCE_READ_ALL, _resource(subject)).allowed:
        action = Action.ATTENDANCE_READ_ALL
    else:
        action = Action.ATTENDANCE_READ_REPORT
    await _require_self(request, principal, action, subject)


async def _require_filing(request: Request, principal: Principal, subject: UUID) -> None:
    """Whether the caller may file a correction about this person.

    Your own punch is `attendance.correction_own` — self-only, like the clock, so
    nobody asks for a correction of somebody else's day through it. Anybody else's
    is `attendance.correction_any`, which is HR's after-the-fact correction. A
    manager is in neither list: they decide corrections, they do not raise them.
    """
    action = (
        Action.ATTENDANCE_CORRECTION_OWN
        if subject == principal.employee_id
        else Action.ATTENDANCE_CORRECTION_ANY
    )
    await _require_self(request, principal, action, subject)


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


# --- the record, for the employee and for whoever may read it -----------------


@router.get(
    "/punches",
    response_model=DayDetailRead,
    summary="Read one day's punches, their correction chain and the day derived",
    dependencies=[Depends(signed_in)],
)
async def read_punches(
    request: Request,
    business_date: date | None = Query(
        default=None, description="Madrid business date; defaults to today"
    ),
    employee_id: UUID | None = Query(
        default=None, description="Whose record; defaults to the caller"
    ),
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> DayDetailRead:
    """The detail behind the day: what was punched, what was corrected, what it adds up to.

    The obligation this answers is the employee's right to see their own record, so
    there is no lower bound on the date: any historical day is answered, and a day
    with no events comes back as a day with no events rather than as a 404.
    """
    subject = employee_id or principal.employee_id
    await _require_reading(request, principal, subject)

    day = business_date or madrid_today(datetime.now(UTC))
    return _detail(await _records(session).day_detail(subject, day))


@router.get(
    "/export",
    summary="Export one person's attendance record as CSV",
    dependencies=[Depends(signed_in)],
    response_class=Response,
)
async def export_record(
    request: Request,
    from_date: date | None = Query(
        default=None, description="First Madrid business date; defaults to today"
    ),
    to_date: date | None = Query(
        default=None, description="Last business date, inclusive; defaults to from_date"
    ),
    employee_id: UUID | None = Query(
        default=None, description="Whose record; defaults to the caller"
    ),
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> Response:
    """The record for a period, as a file, with the column set the ticket documents.

    Guarded by the read actions rather than by one of its own: producing a file is
    not a wider act than reading the same days, and an inspector's copy of somebody's
    record is exactly the read they were already allowed to make. A range wider than
    the retention window is refused with the range read's own code rather than
    streamed.
    """
    subject = employee_id or principal.employee_id
    await _require_reading(request, principal, subject)

    today = madrid_today(datetime.now(UTC))
    start = from_date or today
    end = to_date or start
    export = await _records(session).export(subject, start, end)
    return Response(
        content=export.content,
        media_type=export.content_type,
        headers={"Content-Disposition": f'attachment; filename="{export.filename}"'},
    )


# --- corrections --------------------------------------------------------------


@router.post(
    "/corrections",
    response_model=CorrectionDetail,
    status_code=201,
    summary="File a correction request for a punch",
    dependencies=[Depends(signed_in)],
)
async def draft_correction(
    request: Request,
    payload: CorrectionCreate,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> CorrectionDetail:
    """Write the request. Nothing about the punch moves until two levels approve it.

    The draft is refused if it could never be applied — a future instant, a day that
    has not happened, or a day-and-kind pair that does not identify exactly one punch
    — because a request that fails after approval has already cost two people their
    attention.
    """
    subject = payload.employee_id or principal.employee_id
    await _require_filing(request, principal, subject)

    view = await _corrections(session).draft(
        employee_id=subject,
        business_date=payload.business_date,
        kind=payload.kind,
        corrected_at=payload.corrected_at,
        reason=payload.reason,
        requested_by_employee_id=principal.employee_id,
    )
    return _correction(view)


@router.get(
    "/corrections",
    response_model=CorrectionPage,
    summary="List correction requests",
    dependencies=[Depends(signed_in)],
)
async def list_corrections(
    request: Request,
    employee_id: UUID | None = Query(
        default=None, description="Whose requests; defaults to the caller's own"
    ),
    state: CorrectionState | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> CorrectionPage:
    """The documents about one person's punches, newest first.

    `state` is the derived field rather than a column: filtering by it answers "what
    is still in flight" the same way the row's own status would, and there is no
    status column to disagree with the engine.
    """
    subject = employee_id or principal.employee_id
    await _require_reading(request, principal, subject)

    views, total = await _corrections(session).list(
        CorrectionQuery(employee_id=subject, state=state, limit=limit, offset=offset)
    )
    return CorrectionPage(
        items=[_read(view.correction, view.state) for view in views],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/corrections/{correction_id}",
    response_model=CorrectionDetail,
    summary="Read one correction request, with the engine's history",
    dependencies=[Depends(signed_in)],
)
async def read_correction(
    request: Request,
    correction_id: UUID,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> CorrectionDetail:
    """The document and every round of its approval.

    Read before the permission is decided, because the subject is on the row: an
    unknown id is a 404 for anybody, and a document about somebody the caller cannot
    read is the 403 the kernel answers with.
    """
    view = await _corrections(session).get(correction_id)
    await _require_reading(request, principal, view.correction.employee_id)
    return _correction(view)


@router.patch(
    "/corrections/{correction_id}",
    response_model=CorrectionDetail,
    summary="Change a correction request that has not been filed",
    dependencies=[Depends(signed_in)],
)
async def update_correction(
    request: Request,
    correction_id: UUID,
    payload: CorrectionUpdate,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> CorrectionDetail:
    """Correct the draft — which is what a request returned for correction needs.

    Only a draft: a filed document is what two people were asked to approve, and the
    way to change an approved one is a new request, which the chain then shows beside
    the first.
    """
    service = _corrections(session)
    view = await service.get(correction_id)
    await _require_filing(request, principal, view.correction.employee_id)

    updated = await service.update(
        correction_id,
        CorrectionPatch(corrected_at=payload.corrected_at, reason=payload.reason),
    )
    return _correction(updated)


@router.post(
    "/corrections/{correction_id}/submit",
    response_model=CorrectionDetail,
    summary="File a correction request with the approval engine",
    dependencies=[Depends(signed_in)],
)
async def submit_correction(
    request: Request,
    correction_id: UUID,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> CorrectionDetail:
    """Hand it to the engine, which resolves the route: the manager, then HR.

    The requester is the person who filed the document rather than whoever pressed
    this button, because the route the engine resolves is about them and the
    notification about the outcome goes to them.
    """
    service = _corrections(session)
    view = await service.get(correction_id)
    await _require_filing(request, principal, view.correction.employee_id)
    return _correction(await service.submit(correction_id))


@router.post(
    "/corrections/{correction_id}/decide",
    response_model=CorrectionDetail,
    summary="Approve, reject or return a correction at its current level",
    dependencies=[Depends(signed_in)],
)
async def decide_correction(
    request: Request,
    correction_id: UUID,
    payload: CorrectionDecision,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> CorrectionDetail:
    """Whoever the engine says approves this document, and nobody else.

    An approval *applies* the correction in the same request: there is no effective
    date to wait for, and a document that were approved and not applied would be a
    decision that changed nothing. The notification the requester gets comes from
    the engine this endpoint is wrapped around, not from here.

    The guard is deliberately "signed in" rather than a role, the way the personnel
    change surface states it: *who approves this request* is the engine's answer —
    the requester's manager at the first level, any HR member other than the
    requester at the second — and a role check here would be a second, weaker copy
    of that rule.
    """
    decided = await _corrections(session).decide(
        correction_id,
        approver_employee_id=principal.employee_id,
        decision=payload.decision,
        comment=payload.comment,
        actor_user_id=principal.user_id,
        actor_roles=principal.roles,
    )
    return _correction(decided)
