"""Overtime: the request made in advance, the record it becomes, HR's confirmation.

`docs/architecture/codebase-design.md` §2.3 fixes the shape: there is **one** state
machine and it is the approval engine's. Nothing here decides whether overtime was
approved — the service hands the document over and records what the engine said — and
nothing here notifies anybody, because the engine is wrapped in `ApprovalNotifier` and
the notices follow the decision the way they do for every other document in the system.

Seven decisions, and the rest of the module follows from them:

* **Overtime is applied for before it happens, and there is no other way in.** A draft
  whose date has already passed is refused, there is no endpoint that writes a record
  for a past day, and the only code path that creates an `overtime_records` row is the
  resolution of an approved request. "未事前申请的加班不给补记入口" is therefore a
  property of the module rather than a rule in a router: the retroactive entry does not
  exist to be forgotten about.

* **One day, one record.** A second live request for a day somebody already asked about
  is refused, and a day that already has a record is refused twice over — by the
  service and by `uq_overtime_requests_open_day`. Overtime is counted once per person
  per day, which is what makes the monthly figure a sum of facts rather than of
  intentions.

* **The month is a bucket, not a range.** A record is filed under `YYYY-MM` of its
  Madrid business day, so the monthly summary and the export are group-bys and a night
  shift that ends at 00:30 on the 1st belongs to the month it was worked in.

* **Settlement takes the smaller of the two figures, and never runs before the day is
  over.** `computed_minutes = min(approved_minutes, worked_minutes)`, where the worked
  figure is the attendance module's own answer for the day (`day_view`, snapshot first).
  A record whose day has not ended is left alone rather than settled against a total
  that is still growing.

* **A difference beyond the threshold is marked, not silently resolved.** When the two
  figures are further apart than `threshold_minutes` (a setting, 30 by default), the
  record is flagged for HR with the difference stated. The system chooses the smaller
  one — that is the rule — and it says out loud that the choice is worth a person's
  attention instead of leaving a figure a payroll reader would take for agreement.

* **HR's confirmation is stored beside the computed value, never over it.** The
  confirmed minutes, who wrote them, when, and why are four more columns and a ledger
  row; `computed_minutes` is never written again after settlement. That is what makes
  the two comparable afterwards and what "保留原值与原因" means in practice.

* **The export states minutes and nothing else.** Employee number, name, department,
  date, approved minutes, confirmed minutes — no rate, no multiplier, no amount
  anywhere in this module. Accumulating and exporting is the system's job; what an hour
  costs is finance's calculation (Q12), and a rate column here would be a second payroll
  record that nobody reconciles.

Two acts share the word "settle" and they are different things, so they are named
apart: `resolve_decided` finishes a *document* (an approved request becomes a record,
one per transaction, retried if the process died in between — the engine commits its own
decision, so that gap is real), and `settle_due` finishes a *record* (the day's actual
hours are read, the smaller figure is written, the flag is set). A document is resolved
once; a record is settled once and then confirmed as often as HR needs to correct it.
"""

from datetime import date
from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditAction, record
from app.core.errors import ErrorCode
from app.domain.approval.models import ApprovalStatus, DecisionKind, SubmitContext
from app.domain.attendance.business_day import madrid_today
from app.domain.attendance.models import DayRecord, TimeSource, utc_now
from app.domain.errors import DomainError
from app.domain.notification.approval import ApprovalNotifier
from app.domain.overtime.errors import OvertimeErrorCode
from app.domain.overtime.export import EXPORT_ENTITY, ExportFile, render
from app.domain.overtime.models import (
    ENTITY_TYPE,
    MAX_DAY_MINUTES,
    MAX_DAYS_AHEAD,
    MonthlySummary,
    NewOvertimeRecord,
    OvertimeEntryType,
    OvertimeRecord,
    OvertimeRecordQuery,
    OvertimeRecordView,
    OvertimeRequest,
    OvertimeRequestInput,
    OvertimeRequestPatch,
    OvertimeRequestQuery,
    OvertimeRequestState,
    OvertimeRequestView,
    ResolveFailure,
    ResolveReport,
    SettleFailure,
    SettleReport,
    month_bucket_of,
    period_of,
    state_of_request,
)
from app.domain.overtime.repository import OvertimeRepository

#: The audit entity types. One string per table, plus the period an export is about, so
#: a filter on the trail reads "everything that happened to the overtime ledger" as a
#: prefix.
REQUEST_ENTITY = ENTITY_TYPE
RECORD_ENTITY = "overtime_record"

