"""PostgreSQL implementation of the payslip tables.

Five things here are load-bearing, and each is a decision rather than a query:

* **`upsert` is `INSERT … ON CONFLICT DO UPDATE`, and the conflict target is the ticket's
  replacement rule.** `uq_payslips_employee_period` is `(employee_id, period)`, so a
  re-upload lands on the row that is already there and rewrites the four columns that
  describe the file — its key, its size, its checksum and the name it came in under —
  together with the batch that brought it. Doing it as a `SELECT` followed by an `INSERT`
  or an `UPDATE` would be a race between two uploads of one month, and the loser's row
  would be the one the employee is handed. What is *not* rewritten is `created_at` (the
  row dates from the first upload) and `download_count` (ticket 45's audit of handed-over
  copies: it counts downloads of that payslip slot, and resetting it on a replacement
  would erase the record of files that were already in somebody's mailbox).

* **`existing_by_period` is a read, and it is a read for a reason that is not obvious.**
  The batch's answer has to state *what was replaced*, which is a fact about the rows as
  they were before the write. Reading them first and then upserting would be a race; the
  answer the uploader needs is "was there a payslip for this person and month when I
  pressed upload", and that is exactly what a read at the start of the transaction says.
  `previous_sha256` travelling in the answer is what makes a replacement checkable
  afterwards rather than merely announced — the ticket's 「事后核对」.

* **`candidates` derives "was this person on the books in that month" in SQL, from the
  dates and not from `status`.** `hire_date <= month_end AND (termination_date IS NULL OR
  termination_date >= month_start)` is the same inclusive-both-ends rule the salary
  archive's windows use (`domain/payroll/windows.py`), and it is asked of the *dates*
  because they are what a payroll month is about: somebody who left on the 20th was on the
  books that month and is owed a payslip for it, and somebody who left last year is not.
  `status = 'terminated'` alone would answer the second case and get the first one wrong.
  Note what this does *not* do: it does not decide whether a salary was in force. That is
  the archive's question, and the service asks it through `SalaryService.read(...)` — the
  one path that serves those rows and the one that writes the trail entry for looking.

* **`employee_refs` reads `employee_private.employee_no`, and the row policy is why that is
  safe.** The staff number is a withheld field; it reaches this module because the only
  caller is `finance`, whose rows the policy admits. The department is a correlated
  subquery rather than a join, so a person with two open primary assignments appears once
  with the most recently started one — a join would duplicate the row and make the missing
  list state the same person twice.

* **Nothing commits except `commit`.** The service commits once per operation, so the
  batch row, the payslip rows, the notifications and the audit entries land together or
  not at all. A batch that committed without its payslips would be a month finance
  believes it filed.

The reach is not consulted here. This module's reads are finance-only at the kernel
(`payslip.manage` and `payslip.export`), the rows it serves are the whole company's, and
the database's own policies are the second line: a caller without `finance` in
`app.current_roles` sees nothing at all, which `tests/test_payslips.py` exercises through
the restricted role.
"""

from datetime import date
from uuid import UUID, uuid4

from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.payslip.models import (
    BatchPage,
    BatchRecord,
    EmployeeRef,
    Payslip,
    PayslipStatus,
    UnmatchedFile,
    UnmatchedReason,
)
from app.models.employee import Employee as EmployeeRow
from app.models.employee import EmployeeAssignment as AssignmentRow
from app.models.employee import EmployeePrivate as PrivateRow
from app.models.org import Department as DepartmentRow
from app.models.payslip import Payslip as PayslipRow
from app.models.payslip import PayslipBatch as BatchRow

#: The department an employee is principally in, as a scalar subquery.
#:
#: A scalar rather than an outer join: a person may have two open primary assignments
#: (nothing in the schema forbids it), and a join would return two rows for one person —
#: which the missing list would then state as two missing employees. The subquery picks the
#: most recently started one, so the answer is deterministic and a person appears once.
_PRIMARY_DEPARTMENT = (
    select(DepartmentRow.name_es)
    .select_from(AssignmentRow)
    .join(DepartmentRow, DepartmentRow.id == AssignmentRow.department_id)
    .where(
        AssignmentRow.employee_id == EmployeeRow.id,
        AssignmentRow.is_primary.is_(True),
        AssignmentRow.end_date.is_(None),
    )
    .order_by(AssignmentRow.start_date.desc(), AssignmentRow.id.desc())
    .limit(1)
    .correlate(EmployeeRow)
    .scalar_subquery()
)


