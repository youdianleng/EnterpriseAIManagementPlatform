"""Leave endpoints: the catalogue, your balances, your requests, and the calendar.

Four surfaces, and each is governed by catalogued actions of its own rather than by a
wider version of somebody else's:

* **The catalogue** (`/leave/types`) is published — it is what the company offers, and
  the form that files a request is rendered from it. Maintaining it is HR and
  administration, because its four flags decide what a leave costs somebody.
* **The balances** (`/leave/balances`) are the allowance: your own, your reports'
  through `leave.read_report`, anybody's through `leave.read_all`, and the whole
  company's for a year through the same action. Setting a figure is HR's
  (`leave.balance_manage`) — an allowance is granted, and what is filed against it is
  what goes down the approval chain.
* **The requests** (`/leave/requests`) are the document. Filing is self-only
  (`leave.request_own`), the two-level approval is the engine's — a manager at the
  first level and HR at the second — and a decision *settles the balance* in the same
  request: an approval that left the days reserved would be a decision that changed
  nothing. Withdrawing is the requester's own act and stops working once the leave has
  begun, at which point the refusal names HR's correction flow.
* **The calendar** (`/leave/calendar`) is the overlay an attendance screen draws:
  which dates somebody is away on approved leave. It is the visible half of the same
  fact the anomaly scan reads — a day of approved leave produces no `no_punches`
  anomaly.

**What a request payload may say, and what it may not.** The facts are the type, the
two dates and — for a type that asks for one — a reference to a separately stored
file. There is deliberately no `reason` field: `docs/DESIGN.md` §8 records the AEPD
position that a sick leave is a special category of data, and a free-text field on a
sick note is an invitation to write a diagnosis into the database. `StrictModel` makes
that a 422 rather than a silently dropped field. The attachment *reference* is shown
to whoever may read the request as `has_attachment` plus `attachment_readable_by`; the
reference itself is returned only to a caller holding `leave.attachment_read`, which
is HR alone — absent rather than blanked, the convention the employee projection
established.

**Who may see whose leave is a fact about the caller and the subject, not about the
route**, so the kernel is asked inside the handler — the convention the attendance
surface established in ticket 24. Naming somebody else where only your own is
reachable is a 403 recorded as `access.refused`, not an endpoint that quietly answers
about you.
"""

from datetime import date, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import current_principal, db_session, require, require_own
from app.api.v1.schemas.base import StrictModel
from app.config import get_settings
from app.domain.access import Action, Principal, Resource, ResourceKind, can
from app.domain.access.permissions import rule_for
from app.domain.approval.models import ApprovalState, DecisionKind
from app.domain.approval.service import ApprovalService
from app.domain.attendance.business_day import madrid_today
from app.domain.attendance.models import utc_now
from app.domain.leave.models import (
    BalanceGrant,
    LeaveBalanceEntry,
    LeaveBalanceView,
    LeaveDay,
    LeaveRequestQuery,
    LeaveRequestState,
    LeaveRequestView,
    LeaveType,
    LeaveTypeInput,
    LeaveTypePatch,
)
from app.domain.leave.service import LeaveService
from app.domain.notification.approval import ApprovalNotifier
from app.domain.notification.service import NotificationService
from app.domain.schedule.service import ScheduleService
from app.repositories.approval import PostgresApprovalRepository
from app.repositories.leave import PostgresLeaveRepository
from app.repositories.notification import PostgresNotificationRepository
from app.repositories.schedule import PostgresScheduleRepository

router = APIRouter(prefix="/leave", tags=["leave"])

#: The catalogue is published to everybody; maintaining it is HR and administration.
read_types = require(Action.LEAVE_TYPE_READ, ResourceKind.LEAVE_TYPE)
manage_types = require(Action.LEAVE_TYPE_MANAGE, ResourceKind.LEAVE_TYPE)

#: Deciding and reading are not roles: the engine answers "who approves this request",
#: and the kernel answers *whose* record this is, so the route only insists that the
#: caller is somebody. The same guard the personnel and attendance surfaces use.
signed_in = require(Action.SESSION_READ_OWN, ResourceKind.ACCOUNT)


class LeaveTypeRead(BaseModel):
    id: UUID
    code: str
    name_es: str
    name_en: str
    is_paid: bool
    requires_attachment: bool
    counts_against_annual: bool
    is_active: bool


