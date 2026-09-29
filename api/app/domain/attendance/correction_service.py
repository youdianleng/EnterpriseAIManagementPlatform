"""The correction flow: a document, two levels of approval, and one append.

`docs/architecture/codebase-design.md` §2.3 fixes the shape this module is built
on: there is **one** state machine, and it is the approval engine's. Nothing here
decides whether a correction was approved — `submit` hands the document over and
`decide` records what the engine said — and nothing here notifies anybody, because
the engine is wrapped in `ApprovalNotifier` and the notices follow the decision the
way they do for every other document in the system.

Four operations and the applier, and each one is a rule:

* **`draft` refuses what could never be applied.** The day-and-kind pair is resolved
  while the request is still the requester's to fix: no punch of that kind yet means
  the approval will *make one up* (a forgotten clock_out is the ordinary case), two
  of them means the document cannot say which one it is about and is refused rather
  than guessed at, and a future instant is refused with the vocabulary the clock
  already uses for it. The same argument the personnel module makes: a document that
  fails on its effective date has been wrong for three weeks by then.
* **`update` is for drafts.** A document that was returned for correction is back in
  the requester's hands, and re-filing it unchanged would be a fiction. A filed one
  is not editable — the fix for a wrong *filed* request is a rejection (or a new
  document), not a silent rewrite of what two people were asked to approve.
* **`decide` applies, in the same operation.** A correction has no effective date:
  "approved" *is* "in force" (there is nothing to wait for — the day it is about has
  happened), so the append follows the decision immediately, through the applier so
  that there is exactly one implementation of it. A document whose append fails
  reads `approved` rather than `applied`, and the next run retries it.
* **`apply_approved` is the only thing that writes to the stream.** Idempotent by
  construction: it selects documents that are filed and not applied, oldest first,
  `FOR UPDATE SKIP LOCKED`, asks the engine whether each was approved, appends, and
  marks. Running it twice appends once, and a crash between the append and the
  anomaly sweep leaves a day whose anomalies the next scan of that date — or the
  correction of another day — still cleans up.

**Everything is audited, and the middle of the three moments is the engine's.**
Requesting writes `attendance_correction.requested`, filing and deciding write the
engine's own `approval.submitted` and `approval.decided` keyed on this document's
id, and applying writes `attendance_correction.applied`. A second record of a
decision under this module's name would be a copy of the engine's answer, and the
copy is the one that goes stale.
"""

from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditAction, record
from app.core.errors import ErrorCode
from app.domain.approval.models import ApprovalStatus, DecisionKind, SubmitContext
from app.domain.attendance.anomaly_service import AnomalyService
from app.domain.attendance.business_day import madrid_today
from app.domain.attendance.correction_repository import CorrectionRepository
from app.domain.attendance.corrections import (
    ENTITY_TYPE,
    ApplyFailure,
    ApplyReport,
    Correction,
    CorrectionDraftCheck,
    CorrectionInput,
    CorrectionPatch,
    CorrectionQuery,
    CorrectionState,
    CorrectionTarget,
    CorrectionView,
    state_of_correction,
)
from app.domain.attendance.derivation import chain_tip, corrections_of
from app.domain.attendance.errors import AttendanceErrorCode
from app.domain.attendance.models import (
    CLOCK_SKEW,
    PUNCH_EVENT_TYPES,
    AttendanceEvent,
    EventSource,
    EventType,
    NewEvent,
    TimeSource,
    utc_now,
)
from app.domain.attendance.repository import AttendanceRepository
from app.domain.attendance.service import AttendanceService
from app.domain.errors import DomainError
from app.domain.notification.approval import ApprovalNotifier