def _to_payslip(row: PayslipRow) -> Payslip:
    return Payslip(
        id=row.id,
        employee_id=row.employee_id,
        period=row.period,
        storage_path=row.storage_path,
        file_size=row.file_size,
        content_sha256=row.content_sha256,
        original_filename=row.original_filename,
        uploaded_by_user_id=row.uploaded_by_user_id,
        batch_id=row.batch_id,
        status=row.status,
        download_count=row.download_count,
        withdrawn_at=row.withdrawn_at,
        withdraw_reason=row.withdraw_reason,
        created_at=row.created_at,
    )


def _to_unmatched(entry: object) -> UnmatchedFile:
    """One stored `{filename, reason}` object as the module's value.

    Tolerant of a row somebody edited by hand — an unknown reason token degrades to
    `NO_EMPLOYEE_NUMBER` rather than raising, for the reason
    `domain/notification`'s title keys degrade: a listing that cannot render one bad row
    should not fail the month.
    """
    if not isinstance(entry, dict):  # pragma: no cover - the CHECK refuses anything else
        return UnmatchedFile(filename="", reason=UnmatchedReason.NO_EMPLOYEE_NUMBER)
    try:
        reason = UnmatchedReason(str(entry.get("reason")))
    except ValueError:  # pragma: no cover - a token no build writes
        reason = UnmatchedReason.NO_EMPLOYEE_NUMBER
    return UnmatchedFile(
        filename=str(entry.get("filename", "")),
        reason=reason,
        employee_no=(
            str(entry["employee_no"]) if entry.get("employee_no") is not None else None
        ),
        detail=str(entry["detail"]) if entry.get("detail") is not None else None,
    )


def _to_batch(row: BatchRow) -> BatchRecord:
    return BatchRecord(
        id=row.id,
        period=row.period,
        uploaded_by_user_id=row.uploaded_by_user_id,
        total_count=row.total_count,
        success_count=row.success_count,
        missing_employee_ids=tuple(UUID(str(value)) for value in (row.missing_employee_ids or [])),
        unmatched=tuple(_to_unmatched(entry) for entry in (row.unmatched or [])),
        created_at=row.created_at,
    )


class PostgresPayslipRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- the month's people -------------------------------------------------

    async def candidates(self, period_start: date, period_end: date) -> list[UUID]:
        """Who was on the books during this month, oldest hire first.

        See the module docstring for why this is asked of the dates rather than of
        `status`, and for what it deliberately does not answer. Ordered by `hire_date`
        then id so a run is reproducible and the audit entries a derivation writes follow
        one order.
        """
        rows = await self._session.scalars(
            select(EmployeeRow.id)
            .where(
                EmployeeRow.hire_date <= period_end,
                (EmployeeRow.termination_date.is_(None))
                | (EmployeeRow.termination_date >= period_start),
            )
            .order_by(EmployeeRow.hire_date, EmployeeRow.id)
        )
        return list(rows)

    async def employee_refs(self, employee_ids: list[UUID]) -> list[EmployeeRef]:
        """The staff number, the name and the department for these people, in id order.

        One query for the whole page, because the missing list needs all of them and a
        query per person would be a query per person for facts that are not per-person
        decisions.
        """
        if not employee_ids:
            return []
        rows = (
            await self._session.execute(
                select(
                    EmployeeRow.id,
                    PrivateRow.employee_no,
                    EmployeeRow.last_name,
                    EmployeeRow.first_name,
                    _PRIMARY_DEPARTMENT,
                )
                .outerjoin(PrivateRow, PrivateRow.employee_id == EmployeeRow.id)
                .where(EmployeeRow.id.in_(employee_ids))
                .order_by(EmployeeRow.last_name, EmployeeRow.first_name, EmployeeRow.id)
            )
        ).all()
        return [
            EmployeeRef(
                employee_id=row[0],
                employee_no=row[1],
                employee_name=f"{row[2]}, {row[3]}",
                department_name=row[4],
            )
            for row in rows
        ]

    async def employee_ref(self, employee_id: UUID) -> EmployeeRef | None:
        """One person, when they exist. `None` for an id that names nobody."""
        refs = await self.employee_refs([employee_id])
        return refs[0] if refs else None

    async def employee_exists(self, employee_id: UUID) -> bool:
        return bool(
            await self._session.scalar(
                select(func.count()).select_from(EmployeeRow).where(EmployeeRow.id == employee_id)
            )
        )

    # --- the month's payslips ----------------------------------------------

    async def published_employee_ids(self, period: str) -> set[UUID]:
        """Who already has a *published* payslip for this month.

        `published` and not "any row": a withdrawn payslip is one the employee cannot see
        (§7.5), so somebody whose payslip was withdrawn is still missing one — which is
        what makes the missing list useful again after a withdrawal, and the rule
        `tests/test_payslips.py` pins by withdrawing a row and watching the person
        reappear.
        """
        rows = await self._session.scalars(
            select(PayslipRow.employee_id).where(
                PayslipRow.period == period,
                PayslipRow.status == PayslipStatus.PUBLISHED,
            )
        )
        return set(rows)

    async def existing_by_period(
        self, period: str, employee_ids: list[UUID]
    ) -> dict[UUID, Payslip]:
        """The payslips already filed for these people and this month, whatever their status.

        A read taken *before* the upsert, because the batch's answer states what it
        replaced and the previous checksum is what makes that checkable afterwards.
        """
        if not employee_ids:
            return {}
        rows = await self._session.scalars(
            select(PayslipRow).where(
                PayslipRow.period == period,
                PayslipRow.employee_id.in_(employee_ids),
            )
        )
        return {row.employee_id: _to_payslip(row) for row in rows}

    async def all_employee_ids(self, period: str) -> set[UUID]:
        """Every employee with a row for this month, published or not."""
        rows = await self._session.scalars(
            select(PayslipRow.employee_id).where(PayslipRow.period == period)
        )
        return set(rows)

    async def upsert(
        self,
        *,
        employee_id: UUID,
        period: str,
        storage_path: str,
        file_size: int,
        content_sha256: str,
        original_filename: str,
        uploaded_by_user_id: UUID,
        batch_id: UUID,
    ) -> Payslip:
        """Store this employee's payslip for this month, replacing one if it is there.

        See the module docstring: the conflict target is the uniqueness rule, the four
        file columns and the batch move, and `created_at` and `download_count` do not.
        The status is written back to `published` because a row that was withdrawn and is
        then replaced is a current file again — and the withdrawal columns are cleared with
        it, which the table's CHECK requires to be an all-or-nothing pair.
        """
        statement = (
            pg_insert(PayslipRow)
            .values(
                id=uuid4(),
                employee_id=employee_id,
                period=period,
                storage_path=storage_path,
                file_size=file_size,
                content_sha256=content_sha256,
                original_filename=original_filename,
                uploaded_by_user_id=uploaded_by_user_id,
                batch_id=batch_id,
                status=PayslipStatus.PUBLISHED,
                withdrawn_at=None,
                withdraw_reason=None,
                download_count=0,
            )
            .on_conflict_do_update(
                constraint="uq_payslips_employee_period",
                set_={
                    "storage_path": storage_path,
                    "file_size": file_size,
                    "content_sha256": content_sha256,
                    "original_filename": original_filename,
                    "uploaded_by_user_id": uploaded_by_user_id,
                    "batch_id": batch_id,
                    "status": PayslipStatus.PUBLISHED,
                    "withdrawn_at": None,
                    "withdraw_reason": None,
                },
            )
            .returning(*PayslipRow.__table__.c)
        )
        row = (await self._session.execute(statement)).first()
        return _to_payslip(row)

    # --- the batch ----------------------------------------------------------

    async def create_batch(
        self,
        *,
        period: str,
        uploaded_by_user_id: UUID,
        total_count: int,
        success_count: int,
        missing_employee_ids: list[UUID],
        unmatched: list[dict[str, object]],
    ) -> BatchRecord:
        """Write the batch row: the upload's counts, and what it answered.

        The two lists are stored as the values they were answered with, which is the point
        of the columns — the live missing list moves as the archive and the staff change,
        and what finance was *shown* is a fact that does not.

        **This commits, and it is the one write in the module that does.** A payslip's
        `batch_id` is a foreign key, so the batch has to exist before the first file — and
        the derivation that decides who is missing goes through `SalaryService.read(...)`,
        which commits on every call (that is how "every read writes its own audit entry"
        is implemented). A batch row that was merely flushed would be rolled out from under
        the rest of the upload by the first of those commits, and the payslips that followed
        would name a batch that no longer existed. Committing it first makes the batch the
        durable record of an attempt: an upload that fails half way leaves the batch row,
        which is exactly what an operator investigating "it says I uploaded March and there
        is nothing there" needs to find.
        """
        row = BatchRow(
            id=uuid4(),
            period=period,
            uploaded_by_user_id=uploaded_by_user_id,
            total_count=total_count,
            success_count=success_count,
            missing_employee_ids=[str(value) for value in missing_employee_ids],
            unmatched=unmatched,
        )
        self._session.add(row)
        await self._session.flush()
        # `created_at` is the database's `now()` (a `server_default`), so the value only
        # exists after the insert. One refresh rather than a Python timestamp, because two
        # rows written in one transaction must agree with the database's clock and not
        # with the application's.
        await self._session.refresh(row)
        record = _to_batch(row)
        await self._session.commit()
        return record

    async def batches(self, *, limit: int, offset: int) -> BatchPage:
        """Every upload, newest first, with the total. The history a payroll reader wants."""
        total = int(
            await self._session.scalar(select(func.count()).select_from(BatchRow)) or 0
        )
        rows = await self._session.scalars(
            select(BatchRow)
            .order_by(BatchRow.created_at.desc(), BatchRow.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return BatchPage(
            items=[_to_batch(row) for row in rows], total=total, limit=limit, offset=offset
        )

    async def latest_batch(self, period: str | None = None) -> BatchRecord | None:
        """The most recent batch, optionally for one month."""
        statement: Select = select(BatchRow)
        if period is not None:
            statement = statement.where(BatchRow.period == period)
        statement = statement.order_by(BatchRow.created_at.desc(), BatchRow.id.desc()).limit(1)
        row = await self._session.scalar(statement)
        return _to_batch(row) if row is not None else None

    async def finalize_batch(
        self,
        batch_id: UUID,
        *,
        total_count: int,
        success_count: int,
        missing_employee_ids: list[UUID],
        unmatched: list[dict[str, object]],
    ) -> BatchRecord:
        """Fill in a batch row's counts and lists once the files have been processed.

        The row is written **before** the payslips, because `payslips.batch_id` is a
        foreign key and `NOT NULL` — a payslip cannot name a batch that does not exist yet.
        The counts are written after the files, so the row a reader finds is the completed
        one rather than one that says a month was filed with nothing in it.

        **The row is re-read and refreshed before it is written.** Several commits have
        happened since `create_batch` — the derivation reads salaries through a service that
        commits each one — so the instance in the identity map can predate them, and
        SQLAlchemy refuses to write a row whose snapshot it no longer trusts
        (`StaleDataError`). `refresh` re-reads the columns, `created_at` included, and the
        write that follows is the four columns this method is for: not the uploader, not
        the month.
        """
        row = await self._session.scalar(
            select(BatchRow).where(BatchRow.id == batch_id)
        )
        if row is None:  # pragma: no cover - the caller created it in this request
            raise LookupError(f"no payslip batch {batch_id}")
        # **The row is refreshed before it is written, and this is the point of the read.**
        # Several commits have happened since `create_batch` (the derivation reads salaries
        # through a service that commits each one), so the instance in the identity map may
        # predate them. `refresh` re-reads the row's columns — including `created_at`, which
        # is the database's `now()` — and leaves the ORM free to write a plain `UPDATE … WHERE
        # id = …` rather than one guarded on a snapshot it no longer has.
        await self._session.refresh(row)
        row.total_count = total_count
        row.success_count = success_count
        row.missing_employee_ids = [str(value) for value in missing_employee_ids]
        row.unmatched = unmatched
        await self._session.flush()
        await self._session.refresh(row)
        return _to_batch(row)

    # --- plumbing -----------------------------------------------------------

    async def commit(self) -> None:
        await self._session.commit()


__all__ = ["PostgresPayslipRepository"]
