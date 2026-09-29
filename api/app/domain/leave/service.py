"""Leave: the catalogue, the allowance, the request, and the day it covers.

`docs/architecture/codebase-design.md` §2.3 fixes the shape: there is **one** state
machine and it is the approval engine's. Nothing here decides whether a leave was
approved — the service hands the document over and records what the engine said —
and nothing here notifies anybody, because the engine is wrapped in
`ApprovalNotifier` and the notices follow the decision the way they do for every
other document in the system.

Six decisions, and the rest of the module follows from them:

* **The schedule module answers "is this a working day".** A request's cost is the
  days `ScheduleService.day_expectations` calls working days, which already excludes
  weekends (no schedule day) and holidays (zeroed minutes). Asking rather than
  re-deriving is the point of ticket 22 having landed first: a second calendar in
  this module would eventually disagree with the expected-hours figure a month is
  measured against. A range with no working day in it is refused rather than
  accepted as zero days — such a leave costs nothing, so no balance could account
  for it, and it is also the shape a misconfigured employee would slip through.

* **A request reserves, an approval spends, and a refusal gives back.** Filing moves
  the days into `pending` under the balance row's lock, an approval moves them to
  `used`, and a rejection or a withdrawal releases them. Every movement is a ledger
  row carrying the totals that followed it, so a balance's history is what happened
  rather than a reconstruction of it. `settle_decided` is the sweep that finishes a
  settlement the process died before completing — the engine commits its own
  decision, so the gap between "the engine approved" and "the balance knows" is real
  and is retried rather than assumed away.

* **The allowance is checked twice and enforced once more.** Once while the request
  is a draft, so somebody learns they have three days left before two approvers are
  asked; again at filing, under `SELECT ... FOR UPDATE`, which is where it becomes a
  decision rather than a courtesy; and the database's
  `ck_leave_balances_within_allowance` is the third layer, which no race can pass.
  The refusal states the remainder — the ticket asks for that in as many words.

* **Cross-year requests are split at the boundary and charged to each year.** The
  rule is `calculation.counts_by_year`: December's days come out of December's
  balance and January's out of January's. A year with no row yet gets one, with the
  configured allowance and nothing carried over — carrying days is a decision
  somebody makes, and a default this module invented would be an allowance nobody
  granted. The split is read back from the ledger (`allocations_of`) rather than
  recomputed, so a schedule edited afterwards cannot change what a past request cost.

* **Withdrawal is the requester's act, and only before the leave starts.** A filed
  request is stopped through the engine; an approved one is cancelled here, because
  the engine's withdrawal stops working once it holds an approval and "I am not
  going away after all" is not the engine's question. A leave that has begun cannot
  be withdrawn at all: the refusal names the alternative — HR's after-the-fact
  correction — rather than leaving somebody to guess.

* **A sick note is a type, two dates, and a reference to a file.** The request has
  no `reason`, no `note` and no other free text (the model's docstring carries the
  argument, and `docs/DESIGN.md` §8 is why); a type that requires an attachment is
  refused without one; and the reference is shown to HR alone, which is why
  `leave.attachment_read` is its own catalogued action rather than a field on a
  payload. The file itself is ticket 31's document store — until it lands the
  reference is an opaque string this module never dereferences.
"""

from datetime import date, datetime
from re import compile as compile_pattern
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditAction, record
from app.core.errors import ErrorCode
from app.domain.approval.models import ApprovalStatus, DecisionKind, SubmitContext
from app.domain.attendance.business_day import madrid_today
from app.domain.attendance.models import ExpectationSource, TimeSource, utc_now
from app.domain.errors import DomainError
from app.domain.leave.calculation import counts_by_year, total, working_days
from app.domain.leave.errors import LeaveErrorCode
from app.domain.leave.models import (
    ATTACHMENT_REFERENCE_PATTERN,
    ENTITY_TYPE,
    BalanceGrant,
    LeaveBalance,
    LeaveBalanceView,
    LeaveDay,
    LeaveEntryType,
    LeaveRequest,
    LeaveRequestCheck,
    LeaveRequestInput,
    LeaveRequestQuery,
    LeaveRequestState,
    LeaveRequestView,
    LeaveType,
    LeaveTypeInput,
    LeaveTypePatch,
    SettleFailure,
    SettleReport,
    YearAllocation,
    state_of_request,
)
from app.domain.leave.repository import LeaveRepository
from app.domain.notification.approval import ApprovalNotifier

#: The audit entity types. One string per table, so a filter on the trail reads
#: "everything that happened to the leave catalogue" as a prefix.
TYPE_ENTITY = "leave_type"
BALANCE_ENTITY = "leave_balance"
REQUEST_ENTITY = ENTITY_TYPE

#: Bounds that turn a typo into a refusal rather than into a document nobody notices
#: until the balance is empty. Not policy: the longest leave this module will hold,
#: and how far ahead it will look.
MAX_REQUEST_DAYS = 366
MAX_YEARS_AHEAD = 5

#: What a type code looks like. Lowercase, because a code is what a request, a
#: report and an import name a type by and two spellings of one type is how a
#: catalogue grows a duplicate.
_TYPE_CODE = compile_pattern(r"^[a-z][a-z0-9_]{1,31}$")
_ATTACHMENT_REFERENCE = compile_pattern(ATTACHMENT_REFERENCE_PATTERN)