#: The difference beyond which the two figures are HR's call rather than the system's.
#: Half an hour: below it the gap is the minute somebody spent walking to the lift and
#: the rounding of a punch, and above it somebody worked materially more or less than
#: was agreed. A setting rather than a constant because it is a company's tolerance, and
#: the service carries it so the number a record was flagged against and the number the
#: API reports are the same one.
DEFAULT_CONFIRMATION_THRESHOLD_MINUTES = 30


class AttendanceDays(Protocol):
    """What this module asks the attendance module, and nothing else.

    Two questions, both of them one day at a time: what did this day actually come to
    (`day_view`, the stored snapshot first), and — after this module has written
    something a day carries — rebuild that day's snapshot so the two records agree.
    A Protocol rather than `AttendanceService` so the dependency is stated as the two
    questions, the same shape `ExpectationSource` and `LeaveLookup` establish.
    """

    async def day_view(self, employee_id: UUID, business_date: date) -> DayRecord:
        """One day, as the attendance module has already agreed it was."""
        ...

    async def rebuild_day(self, employee_id: UUID, business_date: date) -> DayRecord:
        """Rebuild one day's snapshot, in the caller's transaction.

        The caller owns the commit, which is what lets a record and the day it changed
        land together: an overtime figure that appeared without the record behind it —
        or the reverse — would be a number nobody can explain.
        """
        ...


