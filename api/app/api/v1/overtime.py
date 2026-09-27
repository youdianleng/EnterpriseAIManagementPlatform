"""Overtime endpoints: the request, the record, HR's confirmation and the export.

Four surfaces, and each is governed by catalogued actions of its own rather than by a
wider version of somebody else's:

* **The requests** (`/overtime/requests`) are the document. Filing is self-only
  (`overtime.request_own`) — nobody asks for somebody else's overtime — and the
  two-level approval is the engine's: a manager at the first level and HR at the second.
  A decision *writes the record* in the same request, because an approval that produced
  nothing would be a decision that changed nothing.
* **The records** (`/overtime/records`) are the fact: the approved minutes, the minutes
  the day actually held, the smaller of the two, and HR's figure when there is one.
  Reading is decided by the kernel against the person the record is about — your own
  (`overtime.read_own`), a report's (`overtime.read_report`) or the company's
  (`overtime.read_all`, HR and finance) — the convention the attendance and leave
  surfaces established.
* **HR's acts** (`/overtime/records/{id}/confirm`, `/overtime/settlements`) are the one
  place a computed figure can be overruled, and the one place the comparison of approved
  against actually worked is run for a period. Both are HR's alone.
* **The export** (`/overtime/export`) is the month as a CSV, and the one surface with an
  action of its own (`overtime.export`, HR and finance): the file carries the staff
  number, so handing it over is a separable decision from reading a screen. Producing
  it *writes an audit record* — who exported which period, when, and how many lines it
  stated — and changes nothing else, so the same month exported twice is the same bytes
  twice and two trail entries.

**There is no endpoint that records overtime retroactively, and that is the ticket's
central rule rather than a gap.** A request's date must be today or later in Madrid, the
module refuses a draft that is edited back into the past, and the only route that leads
to an `overtime_records` row is `POST /overtime/requests/{id}/decide` reaching its second
level — there is no `POST /overtime/records`, no import, and no "correct the hours"
path for anybody, HR included. What HR has instead is the *confirmation*: the record's
computed figure stays readable and HR's sits beside it with a reason, so correcting a
past day is a correction of a record that already exists and never a way of creating
one. `tests/test_overtime.py` asserts exactly that, by walking this router's routes.

**What the caller may not choose.** The requester of a document is the employee it
names, never whoever pressed the button; a record's `month_bucket` is derived from its
Madrid business day rather than sent; and the worked minutes a settlement compares
against are read from the attendance module, never accepted from a caller. The
confirmation's *minutes* are HR's by definition — that is what confirming means — and
they are stored beside the computed value rather than over it.
"""

from datetime import date, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import current_principal, db_session, require, require_own
from app.api.v1.schemas.base import StrictModel
from app.config import get_settings
from app.domain.access import Action, Principal, Resource, ResourceKind, can
from app.domain.approval.models import ApprovalState, DecisionKind
from app.domain.approval.service import ApprovalService
from app.domain.attendance.service import AttendanceService
from app.domain.notification.approval import ApprovalNotifier
from app.domain.notification.service import NotificationService
from app.domain.overtime.models import (
    MAX_DAY_MINUTES,
    MonthlySummary,
    MonthlyTotal,
    OvertimeEntry,
    OvertimeRecord,
    OvertimeRecordQuery,
    OvertimeRecordView,
    OvertimeRequest,
    OvertimeRequestPatch,
    OvertimeRequestQuery,
    OvertimeRequestState,
    OvertimeRequestView,
)
from app.domain.overtime.service import OvertimeLedger, OvertimeService
from app.domain.schedule.service import ScheduleService
from app.repositories.approval import PostgresApprovalRepository
from app.repositories.attendance import PostgresAttendanceRepository
from app.repositories.notification import PostgresNotificationRepository
from app.repositories.overtime import PostgresOvertimeRepository
from app.repositories.schedule import PostgresScheduleRepository

router = APIRouter(prefix="/overtime", tags=["overtime"])