class LeaveTypeCreate(StrictModel):
    """A new kind of leave. The code is its identity for good."""

    code: str = Field(min_length=2, max_length=32, description="Lowercase, e.g. sick")
    name_es: str = Field(min_length=1, max_length=160)
    name_en: str = Field(min_length=1, max_length=160)
    is_paid: bool = True
    requires_attachment: bool = False
    counts_against_annual: bool = False


class LeaveTypeUpdate(StrictModel):
    """What an edit may change. The code is deliberately not one of these."""

    name_es: str | None = Field(default=None, min_length=1, max_length=160)
    name_en: str | None = Field(default=None, min_length=1, max_length=160)
    is_paid: bool | None = None
    requires_attachment: bool | None = None
    counts_against_annual: bool | None = None
    is_active: bool | None = None


class BalanceEntryRead(BaseModel):
    """One movement, with the totals that followed it."""

    entry_type: str
    days: int
    entitled_days: int
    carried_over_days: int
    used_days: int
    pending_days: int
    remaining_days: int
    leave_request_id: UUID | None = None
    note: str | None = None
    created_at: datetime | None = None


class BalanceRead(BaseModel):
    """One year of one type, with the history that produced it.

    `projected` marks a year nobody has needed yet: the row is what the configured
    allowance *would* grant, `id` is null, and a client that wants the ledger account
    created files a request or lets HR set a figure. `remaining_days` is the figure a
    refusal quotes.
    """

    id: UUID | None
    employee_id: UUID
    year: int
    leave_type: str
    leave_type_name_es: str
    leave_type_name_en: str
    entitled_days: int
    carried_over_days: int
    used_days: int
    pending_days: int
    remaining_days: int
    projected: bool = False
    history: list[BalanceEntryRead] = Field(default_factory=list)


class BalancePage(BaseModel):
    """The balances a read reached, and the parameter they were materialised from.

    `annual_leave_days` is the configured allowance (D7). It travels with the rows so
    that "30 of what" is answerable from one response, and so that changing the
    setting is visible in the API rather than only in a new row's `entitled_days`.
    """

    items: list[BalanceRead]
    total: int
    annual_leave_days: int
    year: int | None = None


class BalanceSet(StrictModel):
    """What HR may set. Omitted fields are left alone."""

    entitled_days: int | None = Field(default=None, ge=0, le=366)
    carried_over_days: int | None = Field(default=None, ge=0, le=366)
    note: str | None = Field(
        default=None, max_length=500, description="Why the allowance changed; never medical"
    )


class RequestCreate(StrictModel):
    """A request for leave: which type, which dates, and the file if the type needs one.

    There is deliberately no `reason` and no `note`. Sending one is a 422, because this
    system does not hold free text about a leave — see the module docstring.
    """

    leave_type: str = Field(description="The type's code, e.g. annual or sick")
    start_date: date
    end_date: date
    attachment_reference: str | None = Field(
        default=None,
        max_length=200,
        description="Storage key of the separately stored file; required by some types",
    )
    employee_id: UUID | None = Field(
        default=None,
        description="Whose leave; defaults to the caller, and only ever the caller",
    )


class RequestDecision(StrictModel):
    """What an approver sends. Who may send it is the engine's answer."""

    decision: DecisionKind
    comment: str | None = Field(default=None, max_length=1000)


class AllocationRead(BaseModel):
    """Which year's balance a part of a request was charged to."""

    year: int
    days: int
    balance_id: UUID