class OvertimeService:
    """The requests, the records, the settlement, the confirmation and the export.

    `attendance` is the attendance module seen through `AttendanceDays` — the same
    module whose worked minutes this service compares against, and whose day snapshot
    carries the overtime figure — and `approvals` is the engine *wrapped* in
    `ApprovalNotifier`, so a decision's notifications cannot be forgotten.

    `threshold_minutes` is the configured tolerance, carried here rather than read from
    settings at each use so that the number a record was flagged against and the number
    a reader is told about are the same one. `now` is injectable because the module's
    central rule is about dates — a request may not be for a day that has passed, and a
    record may not be settled before its day has ended — and a test that waits for
    midnight is not a test.
    """

    def __init__(
        self,
        repository: OvertimeRepository,
        session: AsyncSession,
        *,
        approvals: ApprovalNotifier,
        attendance: AttendanceDays,
        threshold_minutes: int = DEFAULT_CONFIRMATION_THRESHOLD_MINUTES,
        now: TimeSource = utc_now,
    ) -> None:
        self._repository = repository
        self._session = session
        self._approvals = approvals
        self._attendance = attendance
        self._threshold_minutes = threshold_minutes
        self._now = now

    @property
    def threshold_minutes(self) -> int:
        """The configured tolerance, reported beside every flag it produced."""
        return self._threshold_minutes

    # --- requests -----------------------------------------------------------

    async def draft(
        self,
        *,
        employee_id: UUID,
        business_date: date,
        expected_minutes: int,
        reason: str,
        actor_user_id: UUID | None = None,
        actor_roles: frozenset[str] | None = None,
    ) -> OvertimeRequestView:
        """Write the request, having refused what could never be filed.

        Everything a later step would refuse is refused here, while the document is
        still the requester's to fix: a date that has passed (the pre-approval rule
        itself), one too far ahead to be a plan, minutes no day holds, an empty reason,
        and a day that already carries a request or a record.
        """
        await self._require_employee(employee_id)
        await self._require_requestable(
            employee_id,
            business_date=business_date,
            expected_minutes=expected_minutes,
            reason=reason,
        )

        request = await self._repository.save_request(
            OvertimeRequestInput(
                employee_id=employee_id,
                business_date=business_date,
                expected_minutes=expected_minutes,
                reason=reason.strip(),
            )
        )
        await record(
            self._session,
            action=AuditAction.OVERTIME_REQUEST_DRAFTED,
            entity_type=REQUEST_ENTITY,
            entity_id=request.id,
            after={
                "employee_id": employee_id,
                "business_date": business_date.isoformat(),
                "expected_minutes": expected_minutes,
                "reason": request.reason,
            },
            actor_user_id=actor_user_id,
            actor_roles=actor_roles,
        )
        await self._repository.commit()
        return await self.get(request.id)

    async def update(
        self,
        request_id: UUID,
        patch: OvertimeRequestPatch,
        *,
        actor_user_id: UUID | None = None,
        actor_roles: frozenset[str] | None = None,
    ) -> OvertimeRequestView:
        """Change a draft — which is what a request returned for correction needs.

        Only a draft: a filed document is what two people were asked to approve, and
        the way to change an approved one is HR's confirmation of the record, which
        leaves the original figure readable.
        """
        request = await self._require(request_id)
        await self._require_state(request, OvertimeRequestState.DRAFT, "changed")

        business_date = patch.business_date or request.business_date
        expected_minutes = (
            request.expected_minutes if patch.expected_minutes is None else patch.expected_minutes
        )
        reason = request.reason if patch.reason is None else patch.reason
        await self._require_requestable(
            request.employee_id,
            business_date=business_date,
            expected_minutes=expected_minutes,
            reason=reason,
            excluding=request.id,
        )

        before = {
            "business_date": request.business_date.isoformat(),
            "expected_minutes": request.expected_minutes,
            "reason": request.reason,
        }
        written = await self._repository.write_draft(
            request.id,
            patch=OvertimeRequestPatch(
                business_date=business_date,
                expected_minutes=expected_minutes,
                reason=reason.strip(),
            ),
        )
        await record(
            self._session,
            action=AuditAction.OVERTIME_REQUEST_UPDATED,
            entity_type=REQUEST_ENTITY,
            entity_id=written.id,
            before=before,
            after={
                "business_date": written.business_date.isoformat(),
                "expected_minutes": written.expected_minutes,
                "reason": written.reason,
            },
            actor_user_id=actor_user_id,
            actor_roles=actor_roles,
        )
        await self._repository.commit()
        return await self.get(request_id)

    async def submit(self, request_id: UUID) -> OvertimeRequestView:
        """Hand the document to the engine, as the person it is about.

        The requester is the employee the request names rather than whoever pressed
        the button — nobody files somebody else's overtime, and the route the engine
        resolves is about them.
        """
        request = await self._require(request_id)
        await self._require_state(request, OvertimeRequestState.DRAFT, "filed")

        try:
            approval_request_id = await self._approvals.submit(
                ENTITY_TYPE, request.id, request.employee_id, SubmitContext()
            )
        except DomainError as error:
            # The engine's refusal in this module's vocabulary: the client routes on
            # the code, and `ERR_APR_002` would tell somebody looking at their own
            # request nothing about it. The engine's own code travels in the detail.
            raise DomainError(
                OvertimeErrorCode.SUBMISSION_REFUSED,
                detail=f"the approval engine refused overtime request {request_id}: {error}",
            ) from error

        await self._repository.mark_filed(
            request.id, approval_request_id=approval_request_id, at=self._now()
        )
        await self._repository.commit()
        return await self.get(request_id)

    async def decide(
        self,
        request_id: UUID,
        *,
        approver_employee_id: UUID,
        decision: DecisionKind,
        comment: str | None = None,
        actor_user_id: UUID | None = None,
        actor_roles: frozenset[str] | None = None,
    ) -> OvertimeRequestView:
        """Record one level's decision, and write the record when it is final.

        Who may decide is the engine's answer — the requester's manager at the first
        level, any HR member other than the requester at the second — so this method
        tests no role. What it does do is *finish* the document: an approval that
        produced no record would be a decision that changed nothing.
        """
        request = await self._require(request_id)
        state = await self._approvals.state_of(ENTITY_TYPE, request.id)
        if state is None:
            raise DomainError(
                OvertimeErrorCode.REQUEST_NOT_DRAFT,
                detail=f"overtime request {request_id} has not been filed",
            )
        await self._approvals.decide(
            state.id,
            approver_employee_id,
            decision,
            comment,
            actor_user_id=actor_user_id,
            actor_roles=actor_roles,
        )

        report = await self.resolve_decided(request_id=request_id)
        failure = report.failure_for(request_id)
        if failure is not None:
            raise DomainError(
                OvertimeErrorCode.RESOLVE_FAILED,
                detail=(
                    f"overtime request {request_id} was decided and its record could not "
                    f"be written: {failure.detail}"
                ),
            )
        return await self.get(request_id)

    async def withdraw(
        self,
        request_id: UUID,
        *,
        actor_employee_id: UUID,
        actor_user_id: UUID | None = None,
        actor_roles: frozenset[str] | None = None,
    ) -> OvertimeRequestView:
        """Stop a request that has not been approved, and give the day back.

        Two mechanisms behind one act, because to the person doing it they are one: a
        filed request is withdrawn through the engine (which is where the approver's
        queue is emptied), and a draft — which the day's one-request rule is already
        holding — is closed here.

        An approved request is refused with the alternative named. Its record is the
        fact of the month, and the way to correct it is HR's confirmation, which keeps
        the original figure rather than deleting the day.
        """
        request = await self._require(request_id)
        state = await self._state_of(request)
        if state is OvertimeRequestState.APPROVED:
            raise DomainError(
                OvertimeErrorCode.NOT_WITHDRAWABLE,
                detail=(
                    f"overtime request {request_id} was approved and its record stands; "
                    "HR confirms or adjusts the day's record rather than withdrawing the "
                    "request"
                ),
            )
        if state is OvertimeRequestState.IN_APPROVAL and request.approval_request_id is not None:
            await self._approvals.withdraw(request.approval_request_id, actor_employee_id)
        elif state is not OvertimeRequestState.DRAFT:
            raise DomainError(
                OvertimeErrorCode.NOT_WITHDRAWABLE,
                detail=f"overtime request {request_id} is {state} and cannot be withdrawn",
            )

        at = self._now()
        await self._repository.mark_withdrawn(request.id, at=at)
        await self._repository.mark_settled(request.id, at=at)
        await record(
            self._session,
            action=AuditAction.OVERTIME_REQUEST_WITHDRAWN,
            entity_type=REQUEST_ENTITY,
            entity_id=request.id,
            before={"state": state.value},
            after={
                "state": OvertimeRequestState.WITHDRAWN.value,
                "employee_id": request.employee_id,
                "business_date": request.business_date.isoformat(),
                "expected_minutes": request.expected_minutes,
                "withdrawn_by_employee_id": actor_employee_id,
            },
            reason="withdrawn before the day it was asked for",
            actor_user_id=actor_user_id,
            actor_roles=actor_roles,
        )
        await self._repository.commit()
        return await self.get(request_id)

    # --- the resolve sweep --------------------------------------------------

    async def resolve_decided(self, *, request_id: UUID | None = None) -> ResolveReport:
        """Write the record for every approved request that has not got one yet.

        Idempotent by construction: a document whose record exists is not a candidate,
        and `uq_overtime_records_request` refuses a second one anyway. A document the
        engine has not decided is left alone — the engine is the only thing that can
        answer, and asking it once per document is what this loop does. One document per
        transaction, so a failure is that document's own and the ones behind it are not
        held up.
        """
        resolved: list[OvertimeRecord] = []
        failed: list[ResolveFailure] = []
        examined: set[UUID] = set()

        while True:
            candidate = await self._repository.lock_next_unresolved(
                exclude=frozenset(examined), only=request_id
            )
            if candidate is None:
                break
            examined.add(candidate.id)

            status = await self._repository.approval_status_of(candidate.id)
            if status is None or status in _UNDECIDED:
                # In the queue, or returned for correction: the document is still going
                # to be decided. The lock goes back with the commit and this document is
                # not a candidate again in this run.
                await self._repository.commit()
                continue

            try:
                resolved.append(await self._resolve(candidate, status))
            except Exception as error:  # noqa: BLE001 - reported, never fatal
                await self._repository.rollback()
                failed.append(_resolve_failure(candidate.id, error))
                continue

        return ResolveReport(resolved=tuple(resolved), failed=tuple(failed))

    async def _resolve(
        self, request: OvertimeRequest, status: ApprovalStatus
    ) -> OvertimeRecord | None:
        """One document, one transaction: write the record, or close it empty.

        A rejection or a withdrawal writes nothing — the engine's answer is that there
        is no overtime — and the document is closed so the day is free for a new
        request. An approval writes the record and then rebuilds the day's attendance
        snapshot, which is where "approved overtime for the day" becomes readable to
        everybody else.
        """
        at = self._now()
        written: OvertimeRecord | None = None
        if status is ApprovalStatus.APPROVED:
            written = await self._repository.save_record(
                NewOvertimeRecord(
                    request_id=request.id,
                    employee_id=request.employee_id,
                    business_date=request.business_date,
                    month_bucket=month_bucket_of(request.business_date),
                    approved_minutes=request.expected_minutes,
                )
            )
            await self._append(written, OvertimeEntryType.APPROVE)
            await self._repository.mark_approved(request.id, at=at)
        await self._repository.mark_settled(request.id, at=at)
        await record(
            self._session,
            action=AuditAction.OVERTIME_REQUEST_RESOLVED,
            entity_type=REQUEST_ENTITY,
            entity_id=request.id,
            after={
                "employee_id": request.employee_id,
                "engine_status": status.value,
                "business_date": request.business_date.isoformat(),
                "approved_minutes": (
                    written.approved_minutes if written is not None else None
                ),
                "month_bucket": written.month_bucket if written is not None else None,
            },
            reason=f"overtime request closed as {status.value}",
            initiated_by="system",
        )
        if written is not None:
            await self._rebuild_day(written.employee_id, written.business_date)
        await self._repository.commit()
        return written

    # --- the settle sweep ---------------------------------------------------

    async def settle_due(
        self,
        *,
        month: str | None = None,
        record_id: UUID | None = None,
        on_date: date | None = None,
    ) -> SettleReport:
        """Compute every record whose day has ended, oldest first.

        Idempotent: a record that is already settled is not a candidate, and settlement
        is arithmetic over the day rather than an accumulation. A record whose day has
        not ended is examined and deliberately left alone — its worked minutes are still
        growing, and the smaller of the two figures is not answerable yet — which is
        reported as `skipped` rather than as a failure.
        """
        today = on_date or madrid_today(self._now())
        settled: list[OvertimeRecord] = []
        failed: list[SettleFailure] = []
        examined: set[UUID] = set()
        skipped = 0

        while True:
            candidate = await self._repository.lock_next_unsettled(
                month=month, exclude=frozenset(examined), only=record_id
            )
            if candidate is None:
                break
            examined.add(candidate.id)

            if candidate.business_date >= today:
                skipped += 1
                await self._repository.commit()
                continue

            try:
                settled.append(await self._settle(candidate))
            except Exception as error:  # noqa: BLE001 - reported, never fatal
                await self._repository.rollback()
                failed.append(_settle_failure(candidate.id, error))
                continue

        return SettleReport(settled=tuple(settled), skipped=skipped, failed=tuple(failed))

    async def _settle(self, record: OvertimeRecord) -> OvertimeRecord:
        """One record, one transaction: read the day, take the smaller figure.

        The worked figure is the attendance module's own answer rather than a second
        reading of the punch stream: the day a reader opens and the day this settlement
        measured have to be the same day, and that module is where the business date,
        the corrections and the DST transitions are already decided.
        """
        day = await self._attendance.day_view(record.employee_id, record.business_date)
        worked = day.worked_minutes
        computed = min(record.approved_minutes, worked)
        difference = abs(record.approved_minutes - worked)
        flagged = difference > self._threshold_minutes
        at = self._now()

        written = await self._repository.write_settlement(
            record.id,
            worked_minutes=worked,
            computed_minutes=computed,
            needs_confirmation=flagged,
            at=at,
        )
        await self._append(
            written,
            OvertimeEntryType.SETTLE,
            note=(
                f"approved {written.approved_minutes} and worked {worked} differ by "
                f"{difference}, beyond the {self._threshold_minutes} this company allows"
                if flagged
                else f"approved {written.approved_minutes} against worked {worked}"
            ),
        )
        await self._audit_settled(written, needs_confirmation=flagged, difference=difference)
        await self._rebuild_day(written.employee_id, written.business_date)
        await self._repository.commit()
        return written

    async def confirm(
        self,
        record_id: UUID,
        *,
        minutes: int,
        note: str,
        confirmed_by_employee_id: UUID | None = None,
        actor_user_id: UUID | None = None,
        actor_roles: frozenset[str] | None = None,
    ) -> OvertimeRecordView:
        """Write HR's figure beside the computed one, with the reason.

        Refused before the day has been settled: there would be no original to keep, and
        "beside" is the whole guarantee — HR adjusts a figure the system has already
        computed rather than supplying one it never had. The computed minutes, the
        worked minutes and the approved minutes are not touched; the flag that asked for
        the confirmation is cleared, because the queue is answered and the ledger is
        where the history of it lives.
        """
        if not note or not note.strip():
            raise DomainError(
                OvertimeErrorCode.INVALID_REQUEST,
                detail="confirming or changing overtime states why",
            )
        if not 0 <= minutes <= MAX_DAY_MINUTES:
            raise DomainError(
                OvertimeErrorCode.REQUEST_INVALID,
                detail=f"{minutes} minutes is not a figure a day holds",
            )

        existing = await self._require_record(record_id)
        if not existing.is_settled:
            raise DomainError(
                OvertimeErrorCode.RECORD_NOT_SETTLED,
                detail=(
                    f"overtime record {record_id} is for {existing.business_date} and that day "
                    "has not been computed; the hours actually worked are compared once the "
                    "day is over"
                ),
            )

        written = await self._repository.write_confirmation(
            existing.id,
            confirmed_minutes=minutes,
            note=note.strip(),
            confirmed_by_employee_id=confirmed_by_employee_id,
            at=self._now(),
        )
        await self._append(
            written,
            OvertimeEntryType.CONFIRM,
            note=note.strip(),
            created_by_employee_id=confirmed_by_employee_id,
        )
        await record(
            self._session,
            action=AuditAction.OVERTIME_RECORD_CONFIRMED,
            entity_type=RECORD_ENTITY,
            entity_id=written.id,
            before={
                "computed_minutes": existing.computed_minutes,
                "confirmed_minutes": existing.confirmed_minutes,
                "needs_confirmation": existing.needs_confirmation,
            },
            after={
                "computed_minutes": written.computed_minutes,
                "confirmed_minutes": written.confirmed_minutes,
                "needs_confirmation": written.needs_confirmation,
                "employee_id": written.employee_id,
                "business_date": written.business_date.isoformat(),
                "confirmed_by_employee_id": confirmed_by_employee_id,
            },
            reason=note.strip(),
            actor_user_id=actor_user_id,
            actor_roles=actor_roles,
        )
        await self._rebuild_day(written.employee_id, written.business_date)
        await self._repository.commit()
        return await self.get_record(record_id)

    # --- reads --------------------------------------------------------------

    async def get(self, request_id: UUID) -> OvertimeRequestView:
        return await self._view(await self._require(request_id))

    async def list_requests(
        self, query: OvertimeRequestQuery
    ) -> tuple[list[OvertimeRequestView], int]:
        """A page of requests, newest first, each with its state.

        The state comes from the query — one join for the whole page — so listing does
        not ask the engine once per row, and `approval` is deliberately absent: the
        steps and comments behind one document are read from its detail.
        """
        rows = await self._repository.list_requests(query)
        count = await self._repository.count_requests(query)
        return [
            OvertimeRequestView(request=request, state=state) for request, state in rows
        ], count

    async def get_record(self, record_id: UUID) -> OvertimeRecordView:
        record = await self._require_record(record_id)
        return OvertimeRecordView(
            record=record, history=tuple(await self._repository.entries_for_record(record.id))
        )

    async def list_records(self, query: OvertimeRecordQuery) -> tuple[list[OvertimeRecord], int]:
        """A page of records, newest day first.

        Without their history: a ledger per row would be one query per row, and the
        movements behind one record are read from its detail — the convention the leave
        module's request list follows for the same reason.
        """
        return (
            await self._repository.list_records(query),
            await self._repository.count_records(query),
        )

    async def summary(self, month: str, *, employee_id: UUID | None = None) -> MonthlySummary:
        """A month, per employee, and the month's totals.

        `employee_id` narrows it to one person — the self-service read — without a
        second query shape: "my total" and "the company's" are the same group-by with
        and without a filter, so the two cannot drift.
        """
        self._require_period(month)
        rows = await self._repository.month_totals(month)
        if employee_id is not None:
            rows = [row for row in rows if row.employee_id == employee_id]
        return MonthlySummary(month=month, totals=tuple(rows))

    async def export_month(
        self,
        month: str,
        *,
        actor_user_id: UUID | None = None,
        actor_roles: frozenset[str] | None = None,
    ) -> ExportFile:
        """The month as a file, and one audit record per export.

        The file is a *read*: exporting does not settle, confirm or mark anything, so
        finance re-running a month produces the same bytes every time and each run
        leaves its own trail entry. What the trail carries is the period, the number of
        lines and the minutes the file stated — never an amount, because the file has
        none.
        """
        self._require_period(month)
        rows = await self._repository.export_rows(month)
        await record(
            self._session,
            action=AuditAction.DATA_EXPORTED,
            entity_type=EXPORT_ENTITY,
            entity_id=None,
            after={
                "period": month,
                "records": len(rows),
                "approved_minutes": sum(row.approved_minutes for row in rows),
                "confirmed_minutes": sum(
                    row.confirmed_minutes for row in rows if row.confirmed_minutes is not None
                ),
            },
            reason=f"overtime for {month} exported",
            actor_user_id=actor_user_id,
            actor_roles=actor_roles,
        )
        await self._repository.commit()
        return ExportFile(filename=f"overtime-{month}.csv", content=render(rows))

    # --- internals: writes --------------------------------------------------

    async def _append(
        self,
        record_row: OvertimeRecord,
        entry_type: OvertimeEntryType,
        *,
        note: str | None = None,
        created_by_employee_id: UUID | None = None,
    ) -> None:
        """One ledger movement, with the three figures that followed it."""
        await self._repository.append_entry(
            record_id=record_row.id,
            entry_type=entry_type,
            approved_minutes=record_row.approved_minutes,
            computed_minutes=record_row.computed_minutes,
            confirmed_minutes=record_row.confirmed_minutes,
            note=note,
            created_by_employee_id=created_by_employee_id,
        )

    async def _rebuild_day(self, employee_id: UUID, business_date: date) -> None:
        """Make the attendance day carry the overtime figure this write produced.

        The same transaction as the record, and that is the point: the day's
        `overtime_minutes` and the record it was read from are one event, so a reader
        can never find one without the other. The attendance module asks this module
        what the day holds (see `OvertimeLedger`), so the number written here is the
        number that module computes rather than a second copy of it.
        """
        await self._attendance.rebuild_day(employee_id, business_date)

    async def _audit_settled(
        self, written: OvertimeRecord, *, needs_confirmation: bool, difference: int
    ) -> None:
        """The settlement's trail entry, in the transaction that wrote the figures."""
        await record(
            self._session,
            action=AuditAction.OVERTIME_RECORD_SETTLED,
            entity_type=RECORD_ENTITY,
            entity_id=written.id,
            after={
                "employee_id": written.employee_id,
                "business_date": written.business_date.isoformat(),
                "approved_minutes": written.approved_minutes,
                "worked_minutes": written.worked_minutes,
                "computed_minutes": written.computed_minutes,
                "difference_minutes": difference,
                "needs_confirmation": needs_confirmation,
            },
            reason=(
                "the two figures differ and the record waits for HR"
                if needs_confirmation
                else "settled as the smaller of the approved and the worked minutes"
            ),
            initiated_by="system",
        )

    # --- internals: reads and lookups ---------------------------------------

    async def _view(self, request: OvertimeRequest) -> OvertimeRequestView:
        approval = await self._approvals.state_of(ENTITY_TYPE, request.id)
        record_row = await self._repository.record_for_request(request.id)
        return OvertimeRequestView(
            request=request,
            state=state_of_request(request, approval.status if approval is not None else None),
            approval=approval,
            record_id=record_row.id if record_row is not None else None,
        )

    async def _state_of(self, request: OvertimeRequest) -> OvertimeRequestState:
        approval = await self._approvals.state_of(ENTITY_TYPE, request.id)
        return state_of_request(request, approval.status if approval is not None else None)

    async def _require(self, request_id: UUID) -> OvertimeRequest:
        request = await self._repository.get_request(request_id)
        if request is None:
            raise DomainError(
                OvertimeErrorCode.REQUEST_NOT_FOUND,
                detail=f"unknown overtime request {request_id}",
            )
        return request

    async def _require_record(self, record_id: UUID) -> OvertimeRecord:
        record_row = await self._repository.get_record(record_id)
        if record_row is None:
            raise DomainError(
                OvertimeErrorCode.RECORD_NOT_FOUND,
                detail=f"unknown overtime record {record_id}",
            )
        return record_row

    async def _require_state(
        self, request: OvertimeRequest, wanted: OvertimeRequestState, act: str
    ) -> None:
        """Refuse an act the document's state does not admit.

        Read through `state_of_request` with the engine's answer, so a document that was
        approved or rejected while this module was not looking cannot be filed on the
        strength of its own row.
        """
        state = await self._state_of(request)
        if state is not wanted:
            raise DomainError(
                OvertimeErrorCode.REQUEST_NOT_DRAFT,
                detail=f"overtime request {request.id} is {state} and cannot be {act}",
            )

    async def _require_employee(self, employee_id: UUID) -> None:
        if not await self._repository.employee_exists(employee_id):
            raise DomainError(
                OvertimeErrorCode.EMPLOYEE_NOT_FOUND, detail=f"unknown employee {employee_id}"
            )

    async def _require_requestable(
        self,
        employee_id: UUID,
        *,
        business_date: date,
        expected_minutes: int,
        reason: str,
        excluding: UUID | None = None,
    ) -> None:
        """The rules a request has to satisfy whether it is drafted or corrected.

        One method for both, because a draft that is edited into a past date is the same
        retroactive entry as one filed that way, and two copies of this rule would
        eventually disagree about which of them the module enforces.
        """
        today = madrid_today(self._now())
        if business_date < today:
            raise DomainError(
                OvertimeErrorCode.REQUEST_INVALID,
                detail=(
                    f"{business_date} has passed; overtime is applied for in advance and "
                    "this system has no way to record it afterwards"
                ),
            )
        if (business_date - today).days > MAX_DAYS_AHEAD:
            raise DomainError(
                OvertimeErrorCode.REQUEST_INVALID,
                detail=f"{business_date} is more than {MAX_DAYS_AHEAD} days ahead",
            )
        if not 1 <= expected_minutes <= MAX_DAY_MINUTES:
            raise DomainError(
                OvertimeErrorCode.REQUEST_INVALID,
                detail=f"expected_minutes must be between 1 and {MAX_DAY_MINUTES}",
            )
        if not reason or not reason.strip():
            raise DomainError(
                OvertimeErrorCode.REQUEST_INVALID, detail="an overtime request states why"
            )

        live = await self._repository.live_request_for_day(
            employee_id, business_date, excluding=excluding
        )
        if live is not None:
            raise DomainError(
                OvertimeErrorCode.REQUEST_EXISTS,
                detail=(
                    f"{employee_id} already has an open overtime request for "
                    f"{business_date}"
                ),
            )
        existing = await self._repository.record_for_day(employee_id, business_date)
        if existing is not None:
            raise DomainError(
                OvertimeErrorCode.REQUEST_EXISTS,
                detail=(
                    f"{employee_id} already has an overtime record for {business_date} "
                    f"({existing.approved_minutes} minutes approved)"
                ),
            )

    def _require_period(self, month: str) -> tuple[int, int]:
        period = period_of(month)
        if period == (0, 0):
            raise DomainError(
                OvertimeErrorCode.PERIOD_INVALID,
                detail=f"{month!r} is not a period this module holds; use YYYY-MM",
            )
        return period