class CorrectionService:
    """The document, the chain it appends to, and the day it re-derives."""

    def __init__(
        self,
        repository: CorrectionRepository,
        session: AsyncSession,
        *,
        punches: AttendanceRepository,
        attendance: AttendanceService,
        anomalies: AnomalyService,
        approvals: ApprovalNotifier,
        now: TimeSource = utc_now,
    ) -> None:
        self._repository = repository
        self._session = session
        #: The stream's own repository, for the two things this module may not do
        #: itself: append an event, and answer whose day this is.
        self._punches = punches
        #: The day is that service's business, and the append commits with the
        #: snapshot it produces — the way a punch does.
        self._attendance = attendance
        #: Ticket 23's entry point. A correction is the only thing in the system
        #: that closes an anomaly, and the rule for *which* ones lives there.
        self._anomalies = anomalies
        #: The engine, wrapped so the notifications cannot be forgotten.
        self._approvals = approvals
        self._now = now

    # --- the document -------------------------------------------------------

    async def check_draft(
        self,
        *,
        employee_id: UUID,
        business_date: date,
        kind: EventType | str,
        corrected_at: datetime,
        reason: str,
    ) -> CorrectionDraftCheck:
        """Every refusal `draft` makes, and **not one write**.

        Extracted from `draft` in ticket 40 for the agent's draft tool. What a correction
        is about is decided by four things — that the kind is a punch, that the instant
        carries a timezone and has happened, that the reason says why, and that the day and
        kind identify exactly one punch (or none, which is the forgotten clock-out the flow
        makes up) — and all four are asked here. The tool shows a form only if this
        answers, so a draft the employee confirms cannot be refused afterwards for a rule
        that was checked somewhere else.
        """
        event_kind = _punch_kind(kind)
        instant = _timed(corrected_at)
        self._require_reason(reason)
        await self._require_employee(employee_id)
        self._require_past(instant, business_date)
        await self._require_resolvable(employee_id, business_date, event_kind)
        return CorrectionDraftCheck(
            employee_id=employee_id,
            business_date=business_date,
            kind=event_kind,
            corrected_at=instant,
            reason=reason.strip(),
        )

    async def draft(
        self,
        *,
        employee_id: UUID,
        business_date: date,
        kind: EventType | str,
        corrected_at: datetime,
        reason: str,
        requested_by_employee_id: UUID,
    ) -> CorrectionView:
        """Write the request, having refused one that could never be applied.

        **The refusals are `check_draft`'s** (ticket 40); what is left here is the write —
        the row and its trail. The agent's draft tool calls the same method, so the four
        rules a correction is checked against have one implementation rather than one per
        surface.
        """
        check = await self.check_draft(
            employee_id=employee_id,
            business_date=business_date,
            kind=kind,
            corrected_at=corrected_at,
            reason=reason,
        )
        correction = await self._repository.save_correction(
            CorrectionInput(
                employee_id=check.employee_id,
                business_date=check.business_date,
                kind=check.kind,
                corrected_at=check.corrected_at,
                reason=check.reason,
                requested_by_employee_id=requested_by_employee_id,
            )
        )
        await self._audit(
            AuditAction.ATTENDANCE_CORRECTION_REQUESTED,
            correction,
            after={
                "employee_id": check.employee_id,
                "business_date": check.business_date.isoformat(),
                "kind": check.kind.value,
                "corrected_at": check.corrected_at.isoformat(),
                "reason": check.reason,
                "requested_by_employee_id": requested_by_employee_id,
            },
        )
        await self._repository.commit()
        return await self.get(correction.id)

    async def update(self, correction_id: UUID, patch: CorrectionPatch) -> CorrectionView:
        """Change a draft: the instant, the reason, or both."""
        correction = await self._require(correction_id)
        await self._require_state(correction, CorrectionState.DRAFT, "edited")

        instant = (
            _timed(patch.corrected_at)
            if patch.corrected_at is not None
            else correction.corrected_at
        )
        reason = patch.reason.strip() if patch.reason is not None else correction.reason
        self._require_reason(reason)
        self._require_past(instant, correction.business_date)
        await self._require_resolvable(
            correction.employee_id, correction.business_date, correction.kind
        )

        before = {
            "corrected_at": correction.corrected_at.isoformat(),
            "reason": correction.reason,
        }
        updated = await self._repository.write_draft(
            correction_id,
            patch=CorrectionPatch(
                corrected_at=instant,
                reason=reason,
            ),
        )
        await self._audit(
            AuditAction.ATTENDANCE_CORRECTION_UPDATED,
            updated,
            before=before,
            after={"corrected_at": instant.isoformat(), "reason": reason},
        )
        await self._repository.commit()
        return await self.get(correction_id)

    async def submit(self, correction_id: UUID) -> CorrectionView:
        """Hand the draft to the approval engine, as the person who filed it.

        The requester is the document's own `requested_by_employee_id`, not whoever
        pressed the button: the engine resolves the route from the requester, and a
        colleague in HR filing on somebody's behalf is not the person the route was
        meant to be about.
        """
        correction = await self._require(correction_id)
        await self._require_state(correction, CorrectionState.DRAFT, "filed")

        try:
            request_id = await self._approvals.submit(
                ENTITY_TYPE,
                correction.id,
                correction.requested_by_employee_id,
                SubmitContext(),
            )
        except DomainError as error:
            # The engine's refusal in this module's vocabulary: the client routes on
            # the code, and `ERR_APR_002` would tell somebody looking at their own
            # correction nothing about it. The engine's own code travels in the
            # detail.
            raise DomainError(
                AttendanceErrorCode.CORRECTION_SUBMISSION_REFUSED,
                detail=f"the approval engine refused correction {correction_id}: {error}",
            ) from error

        await self._repository.mark_submitted(
            correction_id, request_id=request_id, at=self._now()
        )
        await self._repository.commit()
        return await self.get(correction_id)

    async def decide(
        self,
        correction_id: UUID,
        *,
        approver_employee_id: UUID,
        decision: DecisionKind,
        comment: str | None = None,
        actor_user_id: UUID | None = None,
        actor_roles: frozenset[str] | None = None,
    ) -> CorrectionView:
        """Record one level's decision, and apply the document when it is approved.

        Who may decide is the engine's answer — the requester's manager at the first
        level, any HR member other than the requester at the second — so this method
        tests no role. What it does do is *finish* the document: an approval with no
        append would be a decision that changed nothing, and the whole point of the
        flow is that a punch moves only once two levels have agreed.
        """
        correction = await self._require(correction_id)
        state = await self._approvals.state_of(ENTITY_TYPE, correction.id)
        if state is None:
            raise DomainError(
                AttendanceErrorCode.CORRECTION_NOT_DRAFT,
                detail=f"correction {correction_id} has not been filed",
            )

        await self._approvals.decide(
            state.id,
            approver_employee_id,
            decision,
            comment,
            actor_user_id=actor_user_id,
            actor_roles=actor_roles,
        )
        await self._repository.commit()

        report = await self.apply_approved(correction_id=correction_id)
        failure = report.failure_for(correction_id)
        if failure is not None:
            raise DomainError(
                AttendanceErrorCode.CORRECTION_APPLY_FAILED,
                detail=(
                    f"correction {correction_id} was decided and could not be applied: "
                    f"{failure.detail}"
                ),
            )
        return await self.get(correction_id)

    # --- the applier --------------------------------------------------------

    async def apply_approved(self, *, correction_id: UUID | None = None) -> ApplyReport:
        """Append every approved correction that has not been appended, and say what.

        One document per transaction, so a failure is the document's own: the lock
        goes back with the rollback and the ones behind it are not held up. A
        document that is filed and not yet decided is skipped without being written
        to — the engine is asked, and it is the only thing that can answer — and the
        `examined` set is what stops the loop returning to it.
        """
        applied: list[Correction] = []
        failed: list[ApplyFailure] = []
        examined: set[UUID] = set()

        while True:
            candidate = await self._repository.lock_next_unapplied(
                exclude=frozenset(examined), only=correction_id
            )
            if candidate is None:
                break
            examined.add(candidate.id)

            if (
                await self._repository.approval_status_of(candidate.id)
                is not ApprovalStatus.APPROVED
            ):
                # Not approved (yet): the lock goes back with the commit, and this
                # document is not a candidate for the rest of this run either way.
                await self._repository.commit()
                continue

            try:
                applied.append(await self._apply(candidate))
            except Exception as error:  # noqa: BLE001 - reported, never fatal
                await self._session.rollback()
                failed.append(_failure(candidate.id, error))
                continue

        return ApplyReport(applied=tuple(applied), failed=tuple(failed))

    async def _apply(self, correction: Correction) -> Correction:
        """One document, one transaction: append, mark, rebuild, resolve.

        The order is the design. The append, the document's own "applied" mark and
        the audit record commit **together** with the day's new snapshot — the way a
        punch and its snapshot do — so there is no window in which the record says
        the punch moved and the day still reads the old value. The anomaly sweep
        comes after that commit, because it is derived: it re-reads the day, and a
        retry of this method is a no-op for the document and a second chance for the
        sweep.
        """
        event = await self._append(correction)
        await self._repository.mark_applied(
            correction.id, event_id=event.id, at=self._now()
        )
        await record(
            self._session,
            action=AuditAction.ATTENDANCE_CORRECTION_APPLIED,
            entity_type=ENTITY_TYPE,
            entity_id=correction.id,
            after={
                "employee_id": correction.employee_id,
                "business_date": correction.business_date.isoformat(),
                "kind": correction.kind.value,
                "corrected_at": correction.corrected_at.isoformat(),
                # What the append actually wrote, and whether it restated a punch
                # or made one up: the two are different facts about the record.
                "event_id": event.id,
                "event_type": event.event_type.value,
                "corrected_event_id": event.correction_of_event_id,
            },
            reason=f"correction of {correction.kind} on {correction.business_date}",
        )
        # Commits the append, the mark and the trail above, together.
        await self._attendance.recompute_day(correction.employee_id, correction.business_date)
        await self._anomalies.resolve_for_correction(
            correction.employee_id, correction.business_date, event.id
        )
        return replace(correction, applied_event_id=event.id, applied_at=self._now())

    async def _append(self, correction: Correction) -> AttendanceEvent:
        """The one write to the stream this module makes.

        A punch exists: the correction points at the *tip* of its chain, so a second
        correction of one punch continues the chain rather than branching off the
        original, and the day reads the newest row either way. No punch exists: the
        request was about a punch that was never made, and approval appends the
        punch itself — `source='correction'` says how it got there — rather than a
        correction of nothing.
        """
        target = await self._target(
            correction.employee_id, correction.business_date, correction.kind
        )
        event = await self._punches.append_event(
            NewEvent(
                employee_id=correction.employee_id,
                event_type=EventType.CORRECTION if target.exists else correction.kind,
                occurred_at=correction.corrected_at,
                business_date=correction.business_date,
                source=EventSource.CORRECTION,
                correction_of_event_id=target.tip.id if target.tip is not None else None,
                reason=correction.reason,
                created_by_employee_id=correction.requested_by_employee_id,
            )
        )
        if not target.exists and (
            event.event_type is not correction.kind
            or event.business_date != correction.business_date
        ):
            # The punch index refused the insert and handed back a row it collided
            # with — the same instant, another day. Applying this document to that
            # row would change a day nobody asked about, so it is refused.
            raise DomainError(
                AttendanceErrorCode.CORRECTION_TARGET_UNRESOLVED,
                detail=(
                    f"{correction.employee_id} already has a {correction.kind} at "
                    f"{correction.corrected_at.isoformat()}, on "
                    f"{event.business_date}; this request is about "
                    f"{correction.business_date}"
                ),
            )
        return event

    # --- reads --------------------------------------------------------------

    async def get(self, correction_id: UUID) -> CorrectionView:
        correction = await self._require(correction_id)
        return await self._view(correction, await self._approval_of(correction))

    async def list(self, query: CorrectionQuery) -> tuple[list[CorrectionView], int]:
        """A page of documents, newest first, each with its state.

        The state comes from the query — one join for the whole page — so this does
        not ask the engine once per row. `approval` is deliberately absent here: the
        list answers "what is in flight", and the steps and comments behind one
        document are read from its detail endpoint.
        """
        rows = await self._repository.list_corrections(query)
        total = await self._repository.count_corrections(query)
        return (
            [
                CorrectionView(correction=correction, state=state)
                for correction, state in rows
            ],
            total,
        )

    # --- internals ---------------------------------------------------------

    async def _view(self, correction: Correction, approval) -> CorrectionView:  # noqa: ANN001
        return CorrectionView(
            correction=correction,
            state=state_of_correction(
                correction, approval.status if approval is not None else None
            ),
            approval=approval,
        )

    async def _approval_of(self, correction: Correction):  # noqa: ANN201 - ApprovalState
        return await self._approvals.state_of(ENTITY_TYPE, correction.id)

    async def _require(self, correction_id: UUID) -> Correction:
        correction = await self._repository.get_correction(correction_id)
        if correction is None:
            raise DomainError(
                AttendanceErrorCode.CORRECTION_NOT_FOUND,
                detail=f"unknown correction {correction_id}",
            )
        return correction

    async def _require_state(
        self, correction: Correction, wanted: CorrectionState, act: str
    ) -> None:
        """Refuse an operation the document's state does not admit.

        Read through `state_of_correction` with the engine's answer, so a document
        that was approved, rejected or withdrawn while this module was not looking
        cannot be filed or edited on the strength of its own row.
        """
        approval = await self._approval_of(correction)
        state = state_of_correction(
            correction, approval.status if approval is not None else None
        )
        if state is not wanted:
            raise DomainError(
                AttendanceErrorCode.CORRECTION_NOT_DRAFT,
                detail=f"correction {correction.id} is {state} and cannot be {act}",
            )

    async def _require_employee(self, employee_id: UUID) -> None:
        if await self._punches.employee_status(employee_id) is None:
            raise DomainError(
                AttendanceErrorCode.EMPLOYEE_NOT_FOUND, detail=f"unknown employee {employee_id}"
            )

    async def _require_resolvable(
        self, employee_id: UUID, business_date: date, kind: EventType
    ) -> None:
        """Refuse a document whose day and kind do not identify one punch.

        Asked at draft time and again at apply time, and the second is not
        redundant: a punch can be corrected into existence by another document while
        this one waits for approval, and a document approved against a day that has
        since grown a second clock_out must not pick one of them.
        """
        await self._target(employee_id, business_date, kind)

    def _require_reason(self, reason: str) -> None:
        if not reason or not reason.strip():
            raise DomainError(
                AttendanceErrorCode.CORRECTION_INVALID,
                detail="a correction states why the punch is wrong",
            )

    def _require_past(self, instant: datetime, business_date: date) -> None:
        """A correction restates a punch that happened, and records a day that has.

        The instant is checked the way the clock checks one — a little skew is a
        client whose clock is fast, a punch dated tomorrow is a mistake — and the
        business date is checked because a correction of a day nobody has lived yet
        is not a correction of anything.
        """
        now = self._now()
        if instant > now + CLOCK_SKEW:
            raise DomainError(
                AttendanceErrorCode.EVENT_IN_FUTURE,
                detail=(
                    f"the corrected instant {instant.isoformat()} is later than "
                    f"{now.isoformat()}; working time is a record of what happened"
                ),
            )
        if business_date > madrid_today(now):
            raise DomainError(
                AttendanceErrorCode.CORRECTION_INVALID,
                detail=f"{business_date} has not happened yet",
            )

    async def _target(
        self, employee_id: UUID, business_date: date, kind: EventType
    ) -> CorrectionTarget:
        """The punch a document about this day and kind is about, resolved.

        `punch_lineage` gives every row of the chain that starts at any punch of
        this kind on this day, so the two questions are answered from one read:
        *which* punch (none, one, or — the case that is refused — several) and
        which row a new correction should point at (the tip, by the derivation's own
        rule rather than by a second one written here).
        """
        lineage = await self._repository.punch_lineage(employee_id, business_date, kind)
        punches = [event for event in lineage if event.event_type in PUNCH_EVENT_TYPES]
        if not punches:
            return CorrectionTarget(punch=None, tip=None)
        if len(punches) > 1:
            raise DomainError(
                AttendanceErrorCode.CORRECTION_TARGET_UNRESOLVED,
                detail=(
                    f"{business_date} has {len(punches)} {kind} punches for employee "
                    f"{employee_id}; a request that names a day and a kind cannot say "
                    "which of them it corrects"
                ),
            )
        return CorrectionTarget(
            punch=punches[0], tip=chain_tip(punches[0], corrections_of(lineage))
        )

    async def _audit(
        self,
        action: AuditAction,
        correction: Correction,
        *,
        before: dict | None = None,
        after: dict | None = None,
    ) -> None:
        await record(
            self._session,
            action=action,
            entity_type=ENTITY_TYPE,
            entity_id=correction.id,
            before=before,
            after=after,
        )