class LeaveService:
    """The catalogue, the balances, the requests, and the settle sweep.

    `expectations` is the scheduling module seen through `ExpectationSource` — the
    same seam the attendance service takes — and `approvals` is the engine *wrapped*
    in `ApprovalNotifier`, so a decision's notifications cannot be forgotten.
    `annual_leave_days` is the configured allowance, carried here rather than read
    from settings at each use so that the number a balance was materialised from and
    the number the API reports are the same one.
    """

    def __init__(
        self,
        repository: LeaveRepository,
        session: AsyncSession,
        *,
        expectations: ExpectationSource,
        approvals: ApprovalNotifier,
        annual_leave_days: int = 30,
        now: TimeSource = utc_now,
    ) -> None:
        self._repository = repository
        self._session = session
        self._expectations = expectations
        self._approvals = approvals
        self._annual_leave_days = annual_leave_days
        self._now = now

    @property
    def annual_leave_days(self) -> int:
        """The configured allowance, reported beside every balance it produced."""
        return self._annual_leave_days

    # --- the catalogue ------------------------------------------------------

    async def list_types(self, *, include_inactive: bool = False) -> list[LeaveType]:
        return await self._repository.list_types(include_inactive=include_inactive)

    async def get_type(self, code: str) -> LeaveType:
        return await self._require_type(code)

    async def create_type(
        self,
        data: LeaveTypeInput,
        *,
        actor_user_id: UUID | None = None,
        actor_roles: frozenset[str] | None = None,
    ) -> LeaveType:
        """Add a kind of leave to the catalogue."""
        _validate_type(data)
        if await self._repository.get_type_by_code(data.code) is not None:
            raise DomainError(
                LeaveErrorCode.TYPE_CODE_TAKEN, detail=f"leave type {data.code} already exists"
            )
        saved = await self._repository.save_type(data)
        await record(
            self._session,
            action=AuditAction.LEAVE_TYPE_CREATED,
            entity_type=TYPE_ENTITY,
            entity_id=saved.id,
            after=_type_audit(saved),
            actor_user_id=actor_user_id,
            actor_roles=actor_roles,
        )
        await self._repository.commit()
        return saved

    async def update_type(
        self,
        code: str,
        patch: LeaveTypePatch,
        *,
        actor_user_id: UUID | None = None,
        actor_roles: frozenset[str] | None = None,
    ) -> LeaveType:
        """Change what the patch states. The code is not one of those things."""
        current = await self._require_type(code)
        names = (patch.name_es or current.name_es, patch.name_en or current.name_en)
        if not all(name.strip() for name in names):
            raise DomainError(
                LeaveErrorCode.TYPE_INVALID,
                detail=f"leave type {code} is named in both languages",
            )
        updated = await self._repository.update_type(current.id, patch)
        await record(
            self._session,
            action=AuditAction.LEAVE_TYPE_UPDATED,
            entity_type=TYPE_ENTITY,
            entity_id=updated.id,
            before=_type_audit(current),
            after=_type_audit(updated),
            actor_user_id=actor_user_id,
            actor_roles=actor_roles,
        )
        await self._repository.commit()
        return updated

    # --- balances -----------------------------------------------------------

    async def balances(
        self, employee_id: UUID, *, year: int | None = None
    ) -> list[LeaveBalanceView]:
        """One person's balances, with the history that produced each of them.

        **A read does not write.** A year nobody has needed yet has no row, and it is
        returned *projected*: the allowance the parameter would grant, nothing spent,
        no id — so "you have 30 days" is answerable on the first page load without a
        `GET` creating the ledger account as a side effect. The row is materialised
        when something needs it (filing a request, or HR setting a figure), and that
        is where the `grant` entry is written.
        """
        await self._require_employee(employee_id)
        wanted = year or madrid_today(self._now()).year
        rows = await self._repository.list_balances(employee_id, year=year)
        types = await self._types_by_id()
        having = {row.leave_type_id for row in rows}

        views: list[LeaveBalanceView] = []
        for row in rows:
            leave_type = types.get(row.leave_type_id)
            if leave_type is None:  # pragma: no cover - the foreign key forbids it
                continue
            views.append(
                LeaveBalanceView(
                    balance=row,
                    leave_type=leave_type,
                    history=tuple(await self._repository.entries_for_balance(row.id)),
                )
            )
        for leave_type in types.values():
            if leave_type.id in having or not leave_type.counts_against_annual:
                continue
            views.append(
                LeaveBalanceView(
                    balance=_projected_balance(
                        employee_id, wanted, leave_type, self._annual_leave_days
                    ),
                    leave_type=leave_type,
                    history=(),
                    projected=True,
                )
            )
        return views

    async def balances_for_year(self, year: int) -> list[LeaveBalanceView]:
        """Everybody's, for one year: HR's company-wide read.

        Materialised rows only, and that is honest rather than convenient: "which
        balances exist for 2026" is a question about the ledger, and answering it
        with a projected row per employee would report an allowance for everybody who
        has never filed anything as though it had been granted.
        """
        rows = await self._repository.list_balances_for_year(year)
        types = await self._types_by_id()
        views: list[LeaveBalanceView] = []
        for row in rows:
            leave_type = types.get(row.leave_type_id)
            if leave_type is None:  # pragma: no cover - the foreign key forbids it
                continue
            views.append(
                LeaveBalanceView(
                    balance=row,
                    leave_type=leave_type,
                    history=tuple(await self._repository.entries_for_balance(row.id)),
                )
            )
        return views

    async def set_balance(
        self,
        employee_id: UUID,
        year: int,
        code: str,
        grant: BalanceGrant,
        *,
        actor_user_id: UUID | None = None,
        actor_roles: frozenset[str] | None = None,
        actor_employee_id: UUID | None = None,
    ) -> LeaveBalanceView:
        """Set somebody's entitlement or carry-over, and record why.

        The one place a figure other than the parameter is written, and the one place
        a decision about *carrying days over* is expressed. It cannot shrink a year
        below what is already spent and reserved: that would leave the balance
        describing a past that never happened, and the database's constraint would
        refuse it anyway.
        """
        leave_type = await self._require_type(code)
        if not leave_type.counts_against_annual:
            # Not a restriction for its own sake: this type has no allowance to
            # spend, so a balance row for it would be a number nothing reads.
            raise DomainError(
                LeaveErrorCode.TYPE_INVALID,
                detail=f"{code} does not count against an allowance, so it has no balance",
            )
        await self._require_employee(employee_id)
        _validate_year(year, self._now())

        balance = await self._materialise(employee_id, year, leave_type)
        balance = await self._repository.lock_balance(balance.id)
        entitled = (
            balance.entitled_days if grant.entitled_days is None else grant.entitled_days
        )
        carried = (
            balance.carried_over_days
            if grant.carried_over_days is None
            else grant.carried_over_days
        )
        if entitled + carried < balance.used_days + balance.pending_days:
            raise DomainError(
                LeaveErrorCode.BALANCE_INSUFFICIENT,
                detail=(
                    f"{year} {code}: {balance.used_days} used and {balance.pending_days} "
                    f"pending cannot be covered by {entitled} entitled plus {carried} "
                    "carried over"
                ),
            )

        before = _balance_audit(balance)
        written = await self._repository.write_balance(
            balance.id,
            entitled_days=entitled,
            carried_over_days=carried,
            used_days=balance.used_days,
            pending_days=balance.pending_days,
        )
        # One ledger row per figure that moved, each carrying the totals after both:
        # two decisions with one outcome reads better than an arithmetic puzzle.
        await self._append(
            written,
            LeaveEntryType.ADJUSTMENT,
            entitled - balance.entitled_days,
            note=grant.note,
            created_by_employee_id=actor_employee_id,
        )
        await self._append(
            written,
            LeaveEntryType.CARRY_OVER,
            carried - balance.carried_over_days,
            note=grant.note,
            created_by_employee_id=actor_employee_id,
        )
        await record(
            self._session,
            action=AuditAction.LEAVE_BALANCE_ADJUSTED,
            entity_type=BALANCE_ENTITY,
            entity_id=written.id,
            before=before,
            after=_balance_audit(written),
            reason=grant.note,
            actor_user_id=actor_user_id,
            actor_roles=actor_roles,
        )
        await self._repository.commit()
        return LeaveBalanceView(
            balance=written,
            leave_type=leave_type,
            history=tuple(await self._repository.entries_for_balance(written.id)),
        )

    # --- requests -----------------------------------------------------------

    async def check_request(
        self,
        *,
        employee_id: UUID,
        code: str,
        start_date: date,
        end_date: date,
        attachment_reference: str | None = None,
    ) -> LeaveRequestCheck:
        """Every refusal a request meets, and **not one write**.

        Extracted from `draft` in ticket 40, and the reason is the agent's draft tool: a
        draft the employee confirms must not be refused at submission for a rule the
        assistant never asked about, and the surest way to have one rule is to have one
        implementation. `draft` below is this method plus the three writes; the tool is this
        method and nothing else.

        The order of the refusals is `draft`'s own order and is preserved deliberately: a
        range that ends before it starts is refused before the balance is read, and a type
        nobody offers is refused before an employee is looked up. Reordering them would
        change *which* reason a request with two faults is told about, which is a behaviour
        change wearing a refactor's clothes.
        """
        leave_type = await self._require_type(code)
        self._require_active(leave_type)
        await self._require_employee(employee_id)
        self._require_window(start_date, end_date)
        self._require_attachment(leave_type, attachment_reference)

        days = await self._working_days(employee_id, start_date, end_date)
        counts = counts_by_year(days)
        overlap = await self._repository.live_request_overlapping(
            employee_id, start_date, end_date
        )
        if overlap is not None:
            raise DomainError(
                LeaveErrorCode.REQUEST_OVERLAPS,
                detail=(
                    f"{employee_id} already has a request covering "
                    f"{overlap.start_date}..{overlap.end_date}"
                ),
            )
        await self._require_affordable(employee_id, leave_type, counts)
        return LeaveRequestCheck(
            leave_type=leave_type,
            start_date=start_date,
            end_date=end_date,
            working_days=tuple(days),
            counts=counts,
            business_days_count=total(counts.values()),
            attachment_reference=attachment_reference,
        )

    async def draft(
        self,
        *,
        employee_id: UUID,
        code: str,
        start_date: date,
        end_date: date,
        attachment_reference: str | None = None,
        actor_user_id: UUID | None = None,
        actor_roles: frozenset[str] | None = None,
    ) -> LeaveRequestView:
        """Write the request, having refused what could never be filed.

        Everything a later step would refuse is refused here, while the document is
        still the requester's to fix: a range that ends before it starts, one with no
        working day in it, a retired type, a missing attachment, an attachment
        reference that is not a storage key, dates that overlap a leave already asked
        for, and — as a courtesy rather than as the decision — a balance that does not
        cover it. The authoritative check is at filing, under the row's lock.

        **The refusals are `check_request`'s, not this method's** (ticket 40). What is left
        here is the write: the row, the trail, and the read-back. The agent's draft tool
        calls the same `check_request`, so "a draft the assistant proposed" and "a request
        the employee filed" are checked by one implementation of every rule.
        """
        check = await self.check_request(
            employee_id=employee_id,
            code=code,
            start_date=start_date,
            end_date=end_date,
            attachment_reference=attachment_reference,
        )
        request = await self._repository.save_request(
            LeaveRequestInput(
                employee_id=employee_id,
                leave_type_id=check.leave_type.id,
                start_date=check.start_date,
                end_date=check.end_date,
                business_days_count=check.business_days_count,
                attachment_reference=check.attachment_reference,
            )
        )
        await record(
            self._session,
            action=AuditAction.LEAVE_REQUEST_DRAFTED,
            entity_type=REQUEST_ENTITY,
            entity_id=request.id,
            # Deliberately no attachment reference in the trail: it is a storage key
            # rather than a fact about the request, and compliance reads this record.
            after={
                "employee_id": employee_id,
                "leave_type": check.leave_type.code,
                "start_date": check.start_date.isoformat(),
                "end_date": check.end_date.isoformat(),
                "business_days_count": request.business_days_count,
            },
            actor_user_id=actor_user_id,
            actor_roles=actor_roles,
        )
        await self._repository.commit()
        return await self.get(request.id)

    async def submit(self, request_id: UUID) -> LeaveRequestView:
        """Reserve the days and hand the document to the engine.

        The order is the rule: the reservation happens **before** the engine is
        asked, so a request the year cannot afford is refused before two people are
        asked to decide it — and a request the engine refuses takes the reservation
        down with it, because the transaction is the request's and nothing here
        commits a half-filed document.

        The split is recomputed from the stored dates at this point rather than read
        back from the draft: a calendar edited between drafting and filing changes
        what the leave costs, and the figure charged is the figure the requester is
        then shown.
        """
        request = await self._require(request_id)
        await self._require_state(request, LeaveRequestState.DRAFT, "filed")
        leave_type = await self._require_type_by_id(request.leave_type_id)
        self._require_active(leave_type)
        self._require_attachment(leave_type, request.attachment_reference)

        days = await self._working_days(
            request.employee_id, request.start_date, request.end_date
        )
        counts = counts_by_year(days)
        await self._reserve(request, leave_type, counts)

        try:
            approval_request_id = await self._approvals.submit(
                ENTITY_TYPE, request.id, request.employee_id, SubmitContext()
            )
        except DomainError as error:
            # The engine's refusal in this module's vocabulary: the client routes on
            # the code, and `ERR_APR_002` would tell somebody looking at their own
            # leave nothing about it. The engine's own code travels in the detail.
            raise DomainError(
                LeaveErrorCode.SUBMISSION_REFUSED,
                detail=f"the approval engine refused leave request {request_id}: {error}",
            ) from error

        await self._repository.mark_filed(
            request.id,
            approval_request_id=approval_request_id,
            business_days_count=total(counts.values()),
            at=self._now(),
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
    ) -> LeaveRequestView:
        """Record one level's decision, and settle the balance when it is final.

        Who may decide is the engine's answer — the requester's manager at the first
        level, any HR member other than the requester at the second — so this method
        tests no role. What it does do is *finish* the document: an approval that left
        the days reserved would be a decision that changed nothing.
        """
        request = await self._require(request_id)
        state = await self._approvals.state_of(ENTITY_TYPE, request.id)
        if state is None:
            raise DomainError(
                LeaveErrorCode.REQUEST_NOT_DRAFT,
                detail=f"leave request {request_id} has not been filed",
            )
        await self._approvals.decide(
            state.id,
            approver_employee_id,
            decision,
            comment,
            actor_user_id=actor_user_id,
            actor_roles=actor_roles,
        )

        report = await self.settle_decided(request_id=request_id)
        failure = report.failure_for(request_id)
        if failure is not None:
            raise DomainError(
                LeaveErrorCode.SETTLE_FAILED,
                detail=(
                    f"leave request {request_id} was decided and its balance could not be "
                    f"settled: {failure.detail}"
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
    ) -> LeaveRequestView:
        """Stop a leave before it starts, and give the days back.

        Two mechanisms behind one act, because to the person doing it they are one: a
        request still in the queue is withdrawn through the engine (which is where the
        approver's queue is emptied), and an approved one is cancelled here — the
        engine's withdrawal stops working once it holds an approval, and "I am not
        going away after all" is this module's question rather than the engine's.

        A leave that has begun is refused with the alternative named: HR's
        after-the-fact correction is how the record of a day somebody was already away
        for is put right, and a withdrawal would silently turn an absence into a day
        of unexplained nothing.
        """
        request = await self._require(request_id)
        state = await self._state_of(request)
        today = madrid_today(self._now())
        if request.start_date <= today:
            raise DomainError(
                LeaveErrorCode.ALREADY_STARTED,
                detail=(
                    f"leave {request_id} starts on {request.start_date} and cannot be "
                    "withdrawn; HR corrects the record of a day that has begun through "
                    "the attendance correction flow (POST /api/v1/attendance/corrections)"
                ),
            )
        if state is LeaveRequestState.IN_APPROVAL and request.approval_request_id is not None:
            await self._approvals.withdraw(request.approval_request_id, actor_employee_id)
        elif state is not LeaveRequestState.APPROVED:
            raise DomainError(
                LeaveErrorCode.NOT_WITHDRAWABLE,
                detail=f"leave request {request_id} is {state} and cannot be withdrawn",
            )

        at = self._now()
        await self._release(request, refund=state is LeaveRequestState.APPROVED)
        await self._repository.mark_withdrawn(request.id, at=at)
        await self._repository.mark_settled(request.id, at=at)
        await record(
            self._session,
            action=AuditAction.LEAVE_REQUEST_WITHDRAWN,
            entity_type=REQUEST_ENTITY,
            entity_id=request.id,
            before={"state": state.value},
            after={
                "state": LeaveRequestState.WITHDRAWN.value,
                "employee_id": request.employee_id,
                "start_date": request.start_date.isoformat(),
                "end_date": request.end_date.isoformat(),
                "business_days_count": request.business_days_count,
                "withdrawn_by_employee_id": actor_employee_id,
            },
            reason="withdrawn before the leave started",
            actor_user_id=actor_user_id,
            actor_roles=actor_roles,
        )
        await self._repository.commit()
        return await self.get(request_id)

    # --- the settle sweep ---------------------------------------------------

    async def settle_decided(self, *, request_id: UUID | None = None) -> SettleReport:
        """Resolve every filed request's reservation, oldest first.

        Idempotent by construction: a request whose reservation is already resolved is
        not a candidate, and a movement of zero days moves nothing. A document the
        engine has not decided is left alone — the engine is the only thing that can
        answer, and asking it once per document is what this loop does. One document
        per transaction, so a failure is that document's own and the ones behind it
        are not held up.
        """
        settled: list[LeaveRequest] = []
        failed: list[SettleFailure] = []
        examined: set[UUID] = set()

        while True:
            candidate = await self._repository.lock_next_unsettled(
                exclude=frozenset(examined), only=request_id
            )
            if candidate is None:
                break
            examined.add(candidate.id)

            status = await self._repository.approval_status_of(candidate.id)
            if status is None or status in _UNDECIDED:
                # In the queue, or returned for correction: the days stay reserved,
                # because the request is still going to be decided. The lock goes back
                # with the commit and this document is not a candidate again.
                await self._repository.commit()
                continue

            try:
                settled.append(await self._settle(candidate, status))
            except Exception as error:  # noqa: BLE001 - reported, never fatal
                await self._repository.rollback()
                failed.append(_settle_failure(candidate.id, error))
                continue

        return SettleReport(settled=tuple(settled), failed=tuple(failed))

    async def _settle(self, request: LeaveRequest, status: ApprovalStatus) -> LeaveRequest:
        """One document, one transaction: spend the days, or give them back."""
        at = self._now()
        if status is ApprovalStatus.APPROVED:
            await self._consume(request)
            await self._repository.mark_approved(request.id, at=at)
        else:
            # Rejected, or withdrawn through the engine: whatever was reserved goes
            # back, and the request is closed.
            await self._release(request)
        await self._repository.mark_settled(request.id, at=at)
        await record(
            self._session,
            action=AuditAction.LEAVE_REQUEST_SETTLED,
            entity_type=REQUEST_ENTITY,
            entity_id=request.id,
            after={
                "employee_id": request.employee_id,
                "engine_status": status.value,
                "business_days_count": request.business_days_count,
                # The split as applied, read back from the ledger and not recomputed.
                "allocations": [
                    {"year": item.year, "days": item.days}
                    for item in await self._allocations(request.id)
                ],
            },
            reason=f"balance settled as {status.value}",
            initiated_by="system",
        )
        await self._repository.commit()
        return request

    # --- reads --------------------------------------------------------------

    async def get(self, request_id: UUID) -> LeaveRequestView:
        return await self._view(await self._require(request_id))

    async def list_requests(
        self, query: LeaveRequestQuery
    ) -> tuple[list[LeaveRequestView], int]:
        """A page of requests, newest first, each with its state and type.

        The state comes from the query — one join for the whole page — so listing does
        not ask the engine once per row, and `approval` is deliberately absent: the
        steps and comments behind one document are read from its detail.
        """
        rows = await self._repository.list_requests(query)
        count = await self._repository.count_requests(query)
        types = await self._types_by_id()
        views = [
            LeaveRequestView(
                request=request,
                state=state,
                leave_type=types[request.leave_type_id],
                allocations=await self._allocations(request.id),
            )
            for request, state in rows
        ]
        return views, count

    async def calendar(self, employee_id: UUID, from_date: date, to_date: date) -> list[LeaveDay]:
        """The days this person is away on approved leave, for a range.

        Every calendar date the leave covers, not only the working ones: this is the
        overlay an attendance calendar draws, and a leave that showed a gap on the
        Saturday in the middle would read as two leaves. Which of those days
        attendance expected anything from is the day record's answer.
        """
        await self._require_employee(employee_id)
        requests = await self._repository.approved_requests_covering(
            employee_id, from_date, to_date
        )
        types = await self._types_by_id()
        days: list[LeaveDay] = []
        for request in requests:
            leave_type = types.get(request.leave_type_id)
            if leave_type is None:  # pragma: no cover - the foreign key forbids it
                continue
            inside = _dates_of(
                max(request.start_date, from_date), min(request.end_date, to_date)
            )
            days.extend(
                LeaveDay(
                    employee_id=employee_id,
                    business_date=day,
                    leave_type_code=leave_type.code,
                    leave_type_id=leave_type.id,
                    request_id=request.id,
                )
                for day in inside
            )
        return sorted(days, key=lambda item: item.business_date)

    # --- internals: the calendar and the balance ----------------------------

    async def _working_days(
        self, employee_id: UUID, start_date: date, end_date: date
    ) -> tuple[date, ...]:
        """The days of a range somebody was due to work, from the schedule's answer.

        Refused when there are none: a request that costs nothing is either a range
        made entirely of weekends and holidays, or an employee no schedule reaches —
        and neither is a leave a balance can account for.
        """
        expectations = await self._expectations.day_expectations(
            employee_id, start_date, end_date
        )
        days = working_days(expectations.values())
        if not days:
            raise DomainError(
                LeaveErrorCode.REQUEST_INVALID,
                detail=(
                    f"{start_date}..{end_date} covers no working day for {employee_id}; "
                    "weekends, holidays and days no schedule expects are not leave"
                ),
            )
        return days

    async def _materialise(
        self, employee_id: UUID, year: int, leave_type: LeaveType
    ) -> LeaveBalance:
        """The year's row, created with the configured allowance if it is missing.

        The `grant` entry is written exactly once: `ensure_balance` reports whether
        this call is the one that created the row, so a concurrent request that lost
        the race does not write a second allowance into the history.
        """
        balance, created = await self._repository.ensure_balance(
            employee_id, year, leave_type.id, entitled_days=self._annual_leave_days
        )
        if created and balance.entitled_days > 0:
            await self._append(
                balance,
                LeaveEntryType.GRANT,
                balance.entitled_days,
                note=f"granted from annual_leave_days={self._annual_leave_days}",
            )
        return balance

    async def _require_affordable(
        self, employee_id: UUID, leave_type: LeaveType, counts: dict[int, int]
    ) -> None:
        """Refuse a request the year cannot cover, stating the remainder.

        Read-only, and deliberately so: this runs while the document is a draft, and a
        draft that reserved days would hold an allowance for a request nobody has
        filed. The authoritative check is `_reserve`, under the row's lock.
        """
        if not leave_type.counts_against_annual:
            return
        for year, days in sorted(counts.items()):
            balance = await self._repository.get_balance(employee_id, year, leave_type.id)
            remaining = self._annual_leave_days if balance is None else balance.remaining_days
            if days > remaining:
                raise DomainError(
                    LeaveErrorCode.BALANCE_INSUFFICIENT,
                    detail=_insufficient_detail(year, leave_type.code, days, remaining, balance),
                )

    async def _reserve(
        self, request: LeaveRequest, leave_type: LeaveType, counts: dict[int, int]
    ) -> None:
        """Hold the request's days as pending, one year's balance at a time.

        Each row is locked before it is read, so two submissions cannot both see the
        same three remaining days and both take them. The lock is released by the
        caller's commit, which is also what makes the reservation and the filing one
        event. A type that spends no allowance reserves nothing.
        """
        if not leave_type.counts_against_annual:
            return
        for year, days in sorted(counts.items()):
            balance = await self._materialise(request.employee_id, year, leave_type)
            balance = await self._repository.lock_balance(balance.id)
            if days > balance.remaining_days:
                raise DomainError(
                    LeaveErrorCode.BALANCE_INSUFFICIENT,
                    detail=_insufficient_detail(
                        year, leave_type.code, days, balance.remaining_days, balance
                    ),
                )
            written = await self._repository.write_balance(
                balance.id,
                entitled_days=balance.entitled_days,
                carried_over_days=balance.carried_over_days,
                used_days=balance.used_days,
                pending_days=balance.pending_days + days,
            )
            await self._append(
                written, LeaveEntryType.RESERVE, days, leave_request_id=request.id
            )

    async def _consume(self, request: LeaveRequest) -> None:
        """Spend what the request reserved: pending down, used up."""
        for balance_id, pending, _used in await self._holdings(request.id):
            if pending <= 0:
                continue
            balance = await self._repository.lock_balance(balance_id)
            written = await self._repository.write_balance(
                balance.id,
                entitled_days=balance.entitled_days,
                carried_over_days=balance.carried_over_days,
                used_days=balance.used_days + pending,
                pending_days=balance.pending_days - pending,
            )
            await self._append(
                written, LeaveEntryType.CONSUME, pending, leave_request_id=request.id
            )

    async def _release(self, request: LeaveRequest, *, refund: bool = False) -> None:
        """Give days back: `pending` for a request that never took effect, `used` for
        one that was approved and then withdrawn before it started."""
        for balance_id, pending, used in await self._holdings(request.id):
            owed = used if refund else pending
            if owed <= 0:
                continue
            balance = await self._repository.lock_balance(balance_id)
            written = await self._repository.write_balance(
                balance.id,
                entitled_days=balance.entitled_days,
                carried_over_days=balance.carried_over_days,
                used_days=balance.used_days - (owed if refund else 0),
                pending_days=balance.pending_days - (0 if refund else owed),
            )
            await self._append(
                written,
                LeaveEntryType.REFUND if refund else LeaveEntryType.RELEASE,
                owed,
                leave_request_id=request.id,
            )

    async def _holdings(self, request_id: UUID) -> list[tuple[UUID, int, int]]:
        """What each balance currently holds for this request, folded from the ledger.

        `(balance_id, pending, used)`. The ledger is the source rather than a column
        on the request, because the ledger is what an auditor reads and two answers to
        "how many days is this holding" would eventually differ.
        """
        entries = await self._repository.entries_for_request(request_id)
        holdings: dict[UUID, list[int]] = {}
        for entry in entries:
            pending, used = holdings.setdefault(entry.balance_id, [0, 0])
            if entry.entry_type is LeaveEntryType.RESERVE:
                holdings[entry.balance_id] = [pending + entry.days, used]
            elif entry.entry_type is LeaveEntryType.CONSUME:
                holdings[entry.balance_id] = [pending - entry.days, used + entry.days]
            elif entry.entry_type is LeaveEntryType.RELEASE:
                holdings[entry.balance_id] = [pending - entry.days, used]
            elif entry.entry_type is LeaveEntryType.REFUND:
                holdings[entry.balance_id] = [pending, used - entry.days]
        return [
            (balance_id, pending, used)
            for balance_id, (pending, used) in holdings.items()
            if pending or used
        ]

    async def _append(
        self,
        balance: LeaveBalance,
        entry_type: LeaveEntryType,
        days: int,
        *,
        leave_request_id: UUID | None = None,
        note: str | None = None,
        created_by_employee_id: UUID | None = None,
    ) -> None:
        """One ledger movement, with the totals that followed it. Zero writes nothing."""
        if days == 0:
            return
        await self._repository.append_entry(
            balance_id=balance.id,
            entry_type=entry_type,
            days=days,
            entitled_days=balance.entitled_days,
            carried_over_days=balance.carried_over_days,
            used_days=balance.used_days,
            pending_days=balance.pending_days,
            leave_request_id=leave_request_id,
            note=note,
            created_by_employee_id=created_by_employee_id,
        )

    async def _allocations(self, request_id: UUID) -> tuple[YearAllocation, ...]:
        """Which year's balance each part of a request was charged to.

        Read back from the ledger, so the split a reader sees is the split that was
        applied rather than one recomputed from a calendar that has since changed.
        """
        entries = await self._repository.entries_for_request(request_id)
        charged: dict[UUID, int] = {}
        for entry in entries:
            if entry.entry_type is LeaveEntryType.RESERVE:
                charged[entry.balance_id] = charged.get(entry.balance_id, 0) + entry.days
        years: list[YearAllocation] = []
        for balance_id, days in charged.items():
            balance = await self._repository.get_balance_by_id(balance_id)
            if balance is None:  # pragma: no cover - the foreign key forbids it
                continue
            years.append(YearAllocation(year=balance.year, days=days, balance_id=balance_id))
        return tuple(sorted(years, key=lambda item: item.year))

    # --- internals: reads and lookups ---------------------------------------

    async def _view(self, request: LeaveRequest) -> LeaveRequestView:
        approval = await self._approvals.state_of(ENTITY_TYPE, request.id)
        return LeaveRequestView(
            request=request,
            state=state_of_request(request, approval.status if approval is not None else None),
            leave_type=await self._require_type_by_id(request.leave_type_id),
            approval=approval,
            allocations=await self._allocations(request.id),
        )

    async def _state_of(self, request: LeaveRequest) -> LeaveRequestState:
        approval = await self._approvals.state_of(ENTITY_TYPE, request.id)
        return state_of_request(request, approval.status if approval is not None else None)

    async def _types_by_id(self) -> dict[UUID, LeaveType]:
        return {item.id: item for item in await self._repository.list_types(include_inactive=True)}

    async def _require(self, request_id: UUID) -> LeaveRequest:
        request = await self._repository.get_request(request_id)
        if request is None:
            raise DomainError(
                LeaveErrorCode.REQUEST_NOT_FOUND, detail=f"unknown leave request {request_id}"
            )
        return request

    async def _require_state(
        self, request: LeaveRequest, wanted: LeaveRequestState, act: str
    ) -> None:
        """Refuse an act the document's state does not admit.

        Read through `state_of_request` with the engine's answer, so a document that
        was approved or rejected while this module was not looking cannot be filed on
        the strength of its own row.
        """
        state = await self._state_of(request)
        if state is not wanted:
            raise DomainError(
                LeaveErrorCode.REQUEST_NOT_DRAFT,
                detail=f"leave request {request.id} is {state} and cannot be {act}",
            )

    async def _require_type(self, code: str) -> LeaveType:
        leave_type = await self._repository.get_type_by_code(code)
        if leave_type is None:
            raise DomainError(
                LeaveErrorCode.TYPE_NOT_FOUND, detail=f"unknown leave type {code!r}"
            )
        return leave_type

    async def _require_type_by_id(self, leave_type_id: UUID) -> LeaveType:
        leave_type = await self._repository.get_type(leave_type_id)
        if leave_type is None:  # pragma: no cover - the foreign key forbids it
            raise DomainError(
                LeaveErrorCode.TYPE_NOT_FOUND, detail=f"unknown leave type {leave_type_id}"
            )
        return leave_type

    async def _require_employee(self, employee_id: UUID) -> None:
        if not await self._repository.employee_exists(employee_id):
            raise DomainError(
                LeaveErrorCode.EMPLOYEE_NOT_FOUND, detail=f"unknown employee {employee_id}"
            )

    def _require_active(self, leave_type: LeaveType) -> None:
        if not leave_type.is_active:
            raise DomainError(
                LeaveErrorCode.TYPE_INACTIVE,
                detail=f"leave type {leave_type.code} is no longer offered",
            )

    def _require_window(self, start_date: date, end_date: date) -> None:
        if end_date < start_date:
            raise DomainError(
                LeaveErrorCode.REQUEST_INVALID,
                detail=f"a leave ends on or after it starts; got {start_date}..{end_date}",
            )
        if (end_date - start_date).days + 1 > MAX_REQUEST_DAYS:
            raise DomainError(
                LeaveErrorCode.REQUEST_INVALID,
                detail=f"a leave is at most {MAX_REQUEST_DAYS} days",
            )
        if end_date.year > self._now().year + MAX_YEARS_AHEAD:
            raise DomainError(
                LeaveErrorCode.REQUEST_INVALID,
                detail=f"{end_date} is more than {MAX_YEARS_AHEAD} years ahead",
            )

    def _require_attachment(self, leave_type: LeaveType, reference: str | None) -> None:
        """A type that asks for proof is refused without a usable reference to it.

        Two answers in one place: a type that requires an attachment and has none is
        refused, and a reference that is not the *shape* of a storage key is refused
        rather than stored. The second is what §8 needs — a diagnosis typed into this
        field would be a space and an accent away from the pattern — and the database
        states the same rule as a constraint, so neither layer alone is the guarantee.
        """
        if reference is None:
            if leave_type.requires_attachment:
                raise DomainError(
                    LeaveErrorCode.ATTACHMENT_REQUIRED,
                    detail=(
                        f"{leave_type.code} requires an attachment reference; store the "
                        "file and send the key it was stored under"
                    ),
                )
            return
        if not _ATTACHMENT_REFERENCE.match(reference):
            raise DomainError(
                LeaveErrorCode.REQUEST_INVALID,
                detail=(
                    "an attachment reference is a storage key — letters, digits, dot, "
                    "dash, slash and underscore — and not the content of the file"
                ),
            )


class LeaveCalendar:
    """The leave module as the attendance scan sees it: one question, one answer.

    The implementation of ticket 23's `LeaveLookup`, and a separate class rather than
    `LeaveService` itself so the seam is satisfied by something holding nothing but
    the repository: the scan asks "is this person on leave on this date" and cannot
    reach the rest of this module's surface through the object it was handed.

    One indexed query per question, asked once per person per date — the same shape as
    the day expectation the scan asks for beside it.
    """

    def __init__(self, repository: LeaveRepository) -> None:
        self._repository = repository

    async def is_on_leave(self, employee_id: UUID, business_date: date) -> bool:
        """Whether an approved, not-withdrawn leave covers this person's whole day.

        "Approved" is `approved_at`, which the settle sweep writes — so a leave the
        engine approved and a crash left unsettled reads as *not yet* leave. That gap
        is why the scan settles before it examines a day: see
        `jobs/scan_attendance_anomalies.py`.
        """
        covering = await self._repository.approved_requests_covering(
            employee_id, business_date, business_date
        )
        return any(request.covers(business_date) for request in covering)


#: The engine's statuses that mean "not decided yet": the days stay reserved.
_UNDECIDED = frozenset(
    {ApprovalStatus.DRAFT, ApprovalStatus.PENDING_FIRST, ApprovalStatus.PENDING_SECOND}
)


def _projected_balance(
    employee_id: UUID, year: int, leave_type: LeaveType, entitled: int
) -> LeaveBalance:
    """The row a year would get, for a read that must not create it."""
    return LeaveBalance(
        employee_id=employee_id,
        year=year,
        leave_type_id=leave_type.id,
        entitled_days=entitled,
        id=None,
    )


def _insufficient_detail(
    year: int, code: str, wanted: int, remaining: int, balance: LeaveBalance | None
) -> str:
    """The refusal the ticket asks for: what is left, and what was asked for.

    The four figures are named as well as the remainder, because "you have three days
    left" is a different conversation from "you have thirty entitled, two used and
    twenty-five already reserved".
    """
    if balance is None:
        return (
            f"{year} {code}: {wanted} working day(s) requested and no allowance is left "
            f"(remaining {remaining})"
        )
    return (
        f"{year} {code}: {wanted} working day(s) requested; remaining {remaining} "
        f"({balance.entitled_days} entitled + {balance.carried_over_days} carried over "
        f"- {balance.used_days} used - {balance.pending_days} pending)"
    )


def _dates_of(start_date: date, end_date: date) -> list[date]:
    """Every calendar date of an inclusive range, in order."""
    return [
        date.fromordinal(ordinal)
        for ordinal in range(start_date.toordinal(), end_date.toordinal() + 1)
    ]


def _validate_type(data: LeaveTypeInput) -> None:
    if not _TYPE_CODE.match(data.code):
        raise DomainError(
            LeaveErrorCode.TYPE_INVALID,
            detail=f"{data.code!r} is not a leave type code; they look like sick or annual",
        )
    if not data.name_es.strip() or not data.name_en.strip():
        raise DomainError(
            LeaveErrorCode.TYPE_INVALID, detail="a leave type is named in both languages"
        )


def _validate_year(year: int, now: datetime) -> None:
    if not 2000 <= year <= now.year + MAX_YEARS_AHEAD:
        raise DomainError(
            LeaveErrorCode.REQUEST_INVALID, detail=f"{year} is not a year this module holds"
        )


def _type_audit(leave_type: LeaveType) -> dict:
    return {
        "code": leave_type.code,
        "name_es": leave_type.name_es,
        "name_en": leave_type.name_en,
        "is_paid": leave_type.is_paid,
        "requires_attachment": leave_type.requires_attachment,
        "counts_against_annual": leave_type.counts_against_annual,
        "is_active": leave_type.is_active,
    }


def _balance_audit(balance: LeaveBalance) -> dict:
    return {
        "employee_id": str(balance.employee_id),
        "year": balance.year,
        "leave_type_id": str(balance.leave_type_id),
        "entitled_days": balance.entitled_days,
        "carried_over_days": balance.carried_over_days,
        "used_days": balance.used_days,
        "pending_days": balance.pending_days,
        "remaining_days": balance.remaining_days,
    }


def _settle_failure(request_id: UUID, error: Exception) -> SettleFailure:
    code = error.code.value if isinstance(error, DomainError) else ErrorCode.INTERNAL_ERROR.value
    return SettleFailure(request_id=request_id, code=code, detail=str(error))


__all__ = [
    "BALANCE_ENTITY",
    "MAX_REQUEST_DAYS",
    "MAX_YEARS_AHEAD",
    "REQUEST_ENTITY",
    "TYPE_ENTITY",
    "LeaveCalendar",
    "LeaveService",
]