#: Deciding is not a role: the engine answers "who approves this request" — the
#: requester's manager at the first level, any HR member other than the requester at the
#: second — so those routes only insist that the caller is somebody. The same guard the
#: personnel, attendance and leave surfaces use.
signed_in = require(Action.SESSION_READ_OWN, ResourceKind.ACCOUNT)

#: HR's two acts on a record, and the export. Route-level guards because they are not
#: about *which* record: the sweep is about a period, and the file is about a month.
confirm_records = require(Action.OVERTIME_CONFIRM, ResourceKind.EMPLOYEE)
settle_records = require(Action.OVERTIME_SETTLE, ResourceKind.EMPLOYEE)
export_records = require(Action.OVERTIME_EXPORT, ResourceKind.EMPLOYEE)


class RequestCreate(StrictModel):
    """A request for overtime: which day, how many minutes, and why.

    The date must be today or later in Madrid — this module has no way to record
    overtime after the fact — and there is deliberately no field for a day already
    worked, no import and no correction: the pre-approval is the record.
    """

    business_date: date = Field(description="The Madrid day the overtime is for; today or later")
    expected_minutes: int = Field(
        ge=1, le=MAX_DAY_MINUTES, description="How many minutes are expected to be worked"
    )
    reason: str = Field(min_length=1, max_length=1000, description="Why the overtime is needed")
    employee_id: UUID | None = Field(
        default=None,
        description="Whose overtime; defaults to the caller, and only ever the caller",
    )


class RequestUpdate(StrictModel):
    """What a draft may be changed to. Omitted fields are left alone.

    Only a draft: a filed document is what two people were asked to approve, and a
    request returned for correction is back with its requester, which is what this is
    for.
    """

    business_date: date | None = None
    expected_minutes: int | None = Field(default=None, ge=1, le=MAX_DAY_MINUTES)
    reason: str | None = Field(default=None, min_length=1, max_length=1000)


class RequestDecision(StrictModel):
    """What an approver sends. Who may send it is the engine's answer."""

    decision: DecisionKind
    comment: str | None = Field(default=None, max_length=1000)


class RecordConfirmation(StrictModel):
    """HR's figure for a settled record, and the reason it differs.

    `minutes` is what the record is confirmed *to* — the same figure when HR agrees with
    the computed one, a different one when they do not — and it is stored beside the
    computed value, never instead of it. `note` is required: a figure nobody can explain
    is the thing this flow exists to avoid.
    """

    minutes: int = Field(ge=0, le=MAX_DAY_MINUTES)
    note: str = Field(
        min_length=1,
        max_length=500,
        description="Why the record is confirmed or adjusted; about hours, never money",
    )


class SettlementRequest(StrictModel):
    """The period HR wants settled, and only that: which records are candidates is the
    module's rule — a day that has not ended is not one."""

    month: str = Field(max_length=7, description="The month to settle, as YYYY-MM")


class RequestRead(BaseModel):
    """The document, with the state a client reads.

    `state` derives from the row and the engine — nobody has filed it, it is with the
    engine, the engine approved it, or it ended without one — and `record_id` is the
    fact an approval produced, when there is one.
    """

    id: UUID
    employee_id: UUID
    business_date: date
    expected_minutes: int
    reason: str
    state: OvertimeRequestState
    submitted_at: datetime | None = None
    approved_at: datetime | None = None
    withdrawn_at: datetime | None = None
    settled_at: datetime | None = None
    created_at: datetime | None = None
    record_id: UUID | None = None


class ApprovalDecisionRead(BaseModel):
    level: int
    round: int
    decision: str
    approver_employee_id: UUID
    comment: str | None = None
    decided_at: datetime


class ApprovalRead(BaseModel):
    """The engine's request, with every round's decisions.

    A request that was returned, corrected and filed again is two rounds of one
    document, and both stay readable: the history is what explains the state.
    """

    request_id: UUID
    status: str
    round: int
    submitted_at: datetime | None = None
    decided_at: datetime | None = None
    decisions: list[ApprovalDecisionRead] = Field(default_factory=list)


