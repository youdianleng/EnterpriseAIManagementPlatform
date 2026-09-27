"""Personnel change rules: 入转调离 as one document, five change types.

One rule shapes everything here: **approval is not application.** Nothing in this
module writes an employee record because a request was approved. A change that
has been approved sits in `approved_pending` until its effective date, and then
`apply_due` writes it — as a job, because the engine's job is to answer whether
something was approved and nothing else (`docs/DESIGN.md` §7.6,
`docs/architecture/codebase-design.md` §2.3).

The applier is the only place in the system where an approved document becomes
employee facts, and it works one change per transaction: every field change of
one document lands or none does. It is

* **idempotent** — `applied_at` is the guard, and a change that has one is never
  a candidate again, so a second run of the job applies nothing;
* **catch-up capable** — it selects by effective date, oldest first, so a week of
  downtime is a week of changes applied in the order they were meant to happen
  and an assignment chain (transfer, then promotion) still walks in order.

**Cancellation is this module's act, not the engine's.** `ApprovalService.withdraw`
belongs to the requester and stops working once HR holds the request, while
cancelling an *approved* change is a different act by a different authority, so it
is a local terminal state. The engine's request is deliberately left alone: it is
the record of the approval that had already happened, and an approval arriving
after the cancellation changes nothing because the applier reads the change's own
terminal flags first.

**A termination is the one change with a second half.** Applying it writes the
employee record and then finishes the account — disabled, epoch bumped, Redis
sessions revoked through `SessionRevoker` — because "disabled" that takes effect at
cookie expiry is not what the word means. Both halves are one transaction, nothing
historical is deleted, and the account row survives as a disabled identity rather
than being removed. Re-hiring is a separate act with its own document: a `join`,
which creates a new person, because the leaver's record and its ended assignments
are what the retention rules are talking about.

**A route that ends at a leaver is refused, not reassigned.** `submit` reads
`approver_coverage` before it asks the engine for a route, and the same rows are
what `approver_gaps` reports — the module docstring there carries the argument for
choosing a refusal over a fallback to the approver's own approver.

The account repository and the session revoker are **required** constructor
arguments rather than optional ones, and that is a decision about the failure mode:
a service built without them would apply a termination and leave a working login
behind the leaver, silently, on the one day it matters.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditAction, record
from app.core.errors import ErrorCode
from app.domain.access.kernel import apply_rls_context
from app.domain.access.principal import Principal
from app.domain.account.models import SessionRevoker
from app.domain.account.repository import AccountRepository
from app.domain.approval.models import ApprovalStatus, DecisionKind, SubmitContext
from app.domain.approval.service import ApprovalService
from app.domain.employee.models import (
    AssignmentInput,
    EmployeeInput,
    EmployeePatch,
    EmployeePrivate,
    EmploymentStatus,
    JobPosition,
)
from app.domain.employee.repository import EmployeeRepository
from app.domain.employee.service import EmployeeService
from app.domain.errors import DomainError
from app.domain.org.repository import DepartmentRepository
from app.domain.personnel.approver_coverage import ApproverGap, describe
from app.domain.personnel.errors import PersonnelErrorCode
from app.domain.personnel.models import (
    ApplyFailure,
    ApplyReport,
    ChangeInput,
    ChangeQuery,
    ChangeState,
    ChangeStatus,
    ChangeType,
    PersonnelChange,
    PersonnelChangeView,
    parse_changes,
    state_of_change,
)
from app.domain.personnel.repository import PersonnelChangeRepository

#: The entity type the approval engine files these under (DESIGN §3.4). The
#: engine stores it and never interprets it; this module is the only reader.
ENTITY_TYPE = "personnel_change"

#: What a salary change states when it names no currency. The company is Spanish
#: and every other amount in the system is in euros; a payload that means
#: something else says so.
DEFAULT_CURRENCY = "EUR"

#: The applier is not a person, so its published principal names no user. The
#: employee id it carries is the one on the change, which is what makes the write
#: *that person's* personnel action rather than an anonymous one.
SYSTEM_USER_ID = UUID(int=0)
APPLIER_NAME = "personnel-change-applier"

#: States in which a change may still be cancelled, because nothing has taken
#: effect yet. `draft` included: a change nobody has filed is the easiest one to
#: stop.
CANCELLABLE_STATES = frozenset(
    {ChangeState.DRAFT, ChangeState.IN_APPROVAL, ChangeState.APPROVED_PENDING}
)


@dataclass(frozen=True, slots=True)
class _Applied:
    """What one change wrote, and the employee a join created."""

    values: dict[str, Any]
    employee_id: UUID | None = None


class PersonnelChangeService:
    """The five operations the module offers, plus the applier the job calls."""

    def __init__(
        self,
        repository: PersonnelChangeRepository,
        session: AsyncSession,
        *,
        approvals: ApprovalService,
        employees: EmployeeService,
        directory: EmployeeRepository,
        departments: DepartmentRepository,
        accounts: AccountRepository,
        revoker: SessionRevoker,
    ) -> None:
        self._repository = repository
        self._session = session
        self._approvals = approvals
        # The employee module is where employee rules live, so the applier drives
        # it rather than writing `employees` and `employee_assignments` itself. It
        # is built non-transactional by this module's callers: the transaction
        # belongs to the change, not to each of the writes inside it.
        self._employees = employees
        self._directory = directory
        self._departments = departments
        # The account half of a termination (ticket 18). Required, not optional:
        # applying a termination without them would leave a working login behind a
        # leaver, and that is not a configuration this module should be able to be
        # built into — a missing argument is a `TypeError` at startup rather than a
        # quiet omission on the day somebody leaves.
        self._accounts = accounts
        self._revoker = revoker

    # --- reads -------------------------------------------------------------

    async def get(self, change_id: UUID) -> PersonnelChangeView:
        change = await self._require(change_id)
        approval = await self._approvals.state_of(ENTITY_TYPE, change.id)
        return PersonnelChangeView(
            change=change,
            state=state_of_change(change, approval.status if approval else None),
            approval=approval,
        )

    async def list(self, query: ChangeQuery) -> tuple[list[PersonnelChangeView], int]:
        """A page of changes and how many match, newest first.

        Each row carries its state and not its approval request: the list answers
        "what is in flight", and the request behind one change — its steps and the
        comments on them — is read one at a time, from the detail endpoint.
        """
        rows = await self._repository.list(query)
        total = await self._repository.count(query)
        return (
            [PersonnelChangeView(change=change, state=state) for change, state in rows],
            total,
        )

    # --- writes ------------------------------------------------------------

    async def create(
        self,
        *,
        change_type: ChangeType,
        effective_date: date,
        changes: object,
        created_by_employee_id: UUID,
        employee_id: UUID | None = None,
    ) -> PersonnelChangeView:
        """Write a draft, having refused anything it could never apply."""
        parsed = parse_changes(change_type, changes, effective_date=effective_date)
        values = {change.field: change.after for change in parsed}
        await self._validate(change_type, values, employee_id)

        change = await self._repository.save(
            ChangeInput(
                change_type=change_type,
                effective_date=effective_date,
                created_by_employee_id=created_by_employee_id,
                changes=parsed,
                employee_id=employee_id,
            )
        )
        await self._repository.commit()
        return await self.get(change.id)

    async def submit(self, change_id: UUID) -> PersonnelChangeView:
        """Hand a draft to the approval engine, as the person who drafted it.

        The requester is the change's author rather than whoever pressed the
        button: HR colleagues share the work, and the engine's rules — who
        approves first, and that nobody approves their own request — are about the
        person the route resolves for.

        A route that ends at somebody who has left is refused here, before the
        engine is asked. This is the one moment the document is still the
        requester's to correct, and the refusal names the people HR has to
        reassign — see `approver_coverage` for why falling back to the approver's
        own approver was rejected instead.
        """
        change = await self._require(change_id)
        state = await self._state_of(change)
        if state is not ChangeState.DRAFT:
            raise DomainError(
                PersonnelErrorCode.PERSONNEL_CHANGE_NOT_DRAFT,
                detail=f"change {change_id} is {state}",
            )
        await self._require_live_approvers()

        request_id = await self._approvals.submit(
            ENTITY_TYPE, change.id, change.created_by_employee_id, SubmitContext()
        )
        await self._repository.mark_submitted(
            change.id, request_id=request_id, status=ChangeStatus.PENDING
        )
        await self._repository.commit()
        return await self.get(change.id)

    async def approver_gaps(self) -> Sequence[ApproverGap]:
        """Every active employee whose first-level approver has left.

        HR's half of the refusal above: "the system said no and nobody knows who
        to fix" is not an acceptable outcome, so the same rows that refuse a
        submission are readable on their own — the applier job prints them on
        every pass, which is how a gap nobody tripped over still reaches somebody.
        """
        return await self._directory.terminated_approvers()

    async def _require_live_approvers(self) -> None:
        """Refuse a document whose first level can never be decided.

        Raised for the whole company's gaps rather than for this change's own
        route, because the fix is the same act and HR needs the complete list to
        do it once. A company with no gaps — the ordinary case — pays one indexed
        query per submission.
        """
        gaps = await self.approver_gaps()
        if not gaps:
            return
        raise DomainError(
            PersonnelErrorCode.PERSONNEL_APPROVER_TERMINATED,
            detail=(
                f"{len(gaps)} employee(s) have a terminated approver: {describe(gaps)}"
            ),
        )

    async def decide(
        self,
        change_id: UUID,
        *,
        approver_employee_id: UUID,
        decision: DecisionKind,
        comment: str | None = None,
        actor_user_id: UUID | None = None,
        actor_roles: frozenset[str] | None = None,
    ) -> PersonnelChangeView:
        """Record a decision at the change's current level.

        The engine answers whether this person may decide at all — first-level
        approver, or any HR member other than the requester at the second — so this
        method does not test a role. It finds the request behind the change and
        hands it over, and the notifications the decision owes are raised by the
        notifier the engine is wrapped in.
        """
        change = await self._require(change_id)
        approval = await self._approvals.state_of(ENTITY_TYPE, change.id)
        if approval is None:
            raise DomainError(
                PersonnelErrorCode.PERSONNEL_CHANGE_NOT_DRAFT,
                detail=f"change {change_id} has not been filed",
            )

        await self._approvals.decide(
            approval.id,
            approver_employee_id,
            decision,
            comment,
            actor_user_id=actor_user_id,
            actor_roles=actor_roles,
        )
        await self._repository.commit()
        return await self.get(change.id)

    async def cancel(
        self,
        change_id: UUID,
        *,
        reason: str,
        actor_employee_id: UUID,
        actor_user_id: UUID | None = None,
        actor_roles: frozenset[str] | None = None,
    ) -> PersonnelChangeView:
        """Stop a change that has not taken effect, and say who and why.

        The effective date does not enter into it: "before the effective date" and
        "not yet applied" are the same statement, and the second is the one the
        data can answer. An applied change is refused with the alternative named
        — a counter-change — because undoing one by deletion would rewrite a
        personnel record that other records already refer to.
        """
        if not reason or not reason.strip():
            raise DomainError(
                PersonnelErrorCode.INVALID_REQUEST,
                detail="cancelling a change states why",
            )

        change = await self._require(change_id)
        state = await self._state_of(change)
        if state is ChangeState.APPLIED:
            raise DomainError(
                PersonnelErrorCode.PERSONNEL_CHANGE_ALREADY_APPLIED,
                detail=f"change {change_id} took effect on {change.effective_date}",
            )
        if state not in CANCELLABLE_STATES:
            raise DomainError(
                PersonnelErrorCode.PERSONNEL_CHANGE_NOT_CANCELLABLE,
                detail=f"change {change_id} is {state}",
            )

        at = datetime.now(UTC)
        await self._repository.mark_cancelled(
            change.id, at=at, by_employee_id=actor_employee_id, reason=reason.strip()
        )
        await record(
            self._session,
            action=AuditAction.PERSONNEL_CHANGE_CANCELLED,
            entity_type=ENTITY_TYPE,
            entity_id=change.id,
            actor_user_id=actor_user_id,
            actor_roles=actor_roles,
            before={"state": state.value},
            after={
                "state": ChangeState.CANCELLED.value,
                "employee_id": str(change.employee_id) if change.employee_id else None,
                "effective_date": change.effective_date,
                "cancelled_by_employee_id": str(actor_employee_id),
            },
            reason=reason.strip(),
        )
        await self._repository.commit()
        return await self.get(change.id)

    # --- the applier -------------------------------------------------------

    async def apply_due(self, *, on_date: date | None = None) -> ApplyReport:
        """Apply every approved change whose effective date has arrived.

        The lock is taken before the row is read, `SKIP LOCKED` so a second worker
        takes a different change instead of the same one, and it is released by the
        commit that ends each change — one document, one transaction, so a failure
        midway through a change leaves the employee exactly as it was.

        A change that cannot be applied is reported, not raised: it stays
        unapplied, so the next run tries again, and the ones behind it in the
        queue are not held up by it.
        """
        today = on_date or date.today()
        applied: list[UUID] = []
        failed: list[ApplyFailure] = []
        examined: set[UUID] = set()

        while True:
            change = await self._repository.lock_next_due(
                on_date=today, exclude=frozenset(examined)
            )
            if change is None:
                break
            examined.add(change.id)

            approval = await self._approvals.state_of(ENTITY_TYPE, change.id)
            if approval is None or approval.status is not ApprovalStatus.APPROVED:
                # Due, and not approved: the lock goes back with the commit, and
                # the row is not a candidate for this run again either way.
                await self._repository.commit()
                continue

            try:
                # Before the first write of this change, and inside its
                # transaction: `set_config(..., is_local => true)` only lasts until
                # the commit below, so every change publishes its own authority.
                await self._publish_authority(change)
                result = await self._apply(change)
                await self._repository.mark_applied(
                    change.id,
                    at=datetime.now(UTC),
                    applied_values=result.values,
                    employee_id=result.employee_id,
                )
                await self._audit_applied(change, result.values)
                await self._repository.commit()
            except Exception as error:
                await self._repository.rollback()
                failed.append(_failure(change.id, error))
                continue
            applied.append(change.id)

        return ApplyReport(applied=tuple(applied), failed=tuple(failed), examined=len(examined))

    async def _apply(self, change: PersonnelChange) -> _Applied:
        """Write one change's fields, all of them, in the caller's transaction."""
        if change.change_type is ChangeType.JOIN:
            return await self._apply_join(change)
        if change.change_type in (ChangeType.TRANSFER, ChangeType.PROMOTION):
            return await self._apply_move(change)
        if change.change_type is ChangeType.SALARY:
            return self._apply_salary(change)
        return await self._apply_termination(change)

    async def _apply_join(self, change: PersonnelChange) -> _Applied:
        """Create the employee and their first assignment.

        The employee row is written here and not when the draft was: a hire that
        appears in the directory three weeks before it was agreed is exactly the
        leak approval-ahead-of-time is supposed to avoid.
        """
        values = change.values
        created = await self._employees.create(
            EmployeeInput(
                first_name=values["first_name"],
                last_name=values["last_name"],
                email=values["email"],
                hire_date=values["hire_date"],
                preferred_name=values.get("preferred_name"),
                private=EmployeePrivate(employee_no=values.get("employee_no")),
            )
        )
        employee = created.employee
        assigned = await self._employees.assign_position(
            employee.id,
            AssignmentInput(
                department_id=values["department_id"],
                job_position_id=values["job_position_id"],
                start_date=values["hire_date"],
                is_part_time=values.get("is_part_time", False),
                manager_employee_id=values.get("manager_employee_id"),
            ),
        )
        assignment = assigned.primary_assignment
        return _Applied(
            values={
                "employee_id": str(employee.id),
                "assignment_id": str(assignment.id) if assignment else None,
                "department_id": str(values["department_id"]),
                "job_position_id": str(values["job_position_id"]),
            },
            employee_id=employee.id,
        )

    async def _apply_move(self, change: PersonnelChange) -> _Applied:
        """A transfer or a promotion: one assignment ends, another begins.

        The new assignment is added **before** the old one ends, because an
        employee must keep an active position at every moment and the employee
        rules refuse to end the only one. Which position is replaced is the
        primary one at the moment of application: changes are applied in effective
        date order, so a queue of moves walks the chain in the order it was
        written.
        """
        values = change.values
        employee_id = _subject_of(change)
        before = await self._employees.get_record(employee_id)
        outgoing = before.primary_assignment
        if outgoing is None:
            raise DomainError(
                PersonnelErrorCode.PERSONNEL_CHANGE_APPLY_FAILED,
                detail=f"employee {employee_id} has no active position to move from",
            )

        updated = await self._employees.assign_position(
            employee_id,
            AssignmentInput(
                department_id=values.get("department_id", outgoing.department_id),
                job_position_id=values["job_position_id"],
                start_date=change.effective_date,
                is_part_time=values.get("is_part_time", outgoing.is_part_time),
                manager_employee_id=values.get("manager_employee_id"),
            ),
        )
        # The one assignment that was not there before. Read rather than returned,
        # because the employee rules answer with the whole record — and they are
        # the only thing that should be creating assignments.
        known = {assignment.id for assignment in before.assignments}
        incoming = next(
            assignment for assignment in updated.assignments if assignment.id not in known
        )

        await self._employees.end_assignment(
            employee_id,
            outgoing.id,
            on_date=_last_day_before(change.effective_date, outgoing.start_date),
        )
        # Explicitly, rather than trusting the promotion the rule above performs:
        # that one picks the earliest remaining position, which on a second
        # assignment is not necessarily the one this change moved somebody into.
        await self._employees.set_primary(employee_id, incoming.id)
        return _Applied(
            values={
                "assignment_id": str(incoming.id),
                "ended_assignment_id": str(outgoing.id),
                "department_id": str(incoming.department_id),
                "job_position_id": str(incoming.job_position_id),
            }
        )

    def _apply_salary(self, change: PersonnelChange) -> _Applied:
        """Record the agreed figure — in this change's own record.

        There is no salary table yet: `salary_records` arrives with ticket 43, and
        inventing one here would put a second, unofficial payroll record next to
        the official one that is coming. So the agreed values are applied to the
        change's own record — its payload states them and `applied_values` stamps
        what took effect — and the audit carries the before/after pair. Ticket 43
        writes the `salary_records` row from the same payload; nothing else has to
        change when it does.
        """
        values = change.values
        return _Applied(
            values={
                "base_salary": f"{values['base_salary']:.2f}",
                "currency": values.get("currency", DEFAULT_CURRENCY),
            }
        )

    async def _apply_termination(self, change: PersonnelChange) -> _Applied:
        """Write the termination, then finish the account.

        Two halves, one transaction, and the order matters: the record is written
        first and the login finishes after it, so a failure anywhere leaves both
        the employee and their account as they were — the rollback takes the
        epoch bump with it, and no session is lost over a change that never took
        effect.

        **Nothing historical is touched.** Attendance, timesheets, leave, salary
        and approvals do not exist yet (tickets 21–47); what exists is the employee
        row, its assignments including the ended ones, the personnel changes,
        notifications and the audit trail, and this method deletes none of them.
        The account row is *disabled*, never removed, so the login identity the
        audit trail refers to survives.
        """
        values = change.values
        employee_id = _subject_of(change)
        await self._employees.update(
            employee_id,
            EmployeePatch(
                termination_date=values["termination_date"],
                status=EmploymentStatus(values["status"]),
            ),
        )
        applied = {
            "termination_date": values["termination_date"].isoformat(),
            "status": values["status"],
        }
        applied.update(await self._finish_account(change, employee_id))
        return _Applied(values=applied)

    async def _finish_account(
        self, change: PersonnelChange, employee_id: UUID
    ) -> dict[str, Any]:
        """Disable the login, end every session, and say so in the trail.

        Drives the account repository rather than `AccountService`, because the
        account service commits: this write belongs to the change's transaction,
        and a service that ended it would leave a disabled login behind a
        termination that then failed to apply.

        A leaver with no account is a normal outcome, not a failure: not everybody
        has a login. What the audit record carries either way is the account id the
        trail needs — absent when there was none, so "nobody had a login" and "we
        forgot to disable it" cannot look alike.

        `is_active` is the guard against a second, manual disable: disabling an
        already-disabled account is a conflict, and the epoch and the revocation
        are then already done.
        """
        account = await self._accounts.get_by_employee(employee_id)
        if account is None:
            return {"account_id": None, "account_disabled": False}
        if not account.is_active:
            return {"account_id": str(account.id), "account_disabled": False}

        await self._accounts.set_active(account.id, is_active=False)
        # Order, and it is the account service's: the epoch is bumped in the
        # database first, because that is the value every future session check
        # reads, and only then is Redis told. A revocation that runs ahead of a
        # rolled-back bump is harmless — the database is what `resolve_session`
        # compares against — while the reverse would leave live sessions behind a
        # disabled account.
        epoch = await self._accounts.bump_session_epoch(account.id)
        await self._revoker.revoke_all(account.id, epoch=epoch)
        await record(
            self._session,
            action=AuditAction.ACCOUNT_DEACTIVATED,
            entity_type="user",
            entity_id=account.id,
            initiated_by="system",
            before={"is_active": True},
            after={
                "is_active": False,
                "session_epoch": epoch,
                "sessions_revoked": True,
                "employee_id": str(employee_id),
                "personnel_change_id": str(change.id),
            },
            reason=f"termination effective {change.effective_date}",
        )
        return {"account_id": str(account.id), "account_disabled": True}

    async def _publish_authority(self, change: PersonnelChange) -> None:
        """Say who this change is being applied as, for the database's policies.

        Row-level security on `employee_private` asks which roles are writing a
        staff number, and a background job has no session to read that from. The
        honest answer is personnel work: the request was approved by HR, and the
        person who filed it is on the change. So the applier publishes their
        employee id with the `hr` role for the length of this change's transaction,
        and a job that somehow reached this code without it would be refused by
        PostgreSQL rather than quietly trusted.
        """
        await apply_rls_context(
            self._session,
            Principal(
                user_id=SYSTEM_USER_ID,
                employee_id=change.created_by_employee_id,
                username=APPLIER_NAME,
                roles=frozenset({"hr"}),
            ),
        )

    async def _audit_applied(
        self, change: PersonnelChange, applied_values: dict[str, Any]
    ) -> None:
        """One record per applied change, in the same transaction as the write.

        The before/after pair is the whole point: the payload states what was
        agreed and this states what was read and written when it took effect, and
        the two can legitimately differ — a field the payload left empty keeps the
        value it had.

        `initiated_by="system"`, because nobody pressed anything: the change's own
        record says who filed it and the engine's decisions say who approved it.
        """
        await record(
            self._session,
            action=AuditAction.PERSONNEL_CHANGE_APPLIED,
            entity_type=ENTITY_TYPE,
            entity_id=change.id,
            initiated_by="system",
            before=change.before_values,
            after={**change.after_values, **applied_values},
            reason=f"{change.change_type} effective {change.effective_date}",
        )

    # --- internals ---------------------------------------------------------

    async def _require(self, change_id: UUID) -> PersonnelChange:
        change = await self._repository.get(change_id)
        if change is None:
            raise DomainError(
                PersonnelErrorCode.PERSONNEL_CHANGE_NOT_FOUND,
                detail=f"unknown personnel change {change_id}",
            )
        return change

    async def _state_of(self, change: PersonnelChange) -> ChangeState:
        approval = await self._approvals.state_of(ENTITY_TYPE, change.id)
        return state_of_change(change, approval.status if approval else None)

    async def _validate(
        self,
        change_type: ChangeType,
        values: dict[str, Any],
        employee_id: UUID | None,
    ) -> None:
        """Refuse a change that could never be applied, while it is still a draft.

        Reference checks live here rather than at application time for the reason
        the approval engine checks its second approver at submission: a document
        that fails on its effective date has been wrong for three weeks by the
        time anybody finds out.
        """
        if change_type is ChangeType.JOIN:
            if employee_id is not None:
                raise _refused("a join creates the employee; it does not name one")
            await self._require_values_free(values)
            await self._require_targets(values["department_id"], values["job_position_id"])
            await self._require_manager(values.get("manager_employee_id"), employee_id=None)
            return

        if employee_id is None:
            raise DomainError(
                PersonnelErrorCode.PERSONNEL_CHANGE_EMPLOYEE_REQUIRED,
                detail=f"a {change_type} names the employee it is about",
            )
        if change_type is ChangeType.TERMINATION:
            # Nothing to look up: the record it changes is the employee's own, and
            # the employee's existence is what naming them checked.
            await self._require_employee(employee_id)
            return

        await self._require_employee(employee_id)
        if change_type is ChangeType.TRANSFER:
            await self._require_targets(values["department_id"], values["job_position_id"])
            await self._require_manager(values.get("manager_employee_id"), employee_id)
        elif change_type is ChangeType.PROMOTION:
            current = await self._employees.get_record(employee_id)
            primary = current.primary_assignment
            if primary is None:
                raise _refused(f"employee {employee_id} has no active position to promote from")
            position = await self._require_targets(None, values["job_position_id"])
            if position.department_id != primary.department_id:
                raise _refused(
                    f"a promotion keeps the department: {position.code} is not in "
                    f"{primary.department_code}, which is a transfer"
                )
            await self._require_manager(values.get("manager_employee_id"), employee_id)
        # A salary is a figure and nothing else: there is no record to check
        # against until ticket 43 adds one, which is why `before` is the caller's.

    async def _require_employee(self, employee_id: UUID) -> None:
        if await self._directory.get(employee_id) is None:
            raise DomainError(
                PersonnelErrorCode.EMPLOYEE_NOT_FOUND, detail=f"unknown employee {employee_id}"
            )

    async def _require_values_free(self, values: dict[str, Any]) -> None:
        """A join's email and staff number have to be free when it is drafted.

        Checked now because the alternative is discovering it on the effective
        date, with the hire already announced.
        """
        if await self._directory.get_by_email(values["email"]) is not None:
            raise _refused(f"email {values['email']} is already in use")
        employee_no = values.get("employee_no")
        if employee_no and await self._directory.get_by_employee_no(employee_no) is not None:
            raise _refused(f"staff number {employee_no} is already in use")

    async def _require_targets(
        self, department_id: UUID | None, position_id: UUID
    ) -> JobPosition:
        position = await self._directory.get_position(position_id)
        if position is None or not position.is_active:
            raise _refused(f"position {position_id} does not exist or is not active")
        if department_id is None:
            return position
        if await self._departments.get(department_id) is None:
            raise _refused(f"department {department_id} does not exist")
        if position.department_id != department_id:
            # A position belongs to a department, so the two halves of a move have
            # to agree; sending a mismatched pair is a mistake, not a variation.
            raise _refused(
                f"position {position.code} belongs to another department than {department_id}"
            )
        return position

    async def _require_manager(self, manager_id: UUID | None, employee_id: UUID | None) -> None:
        if manager_id is None:
            return
        if manager_id == employee_id or await self._directory.get(manager_id) is None:
            raise _refused(f"manager {manager_id} does not exist or is the employee themselves")


def _subject_of(change: PersonnelChange) -> UUID:
    """The employee a change is about. Only a join may not have one yet."""
    if change.employee_id is None:
        raise DomainError(
            PersonnelErrorCode.PERSONNEL_CHANGE_APPLY_FAILED,
            detail=f"change {change.id} names no employee",
        )
    return change.employee_id


def _last_day_before(effective_date: date, started_on: date) -> date:
    """The outgoing assignment's last day: the day before the move takes effect.

    Ending it on the effective date itself would leave two positions active on the
    same day, which every date-parameterised read in the employee directory would
    then report as one person in two departments.
    """
    return max(started_on, effective_date - timedelta(days=1))


def _failure(change_id: UUID, error: Exception) -> ApplyFailure:
    """What went wrong, as a catalogued code where there is one.

    A change that cannot be applied is a data problem somebody has to fix, so it
    is reported with the same vocabulary a refused request would carry.
    """
    code = error.code.value if isinstance(error, DomainError) else ErrorCode.INTERNAL_ERROR.value
    return ApplyFailure(change_id=change_id, code=code, detail=str(error))


def _refused(detail: str) -> DomainError:
    return DomainError(PersonnelErrorCode.PERSONNEL_CHANGE_INVALID_PAYLOAD, detail=detail)


__all__ = [
    "APPLIER_NAME",
    "CANCELLABLE_STATES",
    "DEFAULT_CURRENCY",
    "ENTITY_TYPE",
    "SYSTEM_USER_ID",
    "PersonnelChangeService",
]
