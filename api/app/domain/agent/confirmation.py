"""DESIGN §6.3's second requirement: the explicit click, and what it is allowed to do.

    「"确认提交"必须是**显式按钮点击**，且需要重新校验权限与会话；
      聊天里回一句"好的"不算确认。」

This module is that requirement, and the four things it does *not* do are as
load-bearing as the one thing it does.

**It is not reachable from the chat.** The graph's `await_confirmation_node` pauses on
`interrupt()` and records the *type* of whatever the human's words turn out to be — never
its value, never a consequence (`ai/agents/nodes.py`). Nothing in `app/ai/**` calls this
module: the only caller is `POST /agent/actions/{id}/confirm`, and the only thing that
produces that request is a person pressing a button. So "a chat reply of 好的 creates
nothing" is a property of the call graph rather than of a prompt or a classifier, and the
test that proves it asserts the schema's row counts.

**It writes nothing under the confirmation path's own name.** Every entity is created and
filed by the module that owns it — `LeaveService.draft` then `LeaveService.submit`,
`CorrectionService.draft` then `CorrectionService.submit`, `TimesheetService.add_entry`
then `TimesheetService.submit` — so a confirmed draft and a hand-filed request are the
*same writes* down the same two-level approval route. There is no second filing path here
to keep in step with the first, and §6.3's 「走既有的两级审批流，不跳过任何一级」 is
satisfied by construction rather than by inspection. The one thing this module passes that
the routes do not is `SubmitContext(initiated_by="agent", confirmed_by_user_id=…)`, which
is exactly the pair of columns §3.4 keeps for it.

**It re-validates through the rules, not through a copy of them.** Every field is
re-validated by the `check_*` method ticket 40 extracted for this purpose —
`LeaveService.check_request`, `CorrectionService.check_draft`,
`TimesheetService.check_entry` — which the write paths themselves call a line later. A
second copy of "is the balance enough" here is the copy that goes stale, and ticket 40's
mutation evidence is that the shared implementation is what stops a draft accepting what
submission refuses.

**What "the permissions changed" is measured against.** Two facts, both re-derived at
confirmation time and neither remembered from when the draft was produced:

* **the permission decision.** The request's principal is resolved from the database on
  *this* request (`api/v1/deps.py::current_principal` → `access.snapshot.resolve_principal`),
  so every input the kernel reads — roles, clearance level, the department subtree, the
  reporting relationship — is today's, and the snapshot cache is keyed by the account's
  epoch and the organisation's structure version so a changed input misses it. This module
  then asks the kernel for the action filing the entity would need
  (`leave.request_own`, `attendance.correction_own`, `timesheet.write_own` and
  `timesheet.submit_own`) against the caller's **own** employee resource. A clearance
  withdrawn, a department moved, a role revoked, an assignment terminated — all of them are
  read here and refused here.
* **the document's own rules.** The `check_*` calls above re-read the leave balance, the
  week's lock and window, the punch the correction resolves to and the project's
  bookability. That is the *cheap* half in the sense the ticket means: it catches the
  balance somebody else spent while the form sat open. The permission question above is the
  interesting half, and the two are asked together — a caller whose reach shrank is refused
  even when the numbers still add up.

**A refusal does not consume the draft.** Nothing here writes `status` unless the document
was actually created or the employee actually rejected it. A confirmation refused because
the rules moved leaves the row `proposed`, so the employee can ask for a fresh draft
without losing the evidence of what was proposed; recording it as `rejected` would answer
"the employee said no" to a question they were never asked.

**The lock, and the two writes that cannot interleave.** `load_for_update` holds the row
for the whole transaction, and `decide` enforces `status = 'proposed' AND expires_at >
now()` in its own `WHERE`. A double-submitted click therefore either blocks until the first
commits — and then finds a draft that is no longer `proposed` — or loses the guarded
`UPDATE`; either way nothing is created, because the guarded write shares the transaction
that created the entity.

**Who is told.** §6.3's third point is that the submission is the employee's, and the flow
that follows is the ordinary one: the entity module hands the document to the same
`ApprovalNotifier`-wrapped engine a hand-filed request reaches, so the manager's and HR's
notifications are the ones they always get. The approver's *screen* is ticket 53's; what
ticket 41 exposes for it is the truth in the API — `approval_requests.initiated_by` and
`confirmed_by_user_id`, which every request detail already carries.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.domain.access.kernel import (
    Action,
    Resource,
    ResourceKind,
    apply_rls_context,
    can,
)
from app.domain.access.principal import Principal
from app.domain.agent.models import (
    AgentAction,
    DraftEntity,
    DraftStatus,
    FieldKind,
    PrefillForm,
)
from app.domain.agent.repository import PostgresAgentActionRepository
from app.domain.agent.service import AgentActionService
from app.domain.agent.service import service_for as agent_actions_for
from app.domain.approval.models import SubmitContext
from app.domain.approval.service import ApprovalService
from app.domain.attendance.anomaly_service import AnomalyService
from app.domain.attendance.business_day import MADRID
from app.domain.attendance.correction_service import CorrectionService
from app.domain.attendance.models import EventType
from app.domain.attendance.service import AttendanceService
from app.domain.errors import DomainError
from app.domain.leave.service import LeaveCalendar, LeaveService
from app.domain.notification.approval import ApprovalNotifier
from app.domain.notification.service import NotificationService
from app.domain.overtime.service import OvertimeLedger
from app.domain.project.service import ProjectService
from app.domain.schedule.service import ScheduleService
from app.domain.timesheet.models import monday_of
from app.domain.timesheet.service import TimesheetService
from app.logging import get_logger
from app.repositories.approval import PostgresApprovalRepository
from app.repositories.attendance import (
    PostgresAnomalyRepository,
    PostgresAttendanceRepository,
    PostgresCorrectionRepository,
)
from app.repositories.leave import PostgresLeaveRepository
from app.repositories.notification import PostgresNotificationRepository
from app.repositories.overtime import PostgresOvertimeRepository
from app.repositories.project import PostgresProjectRepository
from app.repositories.schedule import PostgresScheduleRepository
from app.repositories.timesheet import PostgresTimesheetRepository

logger = get_logger(__name__)

#: The actions each entity's *confirmation* requires, on top of the endpoint's own guard.
#:
#: The same action the corresponding draft tool asked for when it proposed the form
#: (`ai/tools/draft.py::_may`), which is the point: the permission to confirm is the
#: permission to file, asked again. `timesheet_entry` names two because confirming a
#: drafted entry is a write *and* a filing — the entry is added and the week is handed to
#: the engine — and both are the same self-only act seen from two sides.
CONFIRM_ACTIONS: Mapping[DraftEntity, tuple[Action, ...]] = {
    DraftEntity.LEAVE_REQUEST: (Action.LEAVE_REQUEST_OWN,),
    DraftEntity.ATTENDANCE_CORRECTION: (Action.ATTENDANCE_CORRECTION_OWN,),
    DraftEntity.TIMESHEET_ENTRY: (Action.TIMESHEET_WRITE_OWN, Action.TIMESHEET_SUBMIT_OWN),
}

#: What each entity's approval request names (§3.4's `entity_type`), read back from the
#: result so a reader can follow the audit row into the engine without decoding an enum.
ENTITY_TYPE_OF: Mapping[DraftEntity, str] = {
    DraftEntity.LEAVE_REQUEST: "leave_request",
    DraftEntity.ATTENDANCE_CORRECTION: "attendance_correction",
    DraftEntity.TIMESHEET_ENTRY: "timesheet",
}

#: The `FieldKind`s whose value is a whole number, so the form's strings become numbers
#: again before the domain sees them. A closed set, like `FieldKind` itself: "which kinds
#: are numeric" is a question about the form vocabulary and not about one field.
_NUMERIC: frozenset[FieldKind] = frozenset({FieldKind.NUMBER})

#: The `FieldKind`s whose value is an ISO date.
_DATES: frozenset[FieldKind] = frozenset({FieldKind.DATE})

#: The form fields whose value is a **uuid**, keyed by the submission's own field name.
#:
#: A `SELECT`'s value is a string on the wire — that is what an HTML select posts — and three
#: of the fields a draft writes name rows: a timesheet entry's project and task. The
#: submission's request models declare them `UUID` (`schemas/timesheet.py::EntryWrite`), and
#: the domain compares them by identity, so a string that reaches `resolve_record_target`
#: fails its `task.project_id != project_id` comparison and is reported as "no such task in
#: this project" — a refusal that looks like a data problem and is a type problem. This table
#: is named per *field* rather than inferred from `FieldKind`, because `select` also carries
#: a leave type's code, which is a string the domain looks up.
_UUIDS: frozenset[str] = frozenset({"project_id", "task_id"})


def _as_instant(value: Any) -> datetime:
    """The form's `corrected_at` as the instant the employee meant, or a refusal naming it."""
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError) as error:
            raise FieldUnusable("corrected_at", f"{value!r} is not a time") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        # `date`'s midnight and a bare `HH:MM` both land here, which is right: a
        # correction is an instant on a business day in Madrid, and an instant without an
        # offset is not one.
        parsed = datetime.combine(parsed.date(), parsed.time(), tzinfo=MADRID)
    return parsed


