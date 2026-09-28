"""Timesheet endpoints: your own week, the act of filing it, and the correction.

Nine routes, and every one of them answers about the caller. That is the ticket's
只能为本人填报 rule made structural rather than merely checked: `employee_id` defaults
to the caller, naming somebody else is a 403 decided by the kernel (`require_own`,
over the three self-only actions), and no route exists that would let a caller read a
week they are not the subject of.

**The week is a query parameter, and it is a date.** The convention is ticket 21's,
for a surface that answers about the caller: a week is identified by its Monday
rather than by a row id, so a client can open next week before a row exists — and the
`/{id}`-shaped alternative would have made "the week" a thing you have to have
created before you can look at it. A missing `week` is a 422: the grid is always for
a week somebody named, and "this week" is the client's notion of today, not the
server's.

**Reading creates nothing.** `GET /timesheets/week` answers seven empty days for a
week nobody has written, so an idle page load is not a write and "which weeks have I
filed" stays answerable from the rows that exist.

**The over-budget warning is server-computed and travels with the write.** It is in
the read *and* in the response to a submission, because "the week you just filed has
a 9-hour day against 8 expected" is exactly the moment it is worth saying. It never
blocks anything: the ticket is explicit that a long day is a warning and not a
refusal, and the day's expected hours are nowhere near the per-entry ceiling.

**Locking is the approval engine's, and the way through it is a supplement.** A
submitted week refuses edits because the engine's request is open; an approval locks
it for ever, and the route that changes what a locked week says is
`POST /timesheets/supplements` — which writes a *new* sheet beside the original with
a reversal and a replacement per corrected entry. The original is not edited by any
route here. Filing the correction is the same `POST /timesheets/submit`, because it
is the same two-level approval over a second document.
"""

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import current_principal, db_session, require, require_own
from app.api.v1.schemas.timesheet import (
    EntryUpdate,
    EntryWrite,
    SheetStatusRead,
    StatusRead,
    SupplementWrite,
    TimesheetPageRead,
    WeekRead,
    approval_read,
    timesheet_read,
    week_read,
)
from app.domain.access import Action, Principal, ResourceKind
from app.domain.approval.service import ApprovalService
from app.domain.notification.approval import ApprovalNotifier
from app.domain.notification.service import NotificationService
from app.domain.project.service import ProjectService
from app.domain.schedule.service import ScheduleService
from app.domain.timesheet.models import UNSET, EntryPatch, ProjectLabel, WeekView
from app.domain.timesheet.service import TimesheetService
from app.repositories.approval import PostgresApprovalRepository
from app.repositories.notification import PostgresNotificationRepository
from app.repositories.project import PostgresProjectRepository
from app.repositories.schedule import PostgresScheduleRepository
from app.repositories.timesheet import PostgresTimesheetRepository

router = APIRouter(prefix="/timesheets", tags=["timesheets"])

#: Everyone, about themselves. The role check is the catalogue's — every account
#: holds `employee` — and what makes all three self-only is the kernel's
#: `SELF_ONLY_ACTIONS`, which is why naming a colleague passes these dependencies and
#: is refused a line later, with an audited 403.
read_own = require(Action.TIMESHEET_READ_OWN, ResourceKind.TIMESHEET)
write_own = require(Action.TIMESHEET_WRITE_OWN, ResourceKind.TIMESHEET)
submit_own = require(Action.TIMESHEET_SUBMIT_OWN, ResourceKind.TIMESHEET)

#: The week, always named. Every screen that shows a grid has to say which week it is,
#: and a server-side default of "today" would be the server guessing at the client's
#: calendar.
WEEK_QUERY = Query(description="The Monday of the week, as an ISO date (YYYY-MM-DD)")
SUBJECT_QUERY = Query(
    default=None, description="Whose week; defaults to the caller, and anybody else is a 403"
)


def _service(session: AsyncSession, principal: Principal) -> TimesheetService:
    """Assemble the module: its own repository, and the three things it reads.

    The engine is wrapped in `ApprovalNotifier` — filing a week tells the manager, and
    a decision tells the employee — for the reason that decorator exists: a caller
    cannot forget a step it does not have to remember. A bare `ApprovalService` here
    would still record every decision and silently lose the notices that follow.
    """
    approvals = PostgresApprovalRepository(session)
    projects = PostgresProjectRepository(session)
    return TimesheetService(
        PostgresTimesheetRepository(session),
        session,
        principal=principal,
        projects=ProjectService(projects, session),
        project_repository=projects,
        expectations=ScheduleService(PostgresScheduleRepository(session), session),
        approvals=ApprovalNotifier(
            engine=ApprovalService(approvals, session),
            notifications=NotificationService(
                PostgresNotificationRepository(session), session
            ),
            approvals=approvals,
        ),
    )


async def _subject(
    request: Request, principal: Principal, action: Action, employee_id: UUID | None
) -> UUID:
    """Whose week, with the kernel deciding whether the caller may name them.

    The caller's own id when nothing is named. Anything else is a 403 recorded the way
    every other refusal in this API is, which is the ticket's requirement as code:
    只能为本人填报工时，代填返回 403.
    """
    subject = employee_id or principal.employee_id
    await require_own(request, principal, action, ResourceKind.TIMESHEET, subject)
    return subject