class RequestDetail(RequestRead):
    approval: ApprovalRead | None = None


class RequestPage(BaseModel):
    items: list[RequestRead]
    total: int
    limit: int
    offset: int


class EntryRead(BaseModel):
    """One movement of a record, with the figures that followed it."""

    entry_type: str
    approved_minutes: int
    computed_minutes: int | None = None
    confirmed_minutes: int | None = None
    note: str | None = None
    created_by_employee_id: UUID | None = None
    created_at: datetime | None = None


class RecordRead(BaseModel):
    """The record, with the figure in force beside the three it is derived from.

    `effective_minutes` is the fold a client would otherwise have to reimplement —
    HR's confirmation, else the settled smaller-of, else the approved minutes — and
    `needs_confirmation` is the queue: the two figures differed by more than the
    configured threshold and nobody has looked yet. Both are read-only answers; the
    three stored figures travel unchanged, which is what makes an adjustment auditable.
    """

    id: UUID
    request_id: UUID
    employee_id: UUID
    business_date: date
    month_bucket: str
    approved_minutes: int
    worked_minutes: int | None = None
    computed_minutes: int | None = None
    needs_confirmation: bool
    confirmed_minutes: int | None = None
    confirmed_by_employee_id: UUID | None = None
    confirmed_at: datetime | None = None
    confirmation_note: str | None = None
    settled_at: datetime | None = None
    effective_minutes: int
    created_at: datetime | None = None


class RecordDetail(RecordRead):
    history: list[EntryRead] = Field(default_factory=list)


class RecordPage(BaseModel):
    items: list[RecordRead]
    total: int
    limit: int
    offset: int


class MonthlyTotalRead(BaseModel):
    """One employee's month: what was approved, what it came to, and what is unanswered."""

    employee_id: UUID
    employee_name: str
    records: int
    approved_minutes: int
    effective_minutes: int
    awaiting_confirmation: int


class SummaryRead(BaseModel):
    """A month's totals, per employee.

    `threshold_minutes` travels with the rows, the way the leave module's configured
    allowance does: "waiting for confirmation" is a count somebody will want to read
    against the tolerance that produced it.
    """

    month: str
    items: list[MonthlyTotalRead]
    approved_minutes: int
    effective_minutes: int
    awaiting_confirmation: int
    threshold_minutes: int


class SettlementFailureRead(BaseModel):
    record_id: UUID
    code: str
    detail: str


class SettlementRead(BaseModel):
    """What one run of the settle sweep did.

    `skipped` counts the records whose day has not ended: examined and deliberately left
    alone, which is a different answer from "there was nothing to settle".
    """

    settled: int
    skipped: int
    failed: list[SettlementFailureRead] = Field(default_factory=list)