def _corrected_at(fields: Mapping[str, Any]) -> datetime:
    """The instant a correction is about, assembled from the two fields that describe it.

    The form shows the *time of day* beside the business date (`FieldKind.TIME`, ticket
    40's decision) because that is the pair a person reads off a punch, so the platform
    is what turns "the 3rd at 16:10" into an instant — and it does it in **Madrid's**
    zone, which keeps a browser's timezone out of an instant the server has to attribute
    to a Spanish business day. A form that carries a full instant (the fixture's, or a
    client that sent one) is honoured as it stands.
    """
    business_date = fields.get("business_date")
    if not isinstance(business_date, date):
        raise FieldUnusable("business_date", f"{business_date!r} is not a date")
    raw = fields.get("corrected_at")
    if isinstance(raw, datetime) or (isinstance(raw, str) and "T" in raw):
        return _as_instant(raw)
    if isinstance(raw, time):
        clock = raw
    else:
        try:
            clock = time.fromisoformat(str(raw))
        except (TypeError, ValueError) as error:
            raise FieldUnusable("corrected_at", f"{raw!r} is not a time of day") from error
    return datetime.combine(business_date, clock, tzinfo=MADRID)


class FieldUnusable(Exception):
    """A confirmed value this platform cannot turn into the field it names.

    One exception for "the client sent something the form does not have" and for "the
    value is not of the type the form says it is", because the two have one answer: the
    confirmation is refused and the form is redrawn. The field is named so a client can
    point at the control.
    """

    def __init__(self, field: str, detail: str) -> None:
        super().__init__(detail)
        self.field = field
        self.detail = detail