def _punch_kind(kind: EventType | str) -> EventType:
    try:
        event_type = EventType(kind)
    except ValueError as exc:
        raise DomainError(
            AttendanceErrorCode.CORRECTION_INVALID, detail=f"unknown punch kind {kind!r}"
        ) from exc
    if event_type not in PUNCH_EVENT_TYPES:
        raise DomainError(
            AttendanceErrorCode.CORRECTION_INVALID,
            detail=(
                f"a correction restates a clock_in or a clock_out, not {event_type}; "
                "correcting a correction is a request about the punch it belongs to"
            ),
        )
    return event_type


def _timed(at: datetime) -> datetime:
    """Refuse a naive instant, in the catalogue's vocabulary, and keep UTC.

    The same conversion `AttendanceService` makes on a punch, for the same reason:
    a client that sent `2026-09-21T22:00:00` receives a catalogued 400 saying what
    was wrong rather than a 500 whose message mentions timezones.
    """
    if at.tzinfo is None or at.utcoffset() is None:
        raise DomainError(
            AttendanceErrorCode.CORRECTION_INVALID,
            detail=f"the instant {at.isoformat()} carries no timezone",
        )
    return at.astimezone(UTC)


def _failure(correction_id: UUID, error: Exception) -> ApplyFailure:
    """What went wrong with one document, in the vocabulary the rest uses."""
    code = error.code.value if isinstance(error, DomainError) else ErrorCode.INTERNAL_ERROR.value
    return ApplyFailure(correction_id=correction_id, code=code, detail=str(error))


#: Re-exported for the modules that read a correction's chain beside it.
__all__ = ["CorrectionService"]