def _service(session: AsyncSession) -> OvertimeService:
    """The module, wired as the attendance endpoints wire theirs.

    The engine is wrapped in `ApprovalNotifier` so a decision's notifications cannot be
    forgotten, the scheduling module is handed to the attendance service as the seam
    that answers "is this a working day", and *this* module's ledger is handed to it as
    the seam that answers what a day's approved overtime came to — so the day a reader
    opens carries the figure the record states.
    """
    overtime = PostgresOvertimeRepository(session)
    approvals = PostgresApprovalRepository(session)
    return OvertimeService(
        overtime,
        session,
        approvals=ApprovalNotifier(
            engine=ApprovalService(approvals, session),
            notifications=NotificationService(PostgresNotificationRepository(session), session),
            approvals=approvals,
        ),
        attendance=AttendanceService(
            PostgresAttendanceRepository(session),
            expectations=ScheduleService(PostgresScheduleRepository(session), session),
            overtime=OvertimeLedger(overtime),
        ),
        threshold_minutes=get_settings().overtime_confirmation_threshold_minutes,
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


def _request(request: OvertimeRequest, state: OvertimeRequestState, record_id: UUID | None):
    return RequestRead(
        id=request.id,
        employee_id=request.employee_id,
        business_date=request.business_date,
        expected_minutes=request.expected_minutes,
        reason=request.reason,
        state=state,
        submitted_at=request.submitted_at,
        approved_at=request.approved_at,
        withdrawn_at=request.withdrawn_at,
        settled_at=request.settled_at,
        created_at=request.created_at,
        record_id=record_id,
    )


def _read(view: OvertimeRequestView) -> RequestRead:
    return _request(view.request, view.state, view.record_id)


def _detail(view: OvertimeRequestView) -> RequestDetail:
    return RequestDetail(
        **_read(view).model_dump(),
        approval=_approval(view.approval),
    )


def _entry(entry: OvertimeEntry) -> EntryRead:
    return EntryRead(
        entry_type=str(entry.entry_type),
        approved_minutes=entry.approved_minutes,
        computed_minutes=entry.computed_minutes,
        confirmed_minutes=entry.confirmed_minutes,
        note=entry.note,
        created_by_employee_id=entry.created_by_employee_id,
        created_at=entry.created_at,
    )


def _record(record: OvertimeRecord) -> RecordRead:
    return RecordRead(
        id=record.id,
        request_id=record.request_id,
        employee_id=record.employee_id,
        business_date=record.business_date,
        month_bucket=record.month_bucket,
        approved_minutes=record.approved_minutes,
        worked_minutes=record.worked_minutes,
        computed_minutes=record.computed_minutes,
        needs_confirmation=record.needs_confirmation,
        confirmed_minutes=record.confirmed_minutes,
        confirmed_by_employee_id=record.confirmed_by_employee_id,
        confirmed_at=record.confirmed_at,
        confirmation_note=record.confirmation_note,
        settled_at=record.settled_at,
        effective_minutes=record.effective_minutes,
        created_at=record.created_at,
    )


def _record_detail(view: OvertimeRecordView) -> RecordDetail:
    return RecordDetail(
        **_record(view.record).model_dump(),
        history=[_entry(entry) for entry in view.history],
    )


def _total(row: MonthlyTotal) -> MonthlyTotalRead:
    return MonthlyTotalRead(
        employee_id=row.employee_id,
        employee_name=row.employee_name,
        records=row.records,
        approved_minutes=row.approved_minutes,
        effective_minutes=row.effective_minutes,
        awaiting_confirmation=row.awaiting_confirmation,
    )


def _summary(summary: MonthlySummary, threshold_minutes: int) -> SummaryRead:
    return SummaryRead(
        month=summary.month,
        items=[_total(row) for row in summary.totals],
        approved_minutes=summary.approved_minutes,
        effective_minutes=summary.effective_minutes,
        awaiting_confirmation=summary.awaiting_confirmation,
        threshold_minutes=threshold_minutes,
    )


def _resource(subject: UUID) -> Resource:
    """Somebody's overtime, as the kernel sees it: a person's record, owned by them."""
    return Resource(ResourceKind.EMPLOYEE, owner_employee_id=subject)


async def _require_reading(request: Request, principal: Principal, subject: UUID) -> None:
    """Whether the caller may read this person's overtime, and as what.

    Three catalogued actions answer this, and which one applies is a fact about the
    caller and the subject rather than about the route: your own records
    (`overtime.read_own`, self-only), a report's (`overtime.read_report`, a manager's
    reach and only their reports') or the company's (`overtime.read_all`, HR and
    finance). So the kernel is asked, and the refusal is recorded against the action
    that was actually attempted — a manager reaching a colleague is refused
    `overtime.read_report`, not the self-only action nobody asked for.
    """
    if subject == principal.employee_id:
        action = Action.OVERTIME_READ_OWN
    elif can(principal, Action.OVERTIME_READ_ALL, _resource(subject)).allowed:
        action = Action.OVERTIME_READ_ALL
    else:
        action = Action.OVERTIME_READ_REPORT
    await require_own(request, principal, action, ResourceKind.EMPLOYEE, subject)


async def _require_filing(request: Request, principal: Principal, subject: UUID) -> None:
    """Whether the caller may ask for this person's overtime.

    Only about themselves: `overtime.request_own` is self-only through
    `SELF_ONLY_ACTIONS`, so there is no path by which HR or a manager files overtime on
    somebody's behalf — which matters more here than anywhere else, because a request is
    the *only* way a record comes into existence.
    """
    await require_own(
        request, principal, Action.OVERTIME_REQUEST_OWN, ResourceKind.EMPLOYEE, subject
    )


# --- requests -----------------------------------------------------------------


@router.post(
    "/requests",
    response_model=RequestDetail,
    status_code=201,
    summary="Ask for overtime on a date",
    dependencies=[Depends(signed_in)],
)
async def draft_request(
    request: Request,
    payload: RequestCreate,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> RequestDetail:
    """Write the request. Nothing is approved and nothing is counted until it is.

    The date must be today or later in Madrid: overtime is applied for in advance, and
    this endpoint is the only way a day of overtime can enter the system. A request for
    a day that has passed is refused with that rule in the message rather than accepted
    and back-dated, and a day that already carries a request or a record is refused
    because overtime is counted once per person per day.
    """
    subject = payload.employee_id or principal.employee_id
    await _require_filing(request, principal, subject)
    view = await _service(session).draft(
        employee_id=subject,
        business_date=payload.business_date,
        expected_minutes=payload.expected_minutes,
        reason=payload.reason,
        actor_user_id=principal.user_id,
        actor_roles=principal.roles,
    )
    return _detail(view)


@router.get(
    "/requests",
    response_model=RequestPage,
    summary="List overtime requests",
    dependencies=[Depends(signed_in)],
)
async def list_requests(
    request: Request,
    employee_id: UUID | None = Query(
        default=None, description="Whose requests; defaults to the caller's own"
    ),
    state: OvertimeRequestState | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> RequestPage:
    """A page of one person's requests, newest first, each with its state."""
    subject = employee_id or principal.employee_id
    await _require_reading(request, principal, subject)
    views, total = await _service(session).list_requests(
        OvertimeRequestQuery(employee_id=subject, state=state, limit=limit, offset=offset)
    )
    return RequestPage(
        items=[_read(view) for view in views], total=total, limit=limit, offset=offset
    )


@router.get(
    "/requests/{request_id}",
    response_model=RequestDetail,
    summary="Read one overtime request, with the engine's history",
    dependencies=[Depends(signed_in)],
)
async def read_request(
    request: Request,
    request_id: UUID,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> RequestDetail:
    """The document and every round of its approval.

    Read before the permission is decided, because the subject is on the row: an unknown
    id is a 404 for anybody, and a document about somebody the caller cannot read is the
    403 the kernel answers with.
    """
    view = await _service(session).get(request_id)
    await _require_reading(request, principal, view.request.employee_id)
    return _detail(view)


@router.patch(
    "/requests/{request_id}",
    response_model=RequestDetail,
    summary="Change an overtime request that has not been filed",
    dependencies=[Depends(signed_in)],
)
async def update_request(
    request: Request,
    request_id: UUID,
    payload: RequestUpdate,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> RequestDetail:
    """Correct the draft — which is what a request returned for correction needs.

    Only a draft: a filed document is what two people were asked to approve, and an
    approved one is a record whose figure HR may confirm but nobody may rewrite here.
    The pre-approval rule is re-checked, so a draft cannot be edited back into the past
    any more than it could be created there.
    """
    service = _service(session)
    view = await service.get(request_id)
    await _require_filing(request, principal, view.request.employee_id)
    updated = await service.update(
        request_id,
        OvertimeRequestPatch(
            business_date=payload.business_date,
            expected_minutes=payload.expected_minutes,
            reason=payload.reason,
        ),
        actor_user_id=principal.user_id,
        actor_roles=principal.roles,
    )
    return _detail(updated)


@router.post(
    "/requests/{request_id}/submit",
    response_model=RequestDetail,
    summary="File an overtime request with the approval engine",
    dependencies=[Depends(signed_in)],
)
async def submit_request(
    request: Request,
    request_id: UUID,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> RequestDetail:
    """Hand the document to the engine, which resolves the route: the manager, then HR."""
    service = _service(session)
    view = await service.get(request_id)
    await _require_filing(request, principal, view.request.employee_id)
    return _detail(await service.submit(request_id))


@router.post(
    "/requests/{request_id}/decide",
    response_model=RequestDetail,
    summary="Approve, reject or return an overtime request at its current level",
    dependencies=[Depends(signed_in)],
)
async def decide_request(
    request: Request,
    request_id: UUID,
    payload: RequestDecision,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> RequestDetail:
    """Whoever the engine says approves this request, and nobody else.

    An approval *writes the record* in the same request — the approved minutes, in the
    month bucket of the day — and a rejection writes nothing and frees the day for a new
    request. This is the only route in the whole surface that leads to an overtime
    record, which is what "no retroactive entry" means: there is no other way in.
    """
    decided = await _service(session).decide(
        request_id,
        approver_employee_id=principal.employee_id,
        decision=payload.decision,
        comment=payload.comment,
        actor_user_id=principal.user_id,
        actor_roles=principal.roles,
    )
    return _detail(decided)


@router.post(
    "/requests/{request_id}/withdraw",
    response_model=RequestDetail,
    summary="Withdraw an overtime request that has not been approved",
    dependencies=[Depends(signed_in)],
)
async def withdraw_request(
    request: Request,
    request_id: UUID,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> RequestDetail:
    """Stop the request and give the day back — while it is still a request.

    Withdrawing is the requester's own act, so the guard is the self-only filing action
    applied to the document's subject: nobody withdraws somebody else's overtime. An
    approved request is a 409 naming HR's confirmation, because the record is the fact
    of the month and correcting it must not delete the day.
    """
    service = _service(session)
    view = await service.get(request_id)
    await _require_filing(request, principal, view.request.employee_id)
    withdrawn = await service.withdraw(
        request_id,
        actor_employee_id=principal.employee_id,
        actor_user_id=principal.user_id,
        actor_roles=principal.roles,
    )
    return _detail(withdrawn)


# --- records ------------------------------------------------------------------


@router.get(
    "/records",
    response_model=RecordPage,
    summary="List overtime records",
    dependencies=[Depends(signed_in)],
)
async def list_records(
    request: Request,
    employee_id: UUID | None = Query(
        default=None, description="Whose records; defaults to the caller's own"
    ),
    month: str | None = Query(default=None, description="A month to filter by, as YYYY-MM"),
    needs_confirmation: bool | None = Query(
        default=None, description="Only the records waiting for HR, when true"
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> RecordPage:
    """The records about one person, newest day first, each with the figures in force.

    `needs_confirmation=true` is HR's queue: the records whose approved and worked
    minutes differed by more than the configured threshold and which nobody has looked
    at yet.
    """
    subject = employee_id or principal.employee_id
    await _require_reading(request, principal, subject)
    records, total = await _service(session).list_records(
        OvertimeRecordQuery(
            employee_id=subject,
            month=month,
            needs_confirmation=needs_confirmation,
            limit=limit,
            offset=offset,
        )
    )
    return RecordPage(
        items=[_record(record) for record in records], total=total, limit=limit, offset=offset
    )


@router.get(
    "/records/{record_id}",
    response_model=RecordDetail,
    summary="Read one overtime record, with the history that produced its figures",
    dependencies=[Depends(signed_in)],
)
async def read_record(
    request: Request,
    record_id: UUID,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> RecordDetail:
    """The record and its ledger: approved, settled, and every confirmation after that.

    Read before the permission is decided, because the subject is on the row — an
    unknown id is a 404 for anybody, and somebody else's record is the 403 the kernel
    answers with.
    """
    view = await _service(session).get_record(record_id)
    await _require_reading(request, principal, view.record.employee_id)
    return _record_detail(view)


@router.post(
    "/records/{record_id}/confirm",
    response_model=RecordDetail,
    summary="Confirm or adjust the hours of a settled record",
    dependencies=[Depends(confirm_records)],
)
async def confirm_record(
    request: Request,
    record_id: UUID,
    payload: RecordConfirmation,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> RecordDetail:
    """Write HR's figure beside the computed one, with the reason.

    The computed minutes, the worked minutes and the approved minutes are never
    rewritten: the confirmed value is stored next to them and the ledger keeps what the
    record read before and after, which is what "保留原值与原因" asks for. Refused before
    the day has been settled — there would be no original to keep — and the day's
    `overtime_minutes` follows the confirmed figure, so the day and the file agree.
    """
    view = await _service(session).confirm(
        record_id,
        minutes=payload.minutes,
        note=payload.note,
        confirmed_by_employee_id=principal.employee_id,
        actor_user_id=principal.user_id,
        actor_roles=principal.roles,
    )
    return _record_detail(view)


# --- HR's period acts ---------------------------------------------------------


@router.post(
    "/settlements",
    response_model=SettlementRead,
    summary="Compare approved against actually worked for a period",
    dependencies=[Depends(settle_records)],
)
async def settle_month(
    payload: SettlementRequest,
    session: AsyncSession = Depends(db_session),
) -> SettlementRead:
    """Run the settlement for every record of the month whose day has ended.

    Idempotent: a record that is already settled is not a candidate, so running this
    twice settles nothing the second time. A record whose day has not ended is counted
    as skipped rather than settled against a total that is still growing — the day's
    worked minutes are only final once the day is.
    """
    service = _service(session)
    report = await service.settle_due(month=payload.month)
    return SettlementRead(
        settled=report.settled_count,
        skipped=report.skipped,
        failed=[
            SettlementFailureRead(
                record_id=failure.record_id, code=failure.code, detail=failure.detail
            )
            for failure in report.failed
        ],
    )


@router.get(
    "/summary",
    response_model=SummaryRead,
    summary="Read a month's overtime, per employee",
    dependencies=[Depends(signed_in)],
)
async def read_summary(
    request: Request,
    month: str = Query(description="The month to summarise, as YYYY-MM"),
    employee_id: UUID | None = Query(
        default=None, description="Whose total; defaults to the caller's own"
    ),
    everyone: bool = Query(
        default=False, description="Every employee's total: the company-wide read"
    ),
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> SummaryRead:
    """The month, grouped by `month_bucket`, per employee.

    `everyone=true` is the company-wide read (`overtime.read_all`: HR and finance) and
    the one shape that names no subject; anything else is decided by the kernel against
    the person named — your own total (`overtime.read_own`) or a report's
    (`overtime.read_report`).
    """
    service = _service(session)
    if everyone:
        await require_own(
            request,
            principal,
            Action.OVERTIME_READ_ALL,
            ResourceKind.EMPLOYEE,
            principal.employee_id,
        )
        summary = await service.summary(month)
    else:
        subject = employee_id or principal.employee_id
        await _require_reading(request, principal, subject)
        summary = await service.summary(month, employee_id=subject)
    return _summary(summary, service.threshold_minutes)


@router.get(
    "/export",
    summary="Export a month's overtime as CSV",
    dependencies=[Depends(export_records)],
    response_class=Response,
)
async def export_month(
    month: str = Query(description="The month to export, as YYYY-MM"),
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> Response:
    """The month's records as a file: employee number, name, department, date, minutes.

    Guarded by `overtime.export` — HR and finance, the two roles §4.1 gives the
    personnel and payroll sides — because the file carries the staff number, which is a
    withheld field. Every export writes an audit record naming the period and the
    exporter, and changes nothing else: finance re-running a month is expected, produces
    the same bytes, and leaves its own trace. The file holds minutes and nothing
    monetary — there is no rate, no multiplier and no amount anywhere in this module.
    """
    export = await _service(session).export_month(
        month, actor_user_id=principal.user_id, actor_roles=principal.roles
    )
    return Response(
        content=export.content,
        media_type=export.content_type,
        headers={"Content-Disposition": f'attachment; filename="{export.filename}"'},
    )