class DraftNotConfirmable(Exception):
    """The draft exists and is the caller's, but is no longer the caller's to answer.

    Carries the *status* the row is in, because the causes need different sentences: a
    lapsed draft is regenerated (「需重新生成」), an already-answered one is a screen
    somebody has open twice, and a draft the employee already rejected is simply gone.
    """

    def __init__(self, status: DraftStatus) -> None:
        super().__init__(f"the draft is {status}")
        self.status = status


class DraftMissing(Exception):
    """No such draft *for this caller*.

    One exception for "there is no such row" and for "the row is somebody else's", which
    is the same decision the conversation read makes: telling the two apart would make
    this endpoint an existence oracle over other people's proposals.
    """


class ConfirmationRefused(Exception):
    """The caller's *document* was refused at confirmation — the world moved.

    Distinct from `DraftNotConfirmable`: the draft is still `proposed` and still the
    caller's, and it stays that way. What failed is the entity's own rules, so the refusal
    carries the catalogue key that explains it and the sentence tells the employee to have
    the draft regenerated rather than to keep editing this one.
    """

    def __init__(self, message_key: str, detail: str) -> None:
        super().__init__(detail)
        self.message_key = message_key
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ConfirmedDraft:
    """The audit row after the decision, and the document it now points at.

    `entity_id` is `None` for a rejection and is never `None` for a confirmation — the
    same "both or neither" the `agent_actions` constraint expresses in SQL, restated here
    so a caller cannot read a confirmation with nothing to show for it.
    """

    action: AgentAction
    status: DraftStatus
    entity_type: str | None
    entity_id: UUID | None


