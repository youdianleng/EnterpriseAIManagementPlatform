"""The approval events, turned into notifications — without touching the engine.

`domain/approval/service.py` is not modified and does not know this module exists.
This is a decorator over it instead: the same four operations, and each one raises
the notifications the resulting `ApprovalState` owes. A caller constructs one
object where it used to construct the engine, which is what makes the
notifications hard to forget — an "and then call the notifier" step after every
call is a step somebody eventually omits.

**Every caller of the engine is expected to come through here.** Ticket 17's
personnel-change requests and the attendance and timesheet documents behind them
all approve through this object; a caller that reaches for `ApprovalService`
directly still gets its decisions recorded and loses the notifications that were
supposed to follow, silently, because nothing downstream can tell the difference.

**Who is told what:**

| Event | Who | Why |
|---|---|---|
| submitted, and again when level 1 approves | the next approver | it is their turn |
| approved at level 2 | the requester | the outcome |
| rejected | the requester | the outcome, and it is final |
| returned for correction | the requester | they have to correct it |
| withdrawn | the requester | it is closed, and this is the record of it |

Two decisions worth naming:

* **Level 2 has no approver, so it resolves to everyone holding `hr`.** The engine
  states the rule — the second level is a role, not a person, and whoever gets
  there first decides — so "it is their turn" is true of all of them at once. The
  list comes from the approval repository rather than from a query written here: a
  notification sender may *ask* who somebody is, it may not decide.
* **A withdrawal notifies the requester, who performed it.** It reads oddly until
  you ask what the alternative is: the approver whose queue just emptied has
  nothing to do, while the requester's centre gains the record that their request
  is closed — the same place the decision would have appeared had it gone the
  other way.
"""

from uuid import UUID

from app.domain.approval.models import (
    ApprovalState,
    ApprovalStatus,
    DecisionKind,
    SubmitContext,
)
from app.domain.approval.repository import ApprovalRepository
from app.domain.approval.service import ApprovalService
from app.domain.notification.models import (
    NotificationDraft,
    NotificationType,
    RaiseOutcome,
)
from app.domain.notification.service import NotificationService

#: The two levels, named so a payload reads as the engine's vocabulary rather than
#: as a bare integer.
LEVEL_ONE = 1
LEVEL_TWO = 2

#: The outcome token each requester-facing notification carries.
#:
#: Written here rather than read from the state's status, for two reasons: a return
#: leaves the request in `draft`, and "draft" is not what happened to it; and a
#: withdrawal taken while the request is already back in draft would otherwise
#: inherit the earlier round's `returned` — the same dedupe key as the notification
#: the requester already has, so the withdrawal would be suppressed as a duplicate
#: of the event it followed.
OUTCOME_OF: dict[NotificationType, str] = {
    NotificationType.APPROVAL_APPROVED: "approved",
    NotificationType.APPROVAL_REJECTED: "rejected",
    NotificationType.APPROVAL_RETURNED: "returned",
    NotificationType.APPROVAL_WITHDRAWN: "withdrawn",
}


