"""The approval engine.

One state machine for every kind of document, and exactly four operations
(`docs/architecture/codebase-design.md` §2.3): `submit`, `decide`, `withdraw`,
`state_of`.

**It knows nothing about what it approves.** A request stores an `entity_type`
and an `entity_id` and the engine never reads them; approving a leave request, a
timesheet and a personnel change are the same code path. A new kind of document
therefore needs no change here.

**"In force" is not this engine's business.** The engine answers whether something
was approved. When an approval takes *effect* belongs to the module that owns the
document: a scheduled task there reads its own approved-and-due rows and applies
them. An engine that understood effective dates would have to understand every
document's fields, and its depth would collapse the moment the second document
type arrived (§2.3).

Two rules that are easy to get wrong live here rather than at the edge:

* **Self-approval is refused, and the refusal is recorded.** When the route
  resolves to the requester — a department head filing their own request — the
  first level is written down as `skipped` with the reason, and the request goes
  straight to HR. It is not silently reassigned to somebody else, because the
  route named the requester's manager and nobody else is entitled to that step.
* **A rejection is final for the document; a return is not.** Returning sends the
  request back to draft for correction, and the next submission opens a new
  `round` while every earlier decision stays readable. A rejected document cannot
  be submitted again.
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditAction, record
from app.domain.approval.errors import ApprovalErrorCode
from app.domain.approval.models import (
    OUTCOME_OF,
    SELF_APPROVAL_REASON,
    ApprovalState,
    ApprovalStatus,
    DecisionKind,
    StepStatus,
    SubmitContext,
)
from app.domain.approval.repository import ApprovalRepository
from app.domain.errors import DomainError

#: Who may be recorded as having filed a request. Fixed, like every other role
#: vocabulary in this system.
INITIATOR_KINDS = frozenset({"user", "agent", "system"})

#: The two levels, in order. Fixed by DESIGN §3.4: the direct manager, then HR.
LEVEL_ONE = 1
LEVEL_TWO = 2


class ApprovalService:
    """The four operations. Nothing else is public.

    Takes both a repository and the session: the repository persists, and the
    session is what the audit record is written through, so a decision and the
    record of it are one transaction.
    """

    def __init__(self, repository: ApprovalRepository, session: AsyncSession) -> None:
        self._repository = repository
        self._session = session

    # --- submit -------------------------------------------------------------

    async def submit(
        self,
        entity_type: str,
        entity_id: UUID,
        requester_employee_id: UUID,
        context: SubmitContext | None = None,
    ) -> UUID:
        """File a request, or file the next round of one that was returned.

        Resubmitting is this operation rather than a fifth one: a returned request
        is back in draft, and the caller that has corrected the document says so by
        submitting the same entity again.
        """
        filed = context or SubmitContext()
        if filed.initiated_by not in INITIATOR_KINDS:
            raise DomainError(
                ApprovalErrorCode.INVALID_REQUEST,
                detail=f"unknown initiator {filed.initiated_by!r}",
            )

        if not await self._repository.employee_exists(requester_employee_id):
            raise DomainError(
                ApprovalErrorCode.EMPLOYEE_NOT_FOUND,
                detail=f"unknown employee {requester_employee_id}",
            )

        open_request = await self._repository.find_open(entity_type, entity_id)
        if open_request is not None and open_request.status is not ApprovalStatus.DRAFT:
            raise DomainError(
                ApprovalErrorCode.APPROVAL_ALREADY_OPEN,
                detail=f"{entity_type} {entity_id} already has a request in {open_request.status}",
            )

        if open_request is None:
            latest = await self._repository.latest_for(entity_type, entity_id)
            if latest is not None and latest.status is ApprovalStatus.REJECTED:
                raise DomainError(
                    ApprovalErrorCode.APPROVAL_PREVIOUSLY_REJECTED,
                    detail=f"{entity_type} {entity_id} was rejected and is final",
                )

        # Resolved on every submission, including a resubmission: a manager may
        # have changed while the request sat in draft, and the route that gets
        # recorded has to be the one in force when the document was filed.
        level_one = await self._resolve_level_one(requester_employee_id)
        await self._require_an_hr_approver(requester_employee_id)

        now = datetime.now(UTC)
        if open_request is None:
            request_id = await self._repository.create_request(
                entity_type,
                entity_id,
                requester_employee_id,
                initiated_by=filed.initiated_by,
                confirmed_by_user_id=filed.confirmed_by_user_id,
            )
            round_number = 1
        else:
            request_id = open_request.id
            round_number = open_request.round + 1

        if level_one == requester_employee_id:
            await self._skip_level_one(
                request_id=request_id,
                round_number=round_number,
                requester_employee_id=requester_employee_id,
                entity_type=entity_type,
                entity_id=entity_id,
                at=now,
            )
            status = ApprovalStatus.PENDING_SECOND
            await self._repository.add_step(
                request_id,
                level=LEVEL_TWO,
                round_number=round_number,
                approver_employee_id=None,
                status=StepStatus.PENDING,
            )
        else:
            status = ApprovalStatus.PENDING_FIRST
            await self._repository.add_step(
                request_id,
                level=LEVEL_ONE,
                round_number=round_number,
                approver_employee_id=level_one,
                status=StepStatus.PENDING,
            )

        await self._repository.save_submission(
            request_id, round_number=round_number, status=status, submitted_at=now
        )
        await record(
            self._session,
            action=AuditAction.APPROVAL_SUBMITTED,
            entity_type=entity_type,
            entity_id=entity_id,
            after={
                "approval_request_id": str(request_id),
                "round": round_number,
                "status": str(status),
                "requester_employee_id": str(requester_employee_id),
                "level_one_approver_employee_id": str(level_one),
                "initiated_by": filed.initiated_by,
            },
        )
        await self._repository.commit()
        return request_id

    # --- decide -------------------------------------------------------------

    async def decide(
        self,
        request_id: UUID,
        approver_employee_id: UUID,
        decision: DecisionKind,
        comment: str | None = None,
        *,
        actor_user_id: UUID | None = None,
        actor_roles: frozenset[str] | None = None,
    ) -> ApprovalState:
        """Record one level's decision, and move the request accordingly.

        The actor is passed explicitly because the engine's callers may have it
        and the request context may not: a decision taken by a background worker
        has no HTTP request to read it from.
        """
        state = await self._require(request_id)
        if state.status not in (ApprovalStatus.PENDING_FIRST, ApprovalStatus.PENDING_SECOND):
            raise DomainError(
                ApprovalErrorCode.APPROVAL_NOT_PENDING,
                detail=f"request {request_id} is {state.status}",
            )
        step = state.pending_step
        if step is None:
            # Only reachable if a step row went missing; refusing beats reading a
            # request as "pending something that does not exist".
            raise DomainError(
                ApprovalErrorCode.APPROVAL_NOT_PENDING,
                detail=f"request {request_id} is {state.status} with no pending step",
            )

        await self._authorise(state, step.level, step.approver_employee_id, approver_employee_id)

        outcome = OUTCOME_OF[DecisionKind(decision)]
        now = datetime.now(UTC)

        await self._repository.decide_step(step.id, status=outcome, decided_at=now)
        await self._repository.add_decision(
            request_id=request_id,
            level=step.level,
            round_number=state.round,
            approver_employee_id=approver_employee_id,
            decision=outcome,
            comment=comment,
            decided_at=now,
        )
        await self._audit_decision(
            request_id=request_id,
            entity_type=state.entity_type,
            entity_id=state.entity_id,
            level=step.level,
            decision=outcome,
            approver_employee_id=approver_employee_id,
            comment=comment,
            actor_user_id=actor_user_id,
            actor_roles=actor_roles,
        )

        if outcome is StepStatus.APPROVED:
            if step.level == LEVEL_ONE:
                # The second level is a role, not a person, so its step is written
                # without an approver: whoever holds hr and gets there first is
                # entitled to decide it.
                awaiting = ApprovalStatus.PENDING_SECOND
                decided_at = None
                await self._repository.add_step(
                    request_id,
                    level=LEVEL_TWO,
                    round_number=state.round,
                    approver_employee_id=None,
                    status=StepStatus.PENDING,
                )
            else:
                awaiting = ApprovalStatus.APPROVED
                decided_at = now
        elif outcome is StepStatus.REJECTED:
            awaiting = ApprovalStatus.REJECTED
            decided_at = now
        else:
            # Returned for correction: back to the requester, the same round
            # closed and the next one opened by the next submission. Nothing was
            # decided, so `decided_at` stays empty — the decision row keeps the
            # timestamp of the return itself.
            awaiting = ApprovalStatus.DRAFT
            decided_at = None

        await self._repository.save_outcome(request_id, status=awaiting, decided_at=decided_at)
        await self._repository.commit()
        return await self._require(request_id)

    # --- withdraw -----------------------------------------------------------

    async def withdraw(
        self, request_id: UUID, requester_employee_id: UUID
    ) -> ApprovalState:
        """Take a request back, while it is still the requester's to take back.

        Up to the first level: once HR has it, the requester is no longer the only
        party with an interest in the outcome, and the way out is a decision.
        """
        state = await self._require(request_id)
        if state.requester_employee_id != requester_employee_id:
            raise DomainError(
                ApprovalErrorCode.APPROVAL_NOT_REQUESTER,
                detail=f"request {request_id} was filed by {state.requester_employee_id}",
            )
        if state.status not in (ApprovalStatus.DRAFT, ApprovalStatus.PENDING_FIRST):
            raise DomainError(
                ApprovalErrorCode.APPROVAL_NOT_WITHDRAWABLE,
                detail=f"request {request_id} is {state.status}",
            )

        await self._repository.save_outcome(
            request_id, status=ApprovalStatus.WITHDRAWN, decided_at=datetime.now(UTC)
        )
        await record(
            self._session,
            action=AuditAction.APPROVAL_WITHDRAWN,
            entity_type=state.entity_type,
            entity_id=state.entity_id,
            after={
                "approval_request_id": str(request_id),
                "round": state.round,
                "from_status": str(state.status),
                "requester_employee_id": str(requester_employee_id),
            },
        )
        await self._repository.commit()
        return await self._require(request_id)

    # --- state --------------------------------------------------------------

    async def state_of(self, entity_type: str, entity_id: UUID) -> ApprovalState | None:
        """The entity's current request, or nothing if it never had one.

        The latest request wins, so after a withdrawal and a fresh submission the
        answer is the one in flight rather than the abandoned one.
        """
        return await self._repository.latest_for(entity_type, entity_id)

    # --- internals ----------------------------------------------------------

    async def _require(self, request_id: UUID) -> ApprovalState:
        state = await self._repository.get(request_id)
        if state is None:
            raise DomainError(
                ApprovalErrorCode.APPROVAL_NOT_FOUND, detail=f"unknown request {request_id}"
            )
        return state

    async def _resolve_level_one(self, requester_employee_id: UUID) -> UUID:
        """Who approves first: the primary position's manager, else its department's.

        The primary position is the one the person is principally in, so a second
        assignment does not change who signs off their requests. A department
        manager is the fallback because a department of one still has somebody
        accountable for it — but if neither names anybody, the request is refused
        rather than filed into a queue nobody owns.
        """
        position = await self._repository.primary_position(requester_employee_id)
        if position is not None:
            if position.manager_employee_id is not None:
                return position.manager_employee_id
            manager = await self._repository.department_manager(position.department_id)
            if manager is not None:
                return manager

        raise DomainError(
            ApprovalErrorCode.APPROVAL_APPROVER_UNRESOLVED,
            detail=(
                f"no approver configured on the primary position or its department "
                f"for employee {requester_employee_id}"
            ),
        )

    async def _require_an_hr_approver(self, requester_employee_id: UUID) -> None:
        """Refuse a request nobody could ever finish.

        Checked at submission rather than discovered when the first level approves:
        a request that cannot reach its second level is a document stuck in a queue,
        which is worse than a refusal the requester can act on. An HR requester
        still needs a *different* HR person, since the second level may never be
        their own.
        """
        hr = await self._repository.hr_employee_ids()
        if not any(candidate != requester_employee_id for candidate in hr):
            raise DomainError(
                ApprovalErrorCode.APPROVAL_HR_UNAVAILABLE,
                detail="no hr approver other than the requester",
            )

    async def _authorise(
        self,
        state: ApprovalState,
        level: int,
        step_approver_employee_id: UUID | None,
        approver_employee_id: UUID,
    ) -> None:
        """Who may decide this level, and nobody else.

        Nobody decides their own request, at either level. Level 1 is the person
        the route resolved to; level 2 is any holder of the `hr` role other than
        the requester — a role rather than a name, so the second review does not
        depend on one individual being at their desk.

        An administrator has no path here. The engine decides on the route and the
        role, and administration is neither.
        """
        if approver_employee_id == state.requester_employee_id:
            raise DomainError(
                ApprovalErrorCode.APPROVAL_NOT_APPROVER,
                detail=f"employee {approver_employee_id} filed request {state.id}",
            )
        if level == LEVEL_ONE:
            if approver_employee_id == step_approver_employee_id:
                return
            raise DomainError(
                ApprovalErrorCode.APPROVAL_NOT_APPROVER,
                detail=(
                    f"request {state.id} level 1 belongs to "
                    f"{step_approver_employee_id}, not {approver_employee_id}"
                ),
            )
        if await self._repository.holds_hr_role(approver_employee_id):
            return
        raise DomainError(
            ApprovalErrorCode.APPROVAL_NOT_APPROVER,
            detail=f"employee {approver_employee_id} does not hold hr",
        )

    async def _skip_level_one(
        self,
        *,
        request_id: UUID,
        round_number: int,
        requester_employee_id: UUID,
        entity_type: str,
        entity_id: UUID,
        at: datetime,
    ) -> None:
        """Record that the first level is passed over, and why.

        Both rows matter: the step is what makes the round's shape readable, and
        the decision is what makes the reason readable. A skipped step with no
        decision would look like a level nobody got to.
        """
        await self._repository.add_step(
            request_id,
            level=LEVEL_ONE,
            round_number=round_number,
            approver_employee_id=requester_employee_id,
            status=StepStatus.SKIPPED,
            decided_at=at,
        )
        await self._repository.add_decision(
            request_id=request_id,
            level=LEVEL_ONE,
            round_number=round_number,
            approver_employee_id=requester_employee_id,
            decision=StepStatus.SKIPPED,
            comment=SELF_APPROVAL_REASON,
            decided_at=at,
        )
        await self._audit_decision(
            request_id=request_id,
            entity_type=entity_type,
            entity_id=entity_id,
            level=LEVEL_ONE,
            decision=StepStatus.SKIPPED,
            approver_employee_id=requester_employee_id,
            comment=SELF_APPROVAL_REASON,
            actor_user_id=None,
            actor_roles=None,
        )

    async def _audit_decision(
        self,
        *,
        request_id: UUID,
        entity_type: str,
        entity_id: UUID,
        level: int,
        decision: StepStatus,
        approver_employee_id: UUID,
        comment: str | None,
        actor_user_id: UUID | None,
        actor_roles: frozenset[str] | None,
    ) -> None:
        """One record per decision, in the same transaction as the decision.

        Keyed on the *document* rather than on the request, so "everything that
        happened to this leave request" is one equality filter; the request is
        named in the record's body. The approver is an employee id, because that
        is who decides — the acting user is recorded separately, by `record`.
        """
        await record(
            self._session,
            action=AuditAction.APPROVAL_DECIDED,
            entity_type=entity_type,
            entity_id=entity_id,
            actor_user_id=actor_user_id,
            actor_roles=actor_roles,
            after={
                "approval_request_id": str(request_id),
                "level": level,
                "decision": decision.value,
                "approver_employee_id": str(approver_employee_id),
                "comment": comment,
            },
        )


__all__ = ["INITIATOR_KINDS", "LEVEL_ONE", "LEVEL_TWO", "ApprovalService"]
