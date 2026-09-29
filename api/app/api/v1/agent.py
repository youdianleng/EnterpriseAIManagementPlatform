"""The agent draft endpoints: the two things a person may do with a draft.

Two routes, and between them they are the whole of DESIGN §6.3's second requirement:
``POST /agent/actions/{action_id}/confirm`` creates the document a form describes, and
``POST /agent/actions/{action_id}/reject`` records that the employee said no. There is
deliberately no third route — no "answer the interrupt", no "send the confirmation as a
message" — because the ticket's first checklist line is that a *click* is what creates
something, and the surest way to keep that true is for the chat path to have no way to
reach these functions at all.

**Why the guard is `session.read_own` and the action check is inside.** A draft belongs to
a conversation (§3.6 gives it `conversation_id` and `user_id`), and ticket 37's four
conversation routes all carry `session.read_own` for the reason recorded there: a
conversation is a surface that answers about the caller, every role holds its own, and a
second action with an identical role list would be one rule spelled twice. What that guard
does *not* say is whether the caller may file the document the draft describes — and that
is the point of the second check, made inside ``ConfirmationService`` with the kernel, for
the entity's own action: ``leave.request_own``, ``attendance.correction_own``,
``timesheet.write_own`` and ``timesheet.submit_own``. So a caller whose reach has changed
is refused by the document's own rule rather than by a wider one, and the refusal names
that rule.

**A 404 for somebody else's draft, and not a 403.** The row's own `WHERE user_id` makes "no
such draft" and "not yours" the same answer, which is the property that stops this endpoint
reporting which draft ids exist — the same decision the conversation read records.

**The body is the *edited form*, and only the fields that changed.** §6.3's first
requirement is that every field is editable, so the client posts the values the person
actually confirmed. The values are strings because that is what a browser sends, and
``domain/agent/confirmation.py::confirmed_values`` is the one place they become the
request models' types. An empty body is legal and means "the assistant's values, unchanged"
— which is precisely the click the ticket is about, and refusing it would make the honest
case (a person presses the button without editing anything) the hard one.

**A rejection's reason is optional, and the design system is why.** §6.4 requires a reason
when an approver *rejects a request* — because a rejection without one sends the employee
back to guess. Discarding one's own draft is not that flow: the draft is gone either way,
nobody is waiting for an explanation, and requiring a sentence before a person may clear
their own screen would be a form for its own sake. The reason travels to the log and is
deliberately not stored (see `ConfirmationService.reject`).
"""

from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import current_principal, db_session, require
from app.api.v1.schemas.base import StrictModel
from app.core.errors import AppError, ErrorCode
from app.domain.access import Action, Principal, ResourceKind
from app.domain.agent.confirmation import (
    ConfirmationRefused,
    DraftMissing,
    DraftNotConfirmable,
    FieldUnusable,
    service_for,
)
from app.domain.agent.models import AgentAction

router = APIRouter(prefix="/agent", tags=["agent"])

#: The same guard ticket 37's four conversation routes carry, and for the same reason: a
#: draft is conversation material, and this is a surface that answers about the caller.
#: The *document's* permission is asked separately, inside the service, against the action
#: that filing it would need.
read_own_draft = require(Action.SESSION_READ_OWN, ResourceKind.ACCOUNT)


class DraftDecision(StrictModel):
    """What a person may say about a draft, and nothing else.

    `fields` is the form as edited — one key per field the submission will write, named
    exactly as `PrefillForm` names them. Omitted keys keep the proposed value, so a client
    that posts only what changed is correct rather than incomplete.

    `reason` is the employee's own note about discarding the draft. It is optional (see the
    module docstring) and is never stored on the row.
    """

    fields: dict[str, str | int | None] = Field(
        default_factory=dict,
        description=(
            "The confirmed form values, keyed by the submission's own field names. "
            "Omitted fields keep the values the assistant proposed."
        ),
    )
    reason: str | None = Field(default=None, max_length=500)


class DraftDecisionRead(BaseModel):
    """What became of the draft, and what it became.

    `entity_type` and `entity_id` are `null` for a rejection and never null for a
    confirmation — the same "both or neither" the `agent_actions` constraint states in SQL.
    `status` is the row's own vocabulary (`confirmed` / `rejected`), so a client renders the
    outcome it was actually given rather than the route it called.
    """

    id: UUID
    tool_name: str
    status: str
    confirmed_at: str | None = None
    entity_type: str | None = None
    entity_id: UUID | None = None


