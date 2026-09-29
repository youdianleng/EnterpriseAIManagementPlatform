"""Apply approved personnel changes whose effective date has arrived.

    python -m app.jobs.apply_personnel_changes

Run it from cron, from a systemd timer, or from the worker container. One pass,
one exit code, and the same function behind the optional in-process runner
(`PERSONNEL_APPLY_RUNNER_ENABLED=true`), which exists only because a deployment
without a scheduler is common and a loop over this function is three lines —
whereas a scheduler nobody can turn off is a liability.

**Idempotent and catch-up capable**, which are the two properties the ticket asks
for and they are properties of the *domain* applier, not of this file:

* a change with an `applied_at` is never a candidate again, so running it twice
  applies nothing the second time;
* candidates are selected by `effective_date <= today` in effective-date order, so
  a week of downtime is a week of changes applied in the order they were meant to
  happen — a transfer that was effective on Monday is applied before the promotion
  that was effective on Wednesday.

Two workers may run at once: each takes its row with `FOR UPDATE SKIP LOCKED`.

The exit code is 0 even when a change could not be applied. A change that fails is
one whose data moved under it (a deleted department, an email taken in the
meantime); it stays unapplied, is logged here at error level, and is retried by
the next pass. Exiting non-zero would turn one unapplicable document into a
scheduler that reports failure every fifteen minutes forever, which is how a real
failure stops being noticed.

**It is also where HR hears about leavers who were somebody's approver.** A
termination applied by this pass disables a login, and if that person approved for
anybody, those routes are dead: a document filed against one is refused until HR
reassigns. That refusal names the people involved, and `approver_gaps` — printed
here on every pass — is the same list, so a gap nobody has tripped over yet still
reaches somebody. This is a pass, not a queue: nothing is written, and the sweep is
the query it looks like.
"""

import asyncio
import sys
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import RedisSessionRevoker
from app.config import get_settings
from app.db import dispose_engine, get_session_factory
from app.domain.approval.service import ApprovalService
from app.domain.employee.approver_gap import ApproverGap
from app.domain.employee.service import EmployeeService
from app.domain.personnel.models import ApplyReport
from app.domain.personnel.service import PersonnelChangeService
from app.logging import configure_logging, get_logger
from app.repositories.account import PostgresAccountRepository
from app.repositories.approval import PostgresApprovalRepository
from app.repositories.employee import PostgresEmployeeRepository
from app.repositories.org import PostgresDepartmentRepository
from app.repositories.payroll import PostgresSalaryRepository
from app.repositories.personnel import PostgresPersonnelChangeRepository

logger = get_logger(__name__)


def build_service(session: AsyncSession) -> PersonnelChangeService:
    """The module, wired to PostgreSQL and to the employee rules it drives.

    The employee service is non-transactional: applying one change writes an
    employee, an assignment and a termination as a single unit, so the transaction
    is the change's and not each write's.

    The account repository and the Redis revoker are passed here because a
    termination has a second half (ticket 18): applying one disables the login and
    ends its sessions in the same transaction. Neither commits by itself — the
    account repository never does, and the revoker writes to Redis, which is not
    part of this transaction and does not need to be.

    The salary archive is passed for the same kind of reason (ticket 43): a `salary`
    change's second half is the row it appends to `salary_records`, and a service built
    without the repository would apply the change and leave the archive empty —
    silently, on the one day it matters.
    """
    return PersonnelChangeService(
        repository=PostgresPersonnelChangeRepository(session),
        session=session,
        approvals=ApprovalService(PostgresApprovalRepository(session), session),
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
        salary=PostgresSalaryRepository(session),
    )


async def apply_due_changes(*, on_date: date | None = None) -> ApplyReport:
    """One pass: apply everything approved and due, and report what happened.

    `on_date` is for tests and for a backfill somebody runs deliberately; the
    command uses today, which is the server's day in Madrid terms because that is
    the day the effective dates are written in.
    """
    factory = get_session_factory()
    async with factory() as session:
        return await build_service(session).apply_due(on_date=on_date)


async def approver_gaps() -> list[ApproverGap]:
    """One pass's answer to "who now has nobody to approve their requests".

    Read after the changes are applied, on a fresh session, because a termination
    applied a moment ago is exactly what creates a gap. This is the visible half of
    the refusal in the personnel module: a submission whose route ends at a leaver
    is refused, and this is how HR finds out who to reassign before somebody hits
    that refusal — or, worse, before a document sits undecided because it was filed
    while the route still resolved.
    """
    factory = get_session_factory()
    async with factory() as session:
        return await build_service(session).approver_gaps()


async def run_forever(interval_seconds: int) -> None:
    """The optional in-process runner: one pass, sleep, repeat.

    A failure inside a pass never ends the loop — the next pass is the retry — and
    a failure *of* the pass (the database is down) is logged and waited out, which
    is what a scheduler would do with the same information.
    """
    while True:
        try:
            await apply_due_changes()
        except Exception as error:  # noqa: BLE001 - the loop is the retry
            logger.error("personnel_apply_pass_failed", error=str(error))
        await asyncio.sleep(interval_seconds)


async def main() -> int:
    configure_logging(get_settings())
    try:
        report = await apply_due_changes()
        logger.info(
            "personnel_changes_applied",
            applied=len(report.applied),
            failed=len(report.failed),
            examined=report.examined,
        )
        for failure in report.failed:
            logger.error(
                "personnel_change_not_applied",
                change_id=str(failure.change_id),
                code=failure.code,
                detail=failure.detail,
            )
        # A termination applied by this pass is what creates a gap, so the sweep
        # runs after it and on its own session. Logged at error level: nobody's
        # requests can be approved until somebody fixes it, and a report nobody
        # reads is the same as no report.
        for gap in await approver_gaps():
            logger.error(
                "approver_terminated",
                employee_id=str(gap.employee_id),
                employee=gap.employee_name,
                approver_employee_id=str(gap.approver_employee_id),
                approver=gap.approver_name,
                department=gap.department_code,
                named_on_position=gap.named_on_position,
            )
    finally:
        await dispose_engine()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