async def _week(service: TimesheetService, view: WeekView) -> WeekRead:
    """The grid, with each cell's project and task resolved.

    The lookup lives here rather than in the response model because it is a read: a
    shape that could trigger a query is a shape that fires one per row.
    """
    labels: dict[UUID, ProjectLabel] = await service.labels_for(
        [row for day in view.days for row in day.entries]
    )
    return week_read(view, labels)


# --- the week ---------------------------------------------------------------


@router.get(
    "/week",
    response_model=WeekRead,
    summary="Read one of your weeks, as a seven-day grid",
    dependencies=[Depends(read_own)],
)
async def read_week(
    request: Request,
    week: date = WEEK_QUERY,
    employee_id: UUID | None = SUBJECT_QUERY,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> WeekRead:
    """Seven days, Monday to Sunday, with their entries, totals and expectation.

    Read-only, and it creates nothing: a week nobody has written comes back as seven
    empty days with a status of `draft`. The schedule's answer for each day travels
    with it, so the grid can show "8 h expected" beside "9 h recorded" without a
    second request — and so the warning beside it is the server's, not the client's.
    """
    await _subject(request, principal, Action.TIMESHEET_READ_OWN, employee_id)
    service = _service(session, principal)
    return await _week(service, await service.read_week(week))


@router.get(
    "/week/status",
    response_model=StatusRead,
    summary="Read the approval state of one of your weeks",
    dependencies=[Depends(read_own)],
)
async def read_status(
    request: Request,
    week: date = WEEK_QUERY,
    employee_id: UUID | None = SUBJECT_QUERY,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> StatusRead:
    """The week's status, and the engine's history of it.

    `approval` is the original sheet's own `ApprovalState`: every round, every step and
    every decision with its comment. That is the 历史提交记录 the ticket asks to keep —
    a rejection followed by a correction and a resubmission is two rounds of one
    request, and both stay readable. Nothing mirrors it here, because two copies of
    "who rejected this and why" are two versions of the truth.

    `sheets` adds each correction's history beside it. A supplement is a document with
    an approval round of its own, so "the week" has several histories once it has been
    corrected, and a client that showed only the original's would hide the round the
    employee is actually waiting on.
    """
    await _subject(request, principal, Action.TIMESHEET_READ_OWN, employee_id)
    service = _service(session, principal)
    sheets = await service.sheets_of(week)
    view = await service.read_week(week)
    histories: list[SheetStatusRead] = []
    for sheet in sheets:
        state = await service.state_of_sheet(sheet.id)
        histories.append(
            SheetStatusRead(
                timesheet_id=sheet.id,
                status=sheet.status,
                is_supplementary=sheet.is_supplementary,
                corrects_timesheet_id=sheet.supersedes_id,
                submitted_at=sheet.submitted_at,
                approval_request_id=sheet.approval_request_id,
                approval=None if state is None else approval_read(state),
            )
        )
    primary = next((sheet for sheet in sheets if not sheet.is_supplementary), None)
    state = None if primary is None else await service.state_of_sheet(primary.id)
    return StatusRead(
        week_start=view.week_start,
        status=view.status,
        is_editable=view.is_editable,
        is_locked=view.is_locked,
        submitted_at=view.submitted_at,
        approval_request_id=view.approval_request_id,
        approval=None if state is None else approval_read(state),
        sheets=histories,
    )


@router.get(
    "/mine",
    response_model=TimesheetPageRead,
    summary="List your own weeks, newest first",
    dependencies=[Depends(read_own)],
)
async def list_weeks(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> TimesheetPageRead:
    """The weeks that have been written, and their statuses.

    Written weeks only: a week nobody has touched has no row, and inventing one per
    week of somebody's employment would make this list the calendar rather than the
    record. It is scoped to the caller by the query itself, with no parameter that
    could name anybody else.
    """
    page = await _service(session, principal).list_weeks(limit=limit, offset=offset)
    return TimesheetPageRead(
        items=[timesheet_read(sheet) for sheet in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )


@router.post(
    "/copy-previous",
    response_model=WeekRead,
    summary="Copy the previous week's entries into this one",
    dependencies=[Depends(write_own)],
)
async def copy_previous(
    request: Request,
    week: date = WEEK_QUERY,
    employee_id: UUID | None = SUBJECT_QUERY,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> WeekRead:
    """Fill an empty draft with the week before it. Entries only, never the status.

    Refused when the target week is not editable, and when it already holds anything:
    a copy that merged into existing rows would leave the employee with a duplicate
    they cannot see the origin of, and "copy" does not say "merge".
    """
    await _subject(request, principal, Action.TIMESHEET_WRITE_OWN, employee_id)
    service = _service(session, principal)
    return await _week(service, await service.copy_previous_week(week))


@router.post(
    "/submit",
    response_model=WeekRead,
    summary="File your week for approval",
    dependencies=[Depends(submit_own)],
)
async def submit_week(
    request: Request,
    week: date = WEEK_QUERY,
    employee_id: UUID | None = SUBJECT_QUERY,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> WeekRead:
    """Hand the week to the approval engine, and say whether any day ran long.

    The response is the whole grid, with the over-budget warning in it: a week that
    contains a day over its expectation is still filed — the ticket says a warning,
    not a refusal — and this is the moment the employee can still do something about
    it. Who approves is the engine's answer and is not restated here.
    """
    await _subject(request, principal, Action.TIMESHEET_SUBMIT_OWN, employee_id)
    service = _service(session, principal)
    return await _week(service, await service.submit(week))


# --- supplementary submissions (ticket 29) ----------------------------------


@router.post(
    "/supplements",
    response_model=WeekRead,
    status_code=201,
    summary="Correct a locked week with a supplementary submission",
    dependencies=[Depends(write_own)],
)
async def open_supplement(
    request: Request,
    payload: SupplementWrite,
    week: date = WEEK_QUERY,
    employee_id: UUID | None = SUBJECT_QUERY,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> WeekRead:
    """Open a correction beside a locked week, with a reversal per changed entry.

    The original is not edited: the response is the week's grid with the correction's
    rows in it, and the day totals are the *net* of the two sheets — which is what a
    reader takes a total to mean. `entries_total_minutes` therefore moves by exactly
    the difference the corrections ask for, and `reversal_total_minutes` says how much
    of it was cancelled.

    Refused when the week is not approved (there is nothing to correct), when the
    window has closed (nothing may write there at all), when a supplement is already
    in flight, or when a correction names something that is not a live entry of this
    week's original sheet. Filing it afterwards is `POST /timesheets/submit`, because
    the correction goes through the same two levels as any other week.
    """
    await _subject(request, principal, Action.TIMESHEET_WRITE_OWN, employee_id)
    service = _service(session, principal)
    view = await service.open_supplement(week, payload.to_inputs())
    return await _week(service, view)


# --- entries ----------------------------------------------------------------


@router.post(
    "/entries",
    response_model=WeekRead,
    status_code=201,
    summary="Add an entry to a week you are filling in",
    dependencies=[Depends(write_own)],
)
async def add_entry(
    request: Request,
    payload: EntryWrite,
    week: date = WEEK_QUERY,
    employee_id: UUID | None = SUBJECT_QUERY,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> WeekRead:
    """Record minutes on one task on one day, and answer with the whole grid.

    `is_billable` is deliberately absent from the body: the server resolves it from
    the task and its project, exactly as ticket 27's `record-time` decision endpoint
    does, and a request that names a task cannot make it billable.
    """
    await _subject(request, principal, Action.TIMESHEET_WRITE_OWN, employee_id)
    service = _service(session, principal)
    view = await service.add_entry(
        week,
        entry_date=payload.entry_date,
        project_id=payload.project_id,
        task_id=payload.task_id,
        minutes=payload.minutes,
        note=payload.note,
    )
    return await _week(service, view)


@router.patch(
    "/entries/{entry_id}",
    response_model=WeekRead,
    summary="Change one entry of a week you are filling in",
    dependencies=[Depends(write_own)],
)
async def update_entry(
    request: Request,
    entry_id: UUID,
    payload: EntryUpdate,
    week: date = WEEK_QUERY,
    employee_id: UUID | None = SUBJECT_QUERY,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> WeekRead:
    """Move or re-time one entry. Omitted keys are left alone; `note: null` clears it.

    The distinction is the schema's, and it matters: without it a note could be set
    and never removed.
    """
    await _subject(request, principal, Action.TIMESHEET_WRITE_OWN, employee_id)
    service = _service(session, principal)
    return await _week(service, await service.update_entry(week, entry_id, _patch(payload)))


@router.delete(
    "/entries/{entry_id}",
    response_model=WeekRead,
    summary="Remove one entry from a week you are filling in",
    dependencies=[Depends(write_own)],
)
async def remove_entry(
    request: Request,
    entry_id: UUID,
    week: date = WEEK_QUERY,
    employee_id: UUID | None = SUBJECT_QUERY,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> WeekRead:
    """Take a line out of a draft. A filed week refuses it, with its own code."""
    await _subject(request, principal, Action.TIMESHEET_WRITE_OWN, employee_id)
    service = _service(session, principal)
    return await _week(service, await service.remove_entry(week, entry_id))


def _patch(payload: EntryUpdate) -> EntryPatch:
    """Only the keys the client actually sent.

    `exclude_unset` is what keeps "omitted" apart from "explicitly null": the first
    leaves the note `UNSET` (leave it alone) and the second carries a `None` (clear
    it). Merging the two is how a note became impossible to remove.
    """
    sent = payload.model_dump(exclude_unset=True)
    return EntryPatch(
        entry_date=sent.get("entry_date"),
        project_id=sent.get("project_id"),
        task_id=sent.get("task_id"),
        minutes=sent.get("minutes"),
        note=sent["note"] if "note" in sent else UNSET,
    )


__all__ = ["router"]