@dataclass(frozen=True, slots=True)
class ConfirmationService:
    """One draft, one person, one click. `confirm` and `reject` are the whole interface.

    The collaborators are constructed per call from the session and the principal, because
    two of them cannot exist without one: `TimesheetService` is built *with* its principal
    and scopes every read to it, and a service constructed for somebody else is exactly the
    thing that must be impossible to express.
    """

    session: AsyncSession
    actions: AgentActionService

    @property
    def _repository(self) -> PostgresAgentActionRepository:
        """The repository the service above already holds.

        Reached through `AgentActionService.repository` rather than constructed again, so
        these statements run in **that** service's transaction: the row lock
        `load_for_update` takes has to cover the entity write that follows it, and a second
        repository on a second session would hold a lock nothing else could see.
        """
        return self.actions.repository

    # --- the click ----------------------------------------------------------

    async def confirm(
        self,
        *,
        action_id: UUID,
        principal: Principal,
        fields: Mapping[str, Any] | None = None,
    ) -> ConfirmedDraft:
        """Create the document the form describes, as the person who pressed the button.

        The order is the argument of this method, and each step is here for one reason:

        1. **load and lock the row, and check it is the caller's** — `DraftMissing` for
           anything else, which is also the answer for a row that does not exist.
        2. **check the caller may still file this kind of document** — the kernel, asked
           now, with today's snapshot. A caller who cannot is refused before anything else
           is even looked at.
        3. **check the draft is still answerable** — `proposed`, and inside the window the
           *database* computes. A lapsed draft is recorded as `expired` first, so the row
           the employee looks at next says the same thing the answer did.
        4. **turn the form into the submission's values, and re-validate them with the
           submission's own rules** — the three `check_*` methods, called by the write path
           a line later. This is the second half of "the permissions and the rules changed".
        5. **create and file the document** — the entity module's own write path, with
           `SubmitContext(initiated_by="agent", confirmed_by_user_id=…)`.
        6. **close the audit row** — guarded in SQL, so the second of two racing clicks
           records nothing and creates nothing.

        **Steps 2 and 3 are in that order, and the order is a decision.** A caller whose
        permission was withdrawn is told *that*, even if the draft has also lapsed since —
        "you may no longer file this" is the fact that sends them somewhere useful, and
        "it expired, ask again" would be an answer they can act on by asking again and
        being refused identically. It is also the order §6.3 reads in.

        A refusal at steps 2–4 leaves the row `proposed` and untouched.
        """
        action, expired = await self._require_own(action_id=action_id, principal=principal)
        form = action.form
        if form is None:
            # §6.3's first requirement is the form, and a draft without one describes no
            # document. "Not confirmable" rather than a 500: the row is real, and the
            # caller is being told the truth about it.
            raise DraftNotConfirmable(action.status)

        await self._require_permitted(principal=principal, entity=form.entity)
        await self._require_answerable(action_id=action_id, action=action, expired=expired)

        values = confirmed_values(form, fields)
        try:
            entity_id, entity_type = await self._create(
                form=form, values=values, principal=principal
            )
        except DomainError as refusal:
            raise ConfirmationRefused(
                message_key_of(refusal), refusal.detail or str(refusal)
            ) from refusal

        # The context again, for the reason `_republish` gives: `_create` committed, and
        # the next statement is a *write* on a row-level-secured table. Without this the
        # guard would match no rows, `decide` would answer "it moved", and a confirmation
        # that had created its document would be reported as expired — which is precisely
        # the failure this call was added to fix.
        await self._republish(principal)

        decided = await self._repository.decide(
            action_id,
            status=DraftStatus.CONFIRMED,
            resulting_entity_type=entity_type,
            resulting_entity_id=entity_id,
        )
        if decided is None:  # pragma: no cover - the row lock makes this unreachable
            # **Not `DraftNotConfirmable(EXPIRED)`, and the wording matters.** The guard can
            # only lose here for one reason under the lock — the draft was answered while this
            # request was in flight — and reporting that as "it expired" would tell the
            # employee to ask for a new one when the truthful answer is that the one they
            # submitted is already on its way to approval.
            raise ConfirmationRefused(
                "errors.agent_draft_not_confirmable",
                f"draft {action_id} was answered while this confirmation was in flight",
            )
        await self._repository.commit()

        logger.info(
            "agent_draft_confirmed",
            tool_name=action.tool_name,
            entity_type=entity_type,
            entity_id=str(entity_id),
        )
        return ConfirmedDraft(
            action=decided,
            status=DraftStatus.CONFIRMED,
            entity_type=entity_type,
            entity_id=entity_id,
        )

    async def reject(
        self, *, action_id: UUID, principal: Principal, reason: str | None = None
    ) -> ConfirmedDraft:
        """Record that the employee said no. **No document is created, and none is touched.**

        The row's status becomes `rejected` and `confirmed_at` records when the answer was
        given — the column's own constraint requires an instant for every status but
        `proposed`, and "when did the human answer" is worth keeping whichever way they
        answered. `resulting_entity_type` and `resulting_entity_id` stay NULL, which is what
        makes 「不产生任何单据」 a fact about the row rather than a promise in a docstring.

        `reason` is the employee's own words about their own draft, and it is deliberately
        **not stored**: `agent_actions` has no free-text column and §10.1 is about not
        inventing places to put a person's prose. It reaches the caller and the log, which
        is where an operator asks "why did they refuse it" without turning the audit table
        into a message board.

        A rejection is not a document decision, so it consults none of the entity's rules:
        discarding a form cannot collide with a leave balance.
        """
        action, expired = await self._require_own(action_id=action_id, principal=principal)
        await self._require_answerable(action_id=action_id, action=action, expired=expired)

        decision = await self._repository.decide(action_id, status=DraftStatus.REJECTED)
        if decision is None:  # pragma: no cover - the row lock makes this unreachable
            raise DraftNotConfirmable(DraftStatus.EXPIRED)
        await self._repository.commit()

        logger.info(
            "agent_draft_rejected",
            tool_name=action.tool_name,
            had_reason=bool(reason and reason.strip()),
        )
        return ConfirmedDraft(
            action=decision,
            status=DraftStatus.REJECTED,
            entity_type=None,
            entity_id=None,
        )

    # --- the checks ---------------------------------------------------------

    async def _require_own(
        self, *, action_id: UUID, principal: Principal
    ) -> tuple[AgentAction, bool]:
        """The row, locked, if it is this caller's — and the database's verdict on its age.

        Ownership is compared against `user_id` — the same key the table's row-level policy
        uses (`user_id = app_setting('app.current_user_id')::uuid`) — so the application's
        answer and the database's cannot disagree. The comparison is made here rather than
        in the repository's `WHERE` so that both failures produce one exception that a route
        turns into one 404, which is the property that stops this endpoint reporting which
        draft ids exist.

        The second half of the pair is `expired`: it came back *with the locked read*, so it
        describes the row the lock is holding rather than a row read again a moment later.
        """
        loaded = await self._repository.load_for_update(action_id)
        if loaded is None or loaded.action.user_id != principal.user_id:
            raise DraftMissing(str(action_id))
        return loaded.action, loaded.expired

    async def _require_permitted(self, *, principal: Principal, entity: DraftEntity) -> None:
        """The kernel's answer, asked now, about the caller's **own** resource.

        Every action here is self-only (`SELF_ONLY_ACTIONS`), so the resource is the
        caller's own employee id and a principal cannot express "somebody else's" — the
        same shape `ai/tools/draft.py::_may` uses when it proposes the form. What is new at
        confirmation is the *snapshot*: it was resolved on this request, so a clearance
        withdrawn, a department moved, a role revoked or an assignment terminated since the
        draft was produced is read here and refused here.
        """
        resource = Resource(ResourceKind.EMPLOYEE, owner_employee_id=principal.employee_id)
        for action in CONFIRM_ACTIONS[entity]:
            decision = can(principal, action, resource)
            if not decision.allowed:
                logger.info(
                    "agent_confirm_refused",
                    action=str(action),
                    reason=str(decision.primary_reason),
                )
                raise ConfirmationRefused(
                    "errors.forbidden",
                    f"{action} refused at confirmation: {decision.primary_reason} "
                    f"({decision.detail})",
                )

    async def _require_answerable(
        self, *, action_id: UUID, action: AgentAction, expired: bool
    ) -> None:
        """`proposed`, and inside the window the database computes.

        **One clock, one answer.** `expired` arrives from the locked read and there is
        deliberately no second comparison beside it: the row is held by `FOR UPDATE`, so
        nothing can change its window while this transaction runs, and a recheck would either
        agree (which makes it dead code) or disagree (which means the two are not asking the
        same question). The first version of this method *did* recheck — `expired or not await
        repository.can_still_confirm(...)` — and mutation testing showed what that cost: an
        always-`false` flag was invisible, because the recheck caught the same draft on every
        path. A check nothing can distinguish from its own duplicate is not a second line of
        defence, and this repository's own culture says so about copies of rules.

        The comparison is PostgreSQL's (`expires_at <= now()`), evaluated in the same statement
        that took the lock, so a container whose clock is a second behind cannot confirm a
        draft the row says has lapsed. A lapsed draft is **recorded** as expired before the
        refusal, so the row the employee looks at next says the same thing the answer did —
        §6.3 asks for a status, not for a filter.
        """
        if action.status is not DraftStatus.PROPOSED:
            raise DraftNotConfirmable(action.status)
        if expired:
            await self._repository.mark_expired(action_id)
            await self._repository.commit()
            raise DraftNotConfirmable(DraftStatus.EXPIRED)

    # --- the write ----------------------------------------------------------

    async def _create(
        self, *, form: PrefillForm, values: Mapping[str, Any], principal: Principal
    ) -> tuple[UUID, str]:
        """Create the document and file it, through the entity module's own write path.

        Each branch is a handful of lines because everything interesting is one level down:
        `draft`/`add_entry` re-checks and writes, `submit` reserves and hands the document
        to the engine. What is here and not there is the `SubmitContext` and the identity —
        `principal.employee_id` — which is the whole of §6.3's third point.
        """
        agent_submission = SubmitContext(
            initiated_by="agent", confirmed_by_user_id=principal.user_id
        )
        if form.entity is DraftEntity.LEAVE_REQUEST:
            leave = self._leave()
            request = await leave.draft(
                employee_id=principal.employee_id,
                code=values["leave_type"],
                start_date=values["start_date"],
                end_date=values["end_date"],
                attachment_reference=values.get("attachment_reference"),
                actor_user_id=principal.user_id,
                actor_roles=principal.roles,
            )
            await self._republish(principal)
            # `submit` re-reads the request it just wrote and files it — the same call the
            # route makes, with the one argument the route does not pass.
            filed = await leave.submit(request.request.id, agent_submission)
            return filed.request.id, ENTITY_TYPE_OF[form.entity]

        if form.entity is DraftEntity.ATTENDANCE_CORRECTION:
            corrections = self._corrections()
            correction = await corrections.draft(
                employee_id=principal.employee_id,
                business_date=values["business_date"],
                kind=values["kind"],
                corrected_at=values["corrected_at"],
                reason=values["reason"],
                requested_by_employee_id=principal.employee_id,
            )
            await self._republish(principal)
            filed = await corrections.submit(correction.correction.id, agent_submission)
            return filed.correction.id, ENTITY_TYPE_OF[form.entity]

        # The third form: one entry into the caller's own week, then the week is filed —
        # the same two calls `POST /timesheets/entries` and `POST /timesheets/submit` make,
        # in that order, which is why confirming a drafted entry lands under the week's
        # request and not under one of its own.
        week_start = monday_of(values["entry_date"])
        timesheets = self._timesheets(principal)
        await timesheets.add_entry(
            week_start,
            entry_date=values["entry_date"],
            project_id=values["project_id"],
            task_id=values["task_id"],
            minutes=values["minutes"],
            note=values.get("note"),
        )
        await self._republish(principal)
        view = await timesheets.submit(week_start, agent_submission)
        if view.approval_request_id is None:  # pragma: no cover - `submit` writes it
            raise ConfirmationRefused(
                "errors.agent_draft_confirmation_refused",
                f"week {week_start} was written but carries no approval request",
            )
        return view.approval_request_id, ENTITY_TYPE_OF[form.entity]

    async def _republish(self, principal: Principal) -> None:
        """Re-publish the permission context after a write path committed.

        Every entity write path commits, and the request's RLS context is
        `set_config(..., is_local => true)` — transaction-scoped. Without this line the
        very next statement runs with no context published, and the database's policies
        answer "no rows" rather than "not yours": the failure ticket 34 recorded and ticket
        40's fixture met the honest way.

        **This is called before every statement that has to match a row**, and the count is
        not an accident: `_create` commits twice (the document, then the filing), so the
        audit row's guarded `UPDATE` needs it as much as the second entity call does. The
        first version of this module published only between the two entity calls, and the
        guard — which runs in a transaction the last commit opened — matched nothing, so a
        confirmation that had created its document reported itself as expired. The test
        that caught it is
        `test_the_submitted_document_is_the_employee_s_own_and_walks_both_levels`.

        Re-publishing is the fix the brief names; deleting a commit to avoid it would trade
        a visible line for an invisible coupling.
        """
        await apply_rls_context(self.session, principal)

    # --- the collaborators --------------------------------------------------

    def _approvals(self) -> ApprovalNotifier:
        """The engine, wrapped so the notifications cannot be forgotten.

        One builder for the three modules below, for the reason `ai/tools/services.py`
        records: a bare `ApprovalService` at any of them would still record decisions and
        silently lose the notices that follow.
        """
        repository = PostgresApprovalRepository(self.session)
        return ApprovalNotifier(
            engine=ApprovalService(repository, self.session),
            notifications=NotificationService(
                PostgresNotificationRepository(self.session), self.session
            ),
            approvals=repository,
        )

    def _leave(self) -> LeaveService:
        return LeaveService(
            PostgresLeaveRepository(self.session),
            self.session,
            expectations=ScheduleService(PostgresScheduleRepository(self.session), self.session),
            approvals=self._approvals(),
            annual_leave_days=get_settings().annual_leave_days,
        )

    def _corrections(self) -> CorrectionService:
        """The correction flow, wired as `app/api/v1/attendance.py` wires it.

        The punch lineage matters here and not only for the check: `correction_service`
        appends the event and closes the anomaly through these same two collaborators, so
        one wired differently would correct a day the anomaly scan does not recognise.
        """
        punches = PostgresAttendanceRepository(self.session)
        expectations = ScheduleService(PostgresScheduleRepository(self.session), self.session)
        attendance = AttendanceService(
            punches,
            expectations=expectations,
            overtime=OvertimeLedger(PostgresOvertimeRepository(self.session)),
        )
        return CorrectionService(
            PostgresCorrectionRepository(self.session),
            self.session,
            punches=punches,
            attendance=attendance,
            anomalies=AnomalyService(
                PostgresAnomalyRepository(self.session),
                expectations=expectations,
                leave=LeaveCalendar(PostgresLeaveRepository(self.session)),
            ),
            approvals=self._approvals(),
        )

    def _timesheets(self, principal: Principal) -> TimesheetService:
        """The week module, built **with the caller**: its every read is scoped to them.

        `principal` is not decoration — this service cannot be constructed without one, and
        that is the property that makes "somebody else's week" unrepresentable here rather
        than merely refused.
        """
        projects = PostgresProjectRepository(self.session)
        return TimesheetService(
            PostgresTimesheetRepository(self.session),
            self.session,
            principal=principal,
            projects=ProjectService(projects, self.session),
            project_repository=projects,
            expectations=ScheduleService(PostgresScheduleRepository(self.session), self.session),
            approvals=self._approvals(),
        )