@router.post(
    "/actions/{action_id}/confirm",
    response_model=DraftDecisionRead,
    status_code=201,
    summary="Confirm a draft the assistant proposed, and file it as yourself",
    dependencies=[Depends(read_own_draft)],
)
async def confirm_draft(
    action_id: UUID,
    payload: DraftDecision,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> DraftDecisionRead:
    """The explicit click: create the document, as the employee, down the ordinary route.

    Every refusal this route can produce is a catalogued one, and the three that matter are
    the ticket's own negative cases:

    * **`ERR_AGT_001`** — no such draft for this caller (never existed, or somebody else's).
    * **`ERR_AGT_002`** — the draft is not `proposed` any more: already confirmed, already
      rejected, or lapsed. §6.3's 「过期后…需重新生成」 arrives here, and the row's own status
      has been written to `expired` by the time the client reads the answer.
    * **`ERR_AGT_003`** — the caller may no longer file this *(a permission that changed)* or
      the document's own rules refuse the contents *(the balance somebody else spent)*. The
      message key inside the envelope tells the employee which, and both end in "ask for a
      new draft".

    A 403 is not one of them: the permission refusal is reported as a *confirmation*
    refusal, because "you may not do this any more" about a form the person is looking at
    is a different sentence from "you may not open this screen".
    """
    service = service_for(session)
    try:
        confirmed = await service.confirm(
            action_id=action_id, principal=principal, fields=payload.fields
        )
    except DraftMissing as missing:
        raise AppError(ErrorCode.AGENT_DRAFT_NOT_FOUND, detail=str(missing)) from missing
    except DraftNotConfirmable as closed:
        raise AppError(
            ErrorCode.AGENT_DRAFT_NOT_CONFIRMABLE,
            detail=f"the draft is {closed.status}; it can no longer be confirmed",
        ) from closed
    except FieldUnusable as unusable:
        raise AppError(
            ErrorCode.VALIDATION_FAILED,
            detail=f"{unusable.field}: {unusable.detail}",
        ) from unusable
    except ConfirmationRefused as refused:
        # The code is this module's own — the employee owns the draft and the answer is
        # about *that* — and the sentence is the domain's, naming the rule that moved. The
        # two travel together on purpose: a client routes on `ERR_AGT_003` and renders
        # `errors.leave_balance_insufficient` (or the week's, or the project's), which is what
        # tells the employee whether asking again could possibly help.
        raise AppError(
            ErrorCode.AGENT_DRAFT_CONFIRMATION_REFUSED,
            detail=f"{refused.message_key}: {refused.detail}",
            message_key=refused.message_key,
        ) from refused
    return _read(confirmed.action)


@router.post(
    "/actions/{action_id}/reject",
    response_model=DraftDecisionRead,
    summary="Discard a draft the assistant proposed, and create nothing",
    dependencies=[Depends(read_own_draft)],
)
async def reject_draft(
    action_id: UUID,
    payload: DraftDecision,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> DraftDecisionRead:
    """The other half of the click: the draft is recorded as rejected and nothing is written.

    "Nothing is written" is a fact about the *entity* tables, and the response says so in
    the only way a caller can check: `entity_type` and `entity_id` come back `null`.
    `agent_actions` does move — that is the whole point of an audit trail, and a rejection
    that left no trace would make "the employee refused this" indistinguishable from "the
    assistant never proposed anything".

    The two refusals are the confirm route's first two: an unknown draft, and a draft that
    is no longer waiting for an answer.
    """
    service = service_for(session)
    try:
        rejected = await service.reject(
            action_id=action_id, principal=principal, reason=payload.reason
        )
    except DraftMissing as missing:
        raise AppError(ErrorCode.AGENT_DRAFT_NOT_FOUND, detail=str(missing)) from missing
    except DraftNotConfirmable as closed:
        raise AppError(
            ErrorCode.AGENT_DRAFT_NOT_CONFIRMABLE,
            detail=f"the draft is {closed.status}; it can no longer be answered",
        ) from closed
    return _read(rejected.action)


def _read(action: AgentAction) -> DraftDecisionRead:
    """The audit row as the API answers it — the outcome, never the content.

    The tool's input and output and the stored form deliberately do **not** travel here:
    the client already has the form (it drew it), §10.1 keeps tool material out of
    responses that are not the audit read, and a confirmation response that repeated the
    document would be a second copy of a record the entity itself is the truth about. What
    a caller needs back is what happened.
    """
    return DraftDecisionRead(
        id=action.id,
        tool_name=action.tool_name,
        status=str(action.status),
        confirmed_at=None if action.confirmed_at is None else action.confirmed_at.isoformat(),
        entity_type=action.resulting_entity_type,
        entity_id=action.resulting_entity_id,
    )


__all__ = ["router"]