class RequestRead(BaseModel):
    """The document, with the answers that are not on its own row.

    `state` is the field a client reads, `allocations` is the cross-year split as it
    was applied — read back from the ledger — and `attachment_reference` is present
    only for a caller who may read the file itself.
    """

    id: UUID
    employee_id: UUID
    leave_type: str
    start_date: date
    end_date: date
    business_days_count: int
    state: LeaveRequestState
    submitted_at: datetime | None = None
    approved_at: datetime | None = None
    withdrawn_at: datetime | None = None
    settled_at: datetime | None = None
    created_at: datetime | None = None
    allocations: list[AllocationRead] = Field(default_factory=list)
    #: Whether a file is attached at all — visible to everybody who may read the
    #: request, because an approver deciding a sick leave needs to know one exists.
    has_attachment: bool = False
    #: Which roles may read that file, from the action catalogue rather than from a
    #: constant here: the response states who may read it, which is what the ticket
    #: asks the request to say.
    attachment_readable_by: list[str] = Field(default_factory=list)
    #: The storage key itself, for those roles alone.
    attachment_reference: str | None = None


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
    request, and both stay readable: the history is what explains the state.

    **`initiated_by` and `confirmed_by_user_id` are the transparency annotation**
    (ticket 41). §6.3's fifth requirement is that the document an approver reads says
    「由助手起草、本人确认」 from an *API field* and never from a client-side guess, and
    §3.4 already keeps exactly these two columns on `approval_requests`: a request filed
    through the ordinary form is `initiated_by="user"` with nobody named as confirmer,
    and one the assistant drafted and a person confirmed is `agent` with that person's
    user id. The fields travel on the request *detail*, which is what an approver opens,
    so ticket 53 renders them rather than inventing a second source for the same fact.
    """

    request_id: UUID
    status: str
    round: int
    submitted_at: datetime | None = None
    decided_at: datetime | None = None
    decisions: list[ApprovalDecisionRead] = Field(default_factory=list)
    #: `user` | `agent` | `system`, straight from `approval_requests`.
    initiated_by: str = "user"
    #: Who confirmed an assistant-drafted request. Null for one a person filed directly.
    confirmed_by_user_id: UUID | None = None


class RequestDetail(RequestRead):
    approval: ApprovalRead | None = None


class RequestPage(BaseModel):
    items: list[RequestRead]
    total: int
    limit: int
    offset: int


class CalendarDayRead(BaseModel):
    """One date covered by approved leave."""

    employee_id: UUID
    business_date: date
    leave_type: str
    request_id: UUID


class CalendarRead(BaseModel):
    employee_id: UUID
    from_date: date
    to_date: date
    days: list[CalendarDayRead]


def _service(session: AsyncSession) -> LeaveService:
    """The module, wired as the attendance endpoints wire theirs.

    The engine is wrapped in `ApprovalNotifier` so a decision's notifications cannot be
    forgotten, and the scheduling module is handed over as the seam that answers "is
    this a working day" — this module never learns what a schedule is.
    """
    approvals = PostgresApprovalRepository(session)
    return LeaveService(
        PostgresLeaveRepository(session),
        session,
        expectations=ScheduleService(PostgresScheduleRepository(session), session),
        approvals=ApprovalNotifier(
            engine=ApprovalService(approvals, session),
            notifications=NotificationService(PostgresNotificationRepository(session), session),
            approvals=approvals,
        ),
        annual_leave_days=get_settings().annual_leave_days,
    )


def _type(row: LeaveType) -> LeaveTypeRead:
    return LeaveTypeRead(
        id=row.id,
        code=row.code,
        name_es=row.name_es,
        name_en=row.name_en,
        is_paid=row.is_paid,
        requires_attachment=row.requires_attachment,
        counts_against_annual=row.counts_against_annual,
        is_active=row.is_active,
    )


def _entry(row: LeaveBalanceEntry) -> BalanceEntryRead:
    return BalanceEntryRead(
        entry_type=row.entry_type.value,
        days=row.days,
        entitled_days=row.entitled_days,
        carried_over_days=row.carried_over_days,
        used_days=row.used_days,
        pending_days=row.pending_days,
        remaining_days=row.remaining_days,
        leave_request_id=row.leave_request_id,
        note=row.note,
        created_at=row.created_at,
    )


def _balance(view: LeaveBalanceView) -> BalanceRead:
    return BalanceRead(
        id=view.balance.id,
        employee_id=view.balance.employee_id,
        year=view.balance.year,
        leave_type=view.leave_type.code,
        leave_type_name_es=view.leave_type.name_es,
        leave_type_name_en=view.leave_type.name_en,
        entitled_days=view.balance.entitled_days,
        carried_over_days=view.balance.carried_over_days,
        used_days=view.balance.used_days,
        pending_days=view.balance.pending_days,
        remaining_days=view.balance.remaining_days,
        projected=view.projected,
        history=[_entry(item) for item in view.history],
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
        # Ticket 41's transparency annotation: §3.4's two columns, read off the engine's own
        # request rather than inferred from the caller or the document.
        initiated_by=state.request.initiated_by,
        confirmed_by_user_id=state.request.confirmed_by_user_id,
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


def _may_read_attachment(principal: Principal, subject: UUID) -> bool:
    """Whether the caller may be shown the file a request refers to.

    The kernel's answer, asked with the subject as the resource:
    `leave.attachment_read` is HR's, which is the design's §8 rule, and this is the one
    place a response shape depends on it.
    """
    return can(
        principal,
        Action.LEAVE_ATTACHMENT_READ,
        Resource(ResourceKind.EMPLOYEE, owner_employee_id=subject),
    ).allowed


def _read(view: LeaveRequestView, principal: Principal) -> RequestRead:
    request = view.request
    readable = _may_read_attachment(principal, request.employee_id)
    return RequestRead(
        id=request.id,
        employee_id=request.employee_id,
        leave_type=view.leave_type.code,
        start_date=request.start_date,
        end_date=request.end_date,
        business_days_count=request.business_days_count,
        state=view.state,
        submitted_at=request.submitted_at,
        approved_at=request.approved_at,
        withdrawn_at=request.withdrawn_at,
        settled_at=request.settled_at,
        created_at=request.created_at,
        allocations=[
            AllocationRead(year=item.year, days=item.days, balance_id=item.balance_id)
            for item in view.allocations
        ],
        has_attachment=request.attachment_reference is not None,
        attachment_readable_by=sorted(rule_for(Action.LEAVE_ATTACHMENT_READ).roles),
        attachment_reference=request.attachment_reference if readable else None,
    )


def _detail(view: LeaveRequestView, principal: Principal) -> RequestDetail:
    return RequestDetail(
        **_read(view, principal).model_dump(),
        approval=_approval(view.approval),
    )


def _calendar_day(day: LeaveDay) -> CalendarDayRead:
    return CalendarDayRead(
        employee_id=day.employee_id,
        business_date=day.business_date,
        leave_type=day.leave_type_code,
        request_id=day.request_id,
    )


async def _require_reading(request: Request, principal: Principal, subject: UUID) -> None:
    """Whether the caller may read this person's leave, and as what.

    Three catalogued actions answer this, and which one applies is a fact about the
    caller and the subject rather than about the route: your own leave
    (`leave.read_own`, self-only), a report's (`leave.read_report`, a manager's reach
    and only their reports') or the company's (`leave.read_all`, HR). So the kernel is
    asked, and the refusal is recorded against the action that was actually attempted —
    a manager reaching a colleague is refused `leave.read_report`, not the self-only
    action nobody asked for.
    """
    if subject == principal.employee_id:
        action = Action.LEAVE_READ_OWN
    elif can(
        principal,
        Action.LEAVE_READ_ALL,
        Resource(ResourceKind.EMPLOYEE, owner_employee_id=subject),
    ).allowed:
        action = Action.LEAVE_READ_ALL
    else:
        action = Action.LEAVE_READ_REPORT
    await require_own(request, principal, action, ResourceKind.EMPLOYEE, subject)


async def _require_filing(request: Request, principal: Principal, subject: UUID) -> None:
    """Whether the caller may file a request about this person.

    Only about themselves: `leave.request_own` is self-only through
    `SELF_ONLY_ACTIONS`, so there is no path by which HR or a manager files leave on
    somebody's behalf through this surface. HR adjusts the *allowance*
    (`leave.balance_manage`) and the record (the correction flow); the request is the
    employee's own act, which is what makes the approval route about them.
    """
    await require_own(
        request, principal, Action.LEAVE_REQUEST_OWN, ResourceKind.EMPLOYEE, subject
    )


# --- the catalogue ------------------------------------------------------------


@router.get(
    "/types",
    response_model=list[LeaveTypeRead],
    summary="List the leave types",
    dependencies=[Depends(read_types)],
)
async def list_types(
    include_inactive: bool = Query(default=False),
    session: AsyncSession = Depends(db_session),
) -> list[LeaveTypeRead]:
    """The catalogue. A retired type is readable on request, and not offered by default."""
    found = await _service(session).list_types(include_inactive=include_inactive)
    return [_type(row) for row in found]


@router.post(
    "/types",
    response_model=LeaveTypeRead,
    status_code=201,
    summary="Add a leave type",
    dependencies=[Depends(manage_types)],
)
async def create_type(
    payload: LeaveTypeCreate,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> LeaveTypeRead:
    """Add a kind of leave, with the flags that decide what it costs somebody."""
    created = await _service(session).create_type(
        LeaveTypeInput(
            code=payload.code,
            name_es=payload.name_es,
            name_en=payload.name_en,
            is_paid=payload.is_paid,
            requires_attachment=payload.requires_attachment,
            counts_against_annual=payload.counts_against_annual,
        ),
        actor_user_id=principal.user_id,
        actor_roles=principal.roles,
    )
    return _type(created)


@router.patch(
    "/types/{code}",
    response_model=LeaveTypeRead,
    summary="Change a leave type",
    dependencies=[Depends(manage_types)],
)
async def update_type(
    code: str,
    payload: LeaveTypeUpdate,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> LeaveTypeRead:
    """Change what the payload states. The code is not one of those things."""
    updated = await _service(session).update_type(
        code,
        LeaveTypePatch(
            name_es=payload.name_es,
            name_en=payload.name_en,
            is_paid=payload.is_paid,
            requires_attachment=payload.requires_attachment,
            counts_against_annual=payload.counts_against_annual,
            is_active=payload.is_active,
        ),
        actor_user_id=principal.user_id,
        actor_roles=principal.roles,
    )
    return _type(updated)


# --- balances -----------------------------------------------------------------


@router.get(
    "/balances",
    response_model=BalancePage,
    summary="Read leave balances, with the history that produced them",
    dependencies=[Depends(signed_in)],
)
async def read_balances(
    request: Request,
    employee_id: UUID | None = Query(
        default=None, description="Whose balances; defaults to the caller"
    ),
    year: int | None = Query(default=None, description="A year; omitted means all of them"),
    everyone: bool = Query(
        default=False, description="Every balance of the year: HR's company-wide read"
    ),
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> BalancePage:
    """The allowance, and how it was reached.

    `everyone=true` is HR's company-wide read (`leave.read_all`) and the one shape that
    does not name a subject; the others are decided by the kernel against the person
    named. The configured allowance travels with the rows, so a client can show "of the
    30 the company grants" without a second request.
    """
    service = _service(session)
    if everyone:
        await require_own(
            request,
            principal,
            Action.LEAVE_READ_ALL,
            ResourceKind.EMPLOYEE,
            principal.employee_id,
        )
        wanted = year or madrid_today(utc_now()).year
        views = await service.balances_for_year(wanted)
    else:
        subject = employee_id or principal.employee_id
        await _require_reading(request, principal, subject)
        views = await service.balances(subject, year=year)
    return BalancePage(
        items=[_balance(view) for view in views],
        total=len(views),
        annual_leave_days=service.annual_leave_days,
        year=year,
    )


@router.put(
    "/balances/{employee_id}/{year}/{code}",
    response_model=BalanceRead,
    summary="Set somebody's entitlement or carried-over days",
    dependencies=[Depends(signed_in)],
)
async def set_balance(
    request: Request,
    employee_id: UUID,
    year: int,
    code: str,
    payload: BalanceSet,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> BalanceRead:
    """Grant a figure, and record why. The one write here that is not a document.

    Guarded by `leave.balance_manage` inside the handler rather than by a route
    dependency, because the action is about a *person* — the same reason the reads are
    — and the refusal is recorded the same way. The resource is the person whose
    balance is being set, so the decision reads as "may this caller set this person's
    allowance".
    """
    await require_own(
        request, principal, Action.LEAVE_BALANCE_MANAGE, ResourceKind.EMPLOYEE, employee_id
    )
    view = await _service(session).set_balance(
        employee_id,
        year,
        code,
        BalanceGrant(
            entitled_days=payload.entitled_days,
            carried_over_days=payload.carried_over_days,
            note=payload.note,
        ),
        actor_user_id=principal.user_id,
        actor_roles=principal.roles,
        actor_employee_id=principal.employee_id,
    )
    return _balance(view)


# --- requests -----------------------------------------------------------------


@router.post(
    "/requests",
    response_model=RequestDetail,
    status_code=201,
    summary="Draft a leave request",
    dependencies=[Depends(signed_in)],
)
async def draft_request(
    request: Request,
    payload: RequestCreate,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> RequestDetail:
    """Write the request, having refused what could never be filed.

    The draft computes and stores the working days the range is worth — weekends and
    holidays excluded by the schedule module's own answer — and refuses a range with
    none, an overlapping request, a retired type, a missing attachment, an attachment
    reference that is not a storage key, and a balance that does not cover it. Nothing
    is reserved until the request is filed.
    """
    subject = payload.employee_id or principal.employee_id
    await _require_filing(request, principal, subject)
    view = await _service(session).draft(
        employee_id=subject,
        code=payload.leave_type,
        start_date=payload.start_date,
        end_date=payload.end_date,
        attachment_reference=payload.attachment_reference,
        actor_user_id=principal.user_id,
        actor_roles=principal.roles,
    )
    return _detail(view, principal)


@router.get(
    "/requests",
    response_model=RequestPage,
    summary="List leave requests",
    dependencies=[Depends(signed_in)],
)
async def list_requests(
    request: Request,
    employee_id: UUID | None = Query(
        default=None, description="Whose requests; defaults to the caller's own"
    ),
    state: LeaveRequestState | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> RequestPage:
    """A page of one person's requests, newest first, each with its state."""
    subject = employee_id or principal.employee_id
    await _require_reading(request, principal, subject)
    views, total = await _service(session).list_requests(
        LeaveRequestQuery(employee_id=subject, state=state, limit=limit, offset=offset)
    )
    return RequestPage(
        items=[_read(view, principal) for view in views],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/requests/{request_id}",
    response_model=RequestDetail,
    summary="Read one leave request, with the engine's history",
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
    return _detail(view, principal)


@router.post(
    "/requests/{request_id}/submit",
    response_model=RequestDetail,
    summary="File a leave request with the approval engine",
    dependencies=[Depends(signed_in)],
)
async def submit_request(
    request: Request,
    request_id: UUID,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> RequestDetail:
    """Reserve the days, then hand the document to the engine.

    The reservation comes first, so a request the year cannot afford is refused with the
    remainder before two people are asked to decide it. An insufficient balance is a 409
    whose detail names the four figures and what is left.
    """
    service = _service(session)
    view = await service.get(request_id)
    await _require_filing(request, principal, view.request.employee_id)
    return _detail(await service.submit(request_id), principal)


@router.post(
    "/requests/{request_id}/decide",
    response_model=RequestDetail,
    summary="Approve, reject or return a leave request at its current level",
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

    An approval *settles* the balance in the same request — the reserved days become
    spent ones and the leave becomes visible on the attendance calendar — and a
    rejection releases them. The notification the requester gets comes from the engine
    this module is wrapped around, not from here.
    """
    decided = await _service(session).decide(
        request_id,
        approver_employee_id=principal.employee_id,
        decision=payload.decision,
        comment=payload.comment,
        actor_user_id=principal.user_id,
        actor_roles=principal.roles,
    )
    return _detail(decided, principal)


@router.post(
    "/requests/{request_id}/withdraw",
    response_model=RequestDetail,
    summary="Withdraw a leave request before it starts",
    dependencies=[Depends(signed_in)],
)
async def withdraw_request(
    request: Request,
    request_id: UUID,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> RequestDetail:
    """Stop the leave and give the days back — while it is still in the future.

    Withdrawing is the requester's own act, so the guard is the self-only filing action
    applied to the document's subject: nobody withdraws somebody else's leave. A leave
    that has begun is a 409 naming HR's correction flow.
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
    return _detail(withdrawn, principal)


# --- the calendar -------------------------------------------------------------


@router.get(
    "/calendar",
    response_model=CalendarRead,
    summary="Read the days somebody is away on approved leave",
    dependencies=[Depends(signed_in)],
)
async def read_calendar(
    request: Request,
    from_date: date = Query(description="First date, inclusive"),
    to_date: date = Query(description="Last date, inclusive"),
    employee_id: UUID | None = Query(
        default=None, description="Whose calendar; defaults to the caller"
    ),
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> CalendarRead:
    """The leave overlay an attendance screen draws, for an inclusive range.

    Every calendar date an approved leave covers — weekends included, so a leave from
    Friday to Monday does not render as two leaves. Read through the same three actions
    as the balance and the request list.
    """
    subject = employee_id or principal.employee_id
    await _require_reading(request, principal, subject)
    days = await _service(session).calendar(subject, from_date, to_date)
    return CalendarRead(
        employee_id=subject,
        from_date=from_date,
        to_date=to_date,
        days=[_calendar_day(day) for day in days],
    )