class ApprovalNotifier:
    """`ApprovalService`'s four operations, with the notifications they owe."""

    def __init__(
        self,
        engine: ApprovalService,
        notifications: NotificationService,
        approvals: ApprovalRepository,
    ) -> None:
        self._engine = engine
        self._notifications = notifications
        #: The same repository the engine reads its route from, asked who holds
        #: `hr` so the second level's recipients are resolved by the module that
        #: owns that rule.
        self._approvals = approvals

    # --- the engine's operations, with notifications ------------------------

    async def submit(
        self,
        entity_type: str,
        entity_id: UUID,
        requester_employee_id: UUID,
        context: SubmitContext | None = None,
    ) -> UUID:
        request_id = await self._engine.submit(
            entity_type, entity_id, requester_employee_id, context
        )
        # Read back rather than assembled: whether level 1 was skipped is the
        # engine's answer, and the notification has to follow the step it wrote.
        state = await self._engine.state_of(entity_type, entity_id)
        if state is not None:
            await self.submitted(state)
        return request_id

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
        state = await self._engine.decide(
            request_id,
            approver_employee_id,
            decision,
            comment,
            actor_user_id=actor_user_id,
            actor_roles=actor_roles,
        )
        await self.decided(state)
        return state

    async def withdraw(self, request_id: UUID, requester_employee_id: UUID) -> ApprovalState:
        state = await self._engine.withdraw(request_id, requester_employee_id)
        await self.withdrawn(state)
        return state

    async def state_of(self, entity_type: str, entity_id: UUID) -> ApprovalState | None:
        """Passed through unchanged: reading a state raises nothing."""
        return await self._engine.state_of(entity_type, entity_id)

    # --- the events --------------------------------------------------------

    async def submitted(self, state: ApprovalState) -> list[RaiseOutcome]:
        """The request has just reached a level: tell whoever is waiting for it."""
        return await self._awaiting_decision(state)

    async def decided(self, state: ApprovalState) -> list[RaiseOutcome]:
        """The outcome, read from the state the engine returned.

        The state is the whole input on purpose: `pending_second` means level 1
        approved and the second level is now somebody's turn, `draft` means it was
        returned for correction (a request is never *created* in draft), and the
        two terminal statuses are the outcome the requester has been waiting for.
        A decision kind passed in alongside would be a second answer to a question
        the state already answers.
        """
        if state.status is ApprovalStatus.PENDING_SECOND:
            return await self._awaiting_decision(state)
        if state.status is ApprovalStatus.APPROVED:
            return [await self._to_requester(state, NotificationType.APPROVAL_APPROVED)]
        if state.status is ApprovalStatus.REJECTED:
            return [await self._to_requester(state, NotificationType.APPROVAL_REJECTED)]
        if state.status is ApprovalStatus.DRAFT:
            return [await self._to_requester(state, NotificationType.APPROVAL_RETURNED)]
        return []

    async def withdrawn(self, state: ApprovalState) -> list[RaiseOutcome]:
        return [await self._to_requester(state, NotificationType.APPROVAL_WITHDRAWN)]

    # --- recipients and payloads -------------------------------------------

    async def _awaiting_decision(self, state: ApprovalState) -> list[RaiseOutcome]:
        step = state.pending_step
        if step is None:
            # Nothing is pending, so nobody is being kept waiting. Reachable when a
            # caller raises this for a state that is already closed, which is a
            # caller bug rather than a notification to invent.
            return []

        recipients = (
            [step.approver_employee_id]
            if step.approver_employee_id is not None
            else [
                employee_id
                for employee_id in await self._approvals.hr_employee_ids()
                # The engine's own rule: the second level may never be the
                # requester, so neither may the notification asking for it.
                if employee_id != state.requester_employee_id
            ]
        )
        return [
            await self._notifications.notify(
                NotificationDraft(
                    recipient_employee_id=employee_id,
                    type=NotificationType.APPROVAL_AWAITING_DECISION,
                    payload={
                        **self._request_fields(state),
                        "level": step.level,
                        "requester_employee_id": str(state.requester_employee_id),
                    },
                    entity_type=state.entity_type,
                    entity_id=state.entity_id,
                    # One level of one round is one waiting period: a retry is the
                    # same event, the next round is a new one.
                    event=f"r{state.round}:l{step.level}",
                )
            )
            for employee_id in recipients
        ]

    async def _to_requester(
        self, state: ApprovalState, notification_type: NotificationType
    ) -> RaiseOutcome:
        outcome = OUTCOME_OF[notification_type]
        return await self._notifications.notify(
            NotificationDraft(
                recipient_employee_id=state.requester_employee_id,
                type=notification_type,
                payload={
                    **self._request_fields(state),
                    "level": self._decided_level(state),
                    # The token, for a client that wants to style the row without
                    # reading the key: the wording itself lives in the dictionary.
                    "outcome": outcome,
                },
                entity_type=state.entity_type,
                entity_id=state.entity_id,
                # One round reaches each of these at most once: a request can be
                # returned, rejected or approved once, and a withdrawal closes the
                # round it happened in. Level 1 approving is the exception — it
                # hands the round to level 2 — so that event is keyed as the
                # hand-off it is, in `event=f"r{round}:l2"` above.
                event=f"r{state.round}:{outcome}",
            )
        )

    @staticmethod
    def _request_fields(state: ApprovalState) -> dict[str, object]:
        """What every approval notification carries: which request, which attempt."""
        return {
            "approval_request_id": str(state.id),
            "round": state.round,
        }

    @staticmethod
    def _decided_level(state: ApprovalState) -> int:
        """Which level produced the outcome the state now shows.

        The last decision of the current round: decisions are read back ordered by
        level within a round, so the highest one is the one that just happened.
        """
        decided = state.decisions_of(state.round)
        return decided[-1].level if decided else LEVEL_ONE


__all__ = ["LEVEL_ONE", "LEVEL_TWO", "OUTCOME_OF", "ApprovalNotifier"]
