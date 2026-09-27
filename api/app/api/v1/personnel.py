"""Personnel change endpoints: create, file, read, cancel.

Five endpoints, one document type, and the two rules the ticket is about:

* **Filing is not approving, and approving is not applying.** `submit` hands the
  document to the approval engine; nothing about the employee changes until the
  change's effective date arrives and the job applies it. There is deliberately no
  "apply now" endpoint: an effective date that a caller can skip is not an
  effective date.
* **Cancelling is for changes that have not taken effect.** An applied change is
  refused, with the alternative named — raise a counter-change — because a
  personnel record other records already refer to is not something an endpoint
  should rewrite.

Every endpoint asks the kernel for `employee.manage`, which is HR and
administration; the person a change is *about* never reaches it, and neither does
their manager. Read and write share one action because a personnel change is a
personnel file entry: whoever may read the file may see the moves in it.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import current_principal, db_session, require
from app.api.v1.schemas.personnel import (
    ApprovalDecisionRead,
    ApprovalDecisionRequest,
    ApprovalRead,
    FieldChangeRead,
    PersonnelChangeCancel,
    PersonnelChangeCreate,
    PersonnelChangeDetail,
    PersonnelChangePage,
    PersonnelChangeRead,
)
from app.cache import RedisSessionRevoker
from app.domain.access import Action, Principal, ResourceKind
from app.domain.approval.service import ApprovalService
from app.domain.employee.service import EmployeeService
from app.domain.notification.approval import ApprovalNotifier
from app.domain.notification.service import NotificationService
from app.domain.personnel.models import (
    ChangeQuery,
    ChangeState,
    ChangeType,
    PersonnelChangeView,
)
from app.domain.personnel.service import PersonnelChangeService
from app.repositories.account import PostgresAccountRepository
from app.repositories.approval import PostgresApprovalRepository
from app.repositories.employee import PostgresEmployeeRepository
from app.repositories.notification import PostgresNotificationRepository
from app.repositories.org import PostgresDepartmentRepository
from app.repositories.personnel import PostgresPersonnelChangeRepository

router = APIRouter(prefix="/personnel-changes", tags=["personnel"])

#: HR and administration. Nobody else reads or writes a personnel change, which is
#: what makes "a change for somebody else's employee" refuse a caller who may not
#: manage employees.
manage_employees = require(Action.EMPLOYEE_MANAGE, ResourceKind.EMPLOYEE)

#: Deciding is not a role: the engine answers "who approves this request", and the
#: endpoint only insists that the caller is somebody. Every signed-in user may
#: reach it and be refused by the rule that owns the question.
signed_in = require(Action.SESSION_READ_OWN, ResourceKind.ACCOUNT)


def _service(session: AsyncSession) -> PersonnelChangeService:
    """Assemble the module: its own repository, the engine, and the employee rules.

    The employee service is built **non-transactional** here. Applying a change
    writes an employee, an assignment and a termination in one unit, so the
    transaction belongs to the change and not to each write inside it — a service
    that commits per operation would leave the first half of a change behind when
    the second half failed.

    The engine is wrapped in `ApprovalNotifier`, so filing a change tells the next
    approver and a decision tells the requester. Wrapping it here rather than
    asking each caller to notify afterwards is the whole reason that decorator
    exists: a caller cannot forget a step it does not have to remember.

    The account repository and the Redis revoker are the termination's second half
    (ticket 18): applied on the effective date, a termination disables the login
    and ends its sessions. `RedisSessionRevoker` writes to Redis rather than to the
    database, so it is not part of the change's transaction and does not need to
    be — the epoch bump is, and the database is what a session check compares
    against.
    """
    approvals = PostgresApprovalRepository(session)
    return PersonnelChangeService(
        repository=PostgresPersonnelChangeRepository(session),
        session=session,
        approvals=ApprovalNotifier(
            engine=ApprovalService(approvals, session),
            notifications=NotificationService(
                PostgresNotificationRepository(session), session
            ),
            approvals=approvals,
        ),
        employees=EmployeeService(
            repository=PostgresEmployeeRepository(session),
            departments=PostgresDepartmentRepository(session),
            session=session,
            transactional=False,
        ),
        directory=PostgresEmployeeRepository(session),
        departments=PostgresDepartmentRepository(session),
        accounts=PostgresAccountRepository(session),
        revoker=RedisSessionRevoker(),
    )


def _read(view: PersonnelChangeView) -> PersonnelChangeRead:
    change = view.change
    return PersonnelChangeRead(
        id=change.id,
        change_type=change.change_type,
        employee_id=change.employee_id,
        effective_date=change.effective_date,
        state=view.state,
        status=change.status,
        changes=[
            FieldChangeRead(field=item.field, before=item.before, after=item.after)
            for item in change.changes
        ],
        applied_values=change.applied_values,
        applied_at=change.applied_at,
        cancelled_at=change.cancelled_at,
        cancelled_by_employee_id=change.cancelled_by_employee_id,
        cancel_reason=change.cancel_reason,
        created_by_employee_id=change.created_by_employee_id,
        created_at=change.created_at,
        updated_at=change.updated_at,
    )


def _detail(view: PersonnelChangeView) -> PersonnelChangeDetail:
    approval = view.approval
    return PersonnelChangeDetail(
        **_read(view).model_dump(),
        approval=(
            ApprovalRead(
                request_id=approval.id,
                status=str(approval.status),
                round=approval.round,
                submitted_at=approval.request.submitted_at,
                decided_at=approval.decided_at,
                decisions=[
                    ApprovalDecisionRead(
                        level=decision.level,
                        round=decision.round,
                        decision=str(decision.decision),
                        approver_employee_id=decision.approver_employee_id,
                        comment=decision.comment,
                        decided_at=decision.decided_at,
                    )
                    for decision in approval.decisions
                ],
            )
            if approval is not None
            else None
        ),
    )


@router.post(
    "",
    response_model=PersonnelChangeDetail,
    status_code=201,
    summary="Create a personnel change draft",
    dependencies=[Depends(manage_employees)],
)
async def create_change(
    payload: PersonnelChangeCreate,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> PersonnelChangeDetail:
    """Write the draft. It changes nothing about anybody until it is applied."""
    view = await _service(session).create(
        change_type=payload.change_type,
        effective_date=payload.effective_date,
        changes=[item.model_dump() for item in payload.changes],
        created_by_employee_id=principal.employee_id,
        employee_id=payload.employee_id,
    )
    return _detail(view)


@router.get(
    "",
    response_model=PersonnelChangePage,
    summary="List personnel changes",
    dependencies=[Depends(manage_employees)],
)
async def list_changes(
    employee_id: UUID | None = Query(default=None),
    #: The state the UI shows, which is the ticket's four states plus the two ways
    #: a document leaves them. Named `state` rather than `status` because the
    #: stored status cannot answer "approved and waiting to take effect" — that is
    #: the engine's answer.
    state: ChangeState | None = Query(default=None),
    change_type: ChangeType | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(db_session),
) -> PersonnelChangePage:
    views, total = await _service(session).list(
        ChangeQuery(
            employee_id=employee_id,
            state=state,
            change_type=change_type,
            limit=limit,
            offset=offset,
        )
    )
    return PersonnelChangePage(
        items=[_read(view) for view in views], total=total, limit=limit, offset=offset
    )


@router.get(
    "/{change_id}",
    response_model=PersonnelChangeDetail,
    summary="Read one personnel change",
    dependencies=[Depends(manage_employees)],
)
async def read_change(
    change_id: UUID,
    session: AsyncSession = Depends(db_session),
) -> PersonnelChangeDetail:
    return _detail(await _service(session).get(change_id))


@router.post(
    "/{change_id}/submit",
    response_model=PersonnelChangeDetail,
    summary="File a draft with the approval engine",
    dependencies=[Depends(manage_employees)],
)
async def submit_change(
    change_id: UUID,
    session: AsyncSession = Depends(db_session),
) -> PersonnelChangeDetail:
    """Two levels, from the engine, and still nothing applied.

    The request is filed as the change's author, so the route the engine resolves
    is theirs — a manager-and-HR pair, with self-approval passed over rather than
    handed to somebody else.
    """
    return _detail(await _service(session).submit(change_id))


@router.post(
    "/{change_id}/decide",
    response_model=PersonnelChangeDetail,
    summary="Approve, reject or return the change at its current level",
    dependencies=[Depends(signed_in)],
)
async def decide_change(
    change_id: UUID,
    payload: ApprovalDecisionRequest,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> PersonnelChangeDetail:
    """Whoever the engine says approves this document, and nobody else.

    The guard is deliberately "signed in" rather than a role: *who approves this
    request* is the engine's answer — the requester's manager at the first level,
    any HR member other than the requester at the second — and a role check here
    would be a second, weaker copy of that rule. Somebody who is not the approver
    is refused by the engine with a catalogued error, and the refusal is audited.

    Without this endpoint the two-level flow the ticket describes could be driven
    only by tests and jobs: a manager had no way to approve anything.
    """
    return _detail(
        await _service(session).decide(
            change_id,
            approver_employee_id=principal.employee_id,
            decision=payload.decision,
            comment=payload.comment,
            actor_user_id=principal.user_id,
            actor_roles=principal.roles,
        )
    )


@router.post(
    "/{change_id}/cancel",
    response_model=PersonnelChangeDetail,
    summary="Cancel a change that has not taken effect",
    dependencies=[Depends(manage_employees)],
)
async def cancel_change(
    change_id: UUID,
    payload: PersonnelChangeCancel,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> PersonnelChangeDetail:
    """Allowed while nothing has taken effect, whichever side of the effective
    date that is. Refused once it has, naming the counter-change instead."""
    return _detail(
        await _service(session).cancel(
            change_id,
            reason=payload.reason,
            actor_employee_id=principal.employee_id,
            actor_user_id=principal.user_id,
            actor_roles=principal.roles,
        )
    )