def service_for(session: AsyncSession) -> ConfirmationService:
    """The confirmation service on one session.

    A factory rather than eight constructor arguments at the route, for the reason
    `ai/tools/services.py` gives about its own builders: the arrangement is one thing, and
    a second site assembling it differently would be a second answer to "what does
    confirming a draft require". `ttl_hours` is the configured `AGENT_DRAFT_TTL_HOURS`,
    handed to the action service exactly as the draft node hands it.
    """
    return ConfirmationService(
        session=session,
        actions=agent_actions_for(session, ttl_hours=get_settings().agent_draft_ttl_hours),
    )


def message_key_of(refusal: DomainError) -> str:
    """The catalogue key for a domain refusal, so a client can render the sentence itself.

    The same translation `ai/tools/draft.py::_invalid` performs for a refused form, and for
    the same reason: the domain error already has wording in both languages, and the client
    renders the reader's own rather than receiving a sentence the server chose.
    """
    from app.core.errors import ErrorCode, definition_of

    try:
        code = ErrorCode(refusal.code.value)
    except ValueError:  # pragma: no cover - every domain code is in the catalogue
        return "errors.agent_draft_confirmation_refused"
    return definition_of(code).message_key


def confirmed_values(
    form: PrefillForm, overrides: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """The form's values, with the employee's edits applied, as the submission's types.

    §6.3's first requirement is that every field is editable, so the values that reach
    `_create` are the *confirmed* ones and not necessarily the proposed ones — and the
    model that produced them is a JSON object of strings, because that is what a browser
    posts. This function is the only place those strings become the types the three request
    models declare.

    Three rules, and each is one of the ticket's own:

    * **No field the form does not have.** The form is the contract (`PrefillForm`), and an
      unknown name is a `FieldUnusable` rather than a field silently dropped: a client that
      misspells `minutes` should be told, not thanked.
    * **A required field is present and non-empty.** "Required" is the *request model's*
      answer, carried on the field since ticket 40, so this is not a second opinion about
      which fields matter.
    * **A value is converted by the field's own `kind`.** Dates become `date`, numbers
      become `int`, the correction's instant is assembled from its day and its time (see
      `_corrected_at`), and a select's value stays the string the domain looks up.

    `overrides` is what the browser sends: the whole form, as edited. Absent keys keep the
    proposed value, which is what makes a client that posts only what it changed correct
    rather than incomplete.
    """
    sent = dict(overrides or {})
    unknown = sorted(set(sent) - set(form.field_names))
    if unknown:
        raise FieldUnusable(unknown[0], f"{unknown[0]!r} is not a field of this draft")

    values: dict[str, Any] = {}
    for field in form.fields:
        raw = sent.get(field.name, field.value)
        empty = raw is None or (isinstance(raw, str) and not raw.strip())
        if empty:
            if field.required:
                raise FieldUnusable(field.name, f"{field.name} is required")
            values[field.name] = None
            continue
        if field.kind in _DATES:
            values[field.name] = _as_date(field.name, raw)
        elif field.kind in _NUMERIC:
            values[field.name] = _as_integer(field.name, raw)
        elif field.name in _UUIDS:
            values[field.name] = _as_uuid(field.name, raw)
        elif field.name == "corrected_at":
            # Left as the raw value here: `_corrected_at` reads it together with the
            # business date, because the time of day is only an instant on that day.
            values[field.name] = raw
        else:
            values[field.name] = str(raw).strip()

    if form.entity is DraftEntity.ATTENDANCE_CORRECTION:
        values["corrected_at"] = _corrected_at(values)
        values["kind"] = EventType(values["kind"])
    return values


def _as_date(field: str, raw: Any) -> date:
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw))
    except ValueError as error:
        raise FieldUnusable(field, f"{raw!r} is not a date") from error


def _as_integer(field: str, raw: Any) -> int:
    if isinstance(raw, bool):
        raise FieldUnusable(field, f"{raw!r} is not a number")
    try:
        return int(str(raw))
    except ValueError as error:
        raise FieldUnusable(field, f"{raw!r} is not a number") from error


def _as_uuid(field: str, raw: Any) -> UUID:
    """A select's chosen row, as the id the domain compares. See `_UUIDS`."""
    if isinstance(raw, UUID):
        return raw
    try:
        return UUID(str(raw))
    except (TypeError, ValueError) as error:
        raise FieldUnusable(field, f"{raw!r} is not an id") from error


__all__ = [
    "CONFIRM_ACTIONS",
    "ENTITY_TYPE_OF",
    "ConfirmationRefused",
    "ConfirmationService",
    "ConfirmedDraft",
    "DraftMissing",
    "DraftNotConfirmable",
    "FieldUnusable",
    "confirmed_values",
    "message_key_of",
    "service_for",
]
