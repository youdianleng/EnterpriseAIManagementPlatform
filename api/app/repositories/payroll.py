"""PostgreSQL implementation of the salary archive.

Three things here are worth reading before the SQL:

* **`as_of` is answered in the `WHERE` clause, not by walking rows.** 「任一时点可查出当时
  有效的值」 is a range predicate — `effective_from <= :day AND (effective_to IS NULL OR
  effective_to >= :day)` — over the `daterange(..., '[]')` the exclusion constraint
  already indexes. Folding the chain in Python would be a second implementation of the
  same window semantics, and the one that would drift from the constraint's.

* **The reach is `FilterSpec` first, employee second.** `filter_for(principal, SALARY_RECORD)`
  describes what the caller may read as data, and this module translates it: `allow_all`
  is the whole archive (HR's and finance's remit), otherwise `own_employee_id` is the
  only row that is theirs. There is deliberately **no department clause** — a manager and
  a colleague share a department, and "in my department" must not be read as "mine to
  read" — which is why the spec's `department_ids` is empty and a store that applied it
  "just in case" would refuse the very reads the rule allows.

* **`append` is the only write, and there is no `update` or `delete` at all.** The table
  is append-only in the database (migration 0028 revoked both from the runtime role), so
  a method here could not work even if somebody wrote one. Correcting a record means
  appending another, which is what the ticket asks for: 新增记录不覆盖旧记录.

Nothing commits: the service commits once, so a record and the audit entry that says who
entered it land together or not at all.
"""

from datetime import date
from uuid import UUID, uuid4

from sqlalchemy import ColumnElement, false, func, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.access.kernel import FilterSpec
from app.domain.payroll.models import (
    AllowanceLine,
    RecordInput,
    RecordQuery,
    RecordView,
    SalaryRecord,
)
from app.models.employee import Employee as EmployeeRow
from app.models.payroll import SalaryRecord as SalaryRow


def covers(day: date) -> ColumnElement[bool]:
    """The window predicate: `effective_from <= day <= effective_to`, end optional.

    Written once so the list read, the `as_of` read and the service's overlap pre-check
    cannot answer "which window covers this day" differently. The inclusive end is the
    same one the exclusion constraint's `daterange(..., '[]')` uses.
    """
    return (SalaryRow.effective_from <= day) & (
        (SalaryRow.effective_to.is_(None)) | (SalaryRow.effective_to >= day)
    )


def reach(spec: FilterSpec) -> ColumnElement[bool]:
    """The kernel's `FilterSpec` as a predicate over a salary record.

    `allow_all` first: it is HR's and finance's remit, and it does not depend on the row.
    Otherwise the caller's own id is the whole of their reach through this kind — there
    is no manager clause and no department clause, which is exactly the ticket's
    「经理看不到下属薪资」 read as a store.

    A spec that names nobody gets `false()` rather than no clause at all: a filter that
    forgot to state a reach must refuse every row, never return every row.
    """
    if spec.allow_all:
        return true()
    if spec.own_employee_id is not None:
        return SalaryRow.employee_id == spec.own_employee_id
    return false()


def _to_record(row: SalaryRow) -> SalaryRecord:
    return SalaryRecord(
        id=row.id,
        employee_id=row.employee_id,
        effective_from=row.effective_from,
        effective_to=row.effective_to,
        base_salary=row.base_salary,
        currency=row.currency,
        pay_period=row.pay_period,
        components=_components(row.components),
        change_reason_type=row.change_reason_type,
        change_reason=row.change_reason,
        created_by_user_id=row.created_by_user_id,
        created_at=row.created_at,
    )


def _components(stored: object) -> tuple[AllowanceLine, ...]:
    """The breakdown as the row holds it.

    `amount` stays the string the column holds, deliberately: converting it to a
    `Decimal` here and back to a string in the schema would be two chances to round a
    value that was never a number in the first place.
    """
    if not isinstance(stored, list):  # pragma: no cover - the CHECK refuses anything else
        return ()
    return tuple(
        AllowanceLine(
            code=str(line.get("code", "")),
            label=str(line.get("label", "")),
            amount=str(line.get("amount", "")),
        )
        for line in stored
        if isinstance(line, dict)
    )


def format_name(last_name: str, first_name: str) -> str:
    """`"Apellidos, Nombre"`, the form the overtime export and the report also use.

    Every one of these is read alongside a payroll list, and a list that writes names
    two ways is one somebody has to reconcile by eye.
    """
    return f"{last_name}, {first_name}"


class PostgresSalaryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- reads --------------------------------------------------------------

    async def chain(self, spec: FilterSpec, query: RecordQuery) -> list[RecordView]:
        """One person's records, oldest first, with the subject's name.

        Oldest first is the chain's own order — "what was in force, then what replaced
        it" — and it is the order a reader reconstructs a history in. With `as_of` set
        the answer is at most one row, and the `WHERE` clause is what makes it at most
        one rather than a comment saying so.
        """
        statement = (
            self._chain_statement(spec, query)
            .order_by(SalaryRow.effective_from, SalaryRow.id)
            .limit(query.limit)
            .offset(query.offset)
        )
        rows = (await self._session.execute(statement)).all()
        return [
            RecordView(
                record=_to_record(row[0]),
                employee_name=format_name(row[1], row[2]),
            )
            for row in rows
        ]

    async def count_chain(self, spec: FilterSpec, query: RecordQuery) -> int:
        statement = (
            select(func.count())
            .select_from(SalaryRow)
            .where(reach(spec), SalaryRow.employee_id == query.employee_id)
        )
        if query.as_of is not None:
            statement = statement.where(covers(query.as_of))
        return await self._session.scalar(statement) or 0

    async def latest_covering(
        self, employee_id: UUID, day: date, *, excluding: UUID | None = None
    ) -> SalaryRecord | None:
        """The record whose window covers `day`, if there is one.

        Used by the service before an append, so an overlap is refused with a message
        naming the date rather than by an `IntegrityError` a client cannot read. The
        exclusion constraint is still the guarantee; this is the sentence.
        """
        statement = (
            select(SalaryRow)
            .where(SalaryRow.employee_id == employee_id, covers(day))
            .order_by(SalaryRow.effective_from.desc(), SalaryRow.id.desc())
            .limit(1)
        )
        if excluding is not None:
            statement = statement.where(SalaryRow.id != excluding)
        row = await self._session.scalar(statement)
        return _to_record(row) if row is not None else None

    async def has_initial(self, employee_id: UUID) -> bool:
        return bool(
            await self._session.scalar(
                select(func.count())
                .select_from(SalaryRow)
                .where(
                    SalaryRow.employee_id == employee_id,
                    SalaryRow.change_reason_type == "initial",
                )
            )
        )

    async def has_any(self, employee_id: UUID) -> bool:
        """Whether this person has any record at all.

        The question the personnel applier asks (ticket 43): a `salary` change applied to
        somebody with no archive entry is the *opening* one, and the same change applied
        later is an adjustment. One count, because the applier's problem is not "which
        record is in force" — that is the overlap rule's — but "is there one".
        """
        return bool(
            await self._session.scalar(
                select(func.count())
                .select_from(SalaryRow)
                .where(SalaryRow.employee_id == employee_id)
            )
        )

    async def employee_exists(self, employee_id: UUID) -> bool:
        return bool(
            await self._session.scalar(
                select(func.count()).select_from(EmployeeRow).where(EmployeeRow.id == employee_id)
            )
        )

    # --- the one write ------------------------------------------------------

    async def append(self, data: RecordInput) -> SalaryRecord:
        """Append one record. There is no statement here that could rewrite another."""
        row = SalaryRow(
            id=uuid4(),
            employee_id=data.employee_id,
            effective_from=data.effective_from,
            effective_to=data.effective_to,
            base_salary=data.base_salary,
            currency=data.currency,
            pay_period=data.pay_period,
            components=[
                {"code": line.code, "label": line.label, "amount": line.amount}
                for line in data.components
            ],
            change_reason_type=data.change_reason_type,
            change_reason=data.change_reason,
            created_by_user_id=data.created_by_user_id,
        )
        self._session.add(row)
        await self._session.flush()
        return _to_record(row)

    # --- plumbing -----------------------------------------------------------

    async def commit(self) -> None:
        await self._session.commit()

    # --- internals ----------------------------------------------------------

    def _chain_statement(self, spec: FilterSpec, query: RecordQuery):  # noqa: ANN202
        statement = (
            select(SalaryRow, EmployeeRow.last_name, EmployeeRow.first_name)
            .join(EmployeeRow, EmployeeRow.id == SalaryRow.employee_id)
            .where(reach(spec), SalaryRow.employee_id == query.employee_id)
        )
        if query.as_of is not None:
            statement = statement.where(covers(query.as_of))
        return statement


__all__ = ["PostgresSalaryRepository", "covers", "format_name", "reach"]