class OvertimeLedger:
    """The overtime module as the attendance module sees it: one question, one answer.

    The implementation of the attendance seam, and a separate class rather than
    `OvertimeService` itself so the seam is satisfied by something holding nothing but
    the repository: the attendance derivation asks "what does this day's overtime come
    to" and cannot reach the rest of this module's surface through the object it was
    handed.

    One indexed query per question, asked once per day the attendance module derives —
    the same shape as the leave calendar and the day expectation the scan asks for
    beside it.
    """

    def __init__(self, repository: OvertimeRepository) -> None:
        self._repository = repository

    async def overtime_minutes(self, employee_id: UUID, business_date: date) -> int | None:
        """The day's approved overtime, or nothing when the day has none.

        "Nothing" and zero are different answers and stay different all the way to the
        client: zero is "the day was approved and came to nothing" — nobody worked, or
        HR confirmed it away — and null is "no overtime was approved for this day".
        """
        return await self._repository.day_minutes(employee_id, business_date)

    async def overtime_by_date(
        self, employee_id: UUID, from_date: date, to_date: date
    ) -> dict[date, int]:
        """The same for a range, in one query. Empty days are absent, not zero."""
        return await self._repository.minutes_by_date(employee_id, from_date, to_date)


#: The engine's statuses that mean "not decided yet": the document stays open.
_UNDECIDED = frozenset(
    {ApprovalStatus.DRAFT, ApprovalStatus.PENDING_FIRST, ApprovalStatus.PENDING_SECOND}
)


def _resolve_failure(request_id: UUID, error: Exception) -> ResolveFailure:
    """What went wrong, as a catalogued code where there is one."""
    code = error.code.value if isinstance(error, DomainError) else ErrorCode.INTERNAL_ERROR.value
    return ResolveFailure(request_id=request_id, code=code, detail=str(error))


def _settle_failure(record_id: UUID, error: Exception) -> SettleFailure:
    code = error.code.value if isinstance(error, DomainError) else ErrorCode.INTERNAL_ERROR.value
    return SettleFailure(record_id=record_id, code=code, detail=str(error))


__all__ = [
    "DEFAULT_CONFIRMATION_THRESHOLD_MINUTES",
    "AttendanceDays",
    "OvertimeLedger",
    "OvertimeService",
]
