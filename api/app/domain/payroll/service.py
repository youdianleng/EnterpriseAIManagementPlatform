"""The salary archive's service: one read that records itself, one append.

Two operations, and the first is the ticket's unusual requirement:

    read(principal, query)   -> SalaryReading
    append(principal, payload) -> RecordView

**`read` is the only way to obtain a salary figure, and it always writes an audit
entry.** Every caller that answers a question about salary data goes through it — the
employee's own chain, HR's and finance's view of somebody else's, the "what was in force
on this day" lookup, and the question about somebody who has no records at all — so
"every read writes an audit entry" is true because there is one place that serves the
table, not because several routes each remembered to call `record()`. The rows travel
inside a `SalaryReading`, whose constructor refuses anything this module did not issue,
which is what turns the guarantee into a shape a future caller cannot break by omission.

**Nothing here computes money.** `append` stores the figures it was given; `read` returns
them. There is no total, no annual figure, no currency conversion, no proration and no
tax — DESIGN D9 and §8.3 make 西班牙工资单计算 an explicit non-goal, and a service that
folded a base and its allowances into a total would be the first line of one. The
module's own arithmetic is limited to refusing an amount finer than a cent.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditAction, record
from app.domain.access.kernel import ResourceKind, filter_for
from app.domain.access.principal import Principal
from app.domain.errors import DomainError
from app.domain.payroll.errors import PayrollErrorCode
from app.domain.payroll.models import (
    SALARY_ENTITY,
    AllowanceLine,
    Reading,
    RecordInput,
    RecordQuery,
    RecordView,
    SalaryReading,
    parse_amount,
    parse_components,
    parse_currency,
    parse_pay_period,
    parse_reason,
    parse_reason_type,
    parse_window,
)
from app.models.employee import Employee as EmployeeRow
from app.repositories.payroll import PostgresSalaryRepository, format_name


def _invalid(detail: str) -> DomainError:
    return DomainError(PayrollErrorCode.RECORD_INVALID, detail=detail)


class SalaryService:
    """The archive, as the api layer uses it."""

    def __init__(
        self,
        repository: PostgresSalaryRepository,
        session: AsyncSession,
        *,
        principal: Principal,
    ) -> None:
        self._repository = repository
        self._session = session
        #: The principal is construction state rather than a parameter of every method,
        #: for the reason the timesheet report service records: the reach is a *property
        #: of the caller*, and a method that took it as an argument would make "whose
        #: reach is this" a question a call site could answer wrongly.
        self._principal = principal

    # --- the read that records itself ---------------------------------------

    async def read(self, query: RecordQuery) -> SalaryReading:
        """One person's salary chain — and an audit entry saying who looked.

        **This is the only method that returns salary rows**, and it always writes the
        trail entry, in the same transaction as the rows: an audit record that could land
        without the read it describes, or a read that could be served without one, would
        be the two halves of the same defect.

        The entry carries **who read whose record when**, the kind of reach it was (the
        caller's own, or the company's), and how many rows came back — and **no amounts,
        no currency and no reason**. `audit_log` is append-only, retained four years and
        readable by `compliance`, while the figures live in `salary_records` behind its
        own policy; putting a figure in the trail would move it to the wider of the two
        tables, which is the leak this separation exists to prevent.

        A subject with no records is a reading of *no rows*, not a refusal and not a zero:
        somebody in their first week has no salary record, and "0.00 EUR" is a number a
        person could act on. The route serves that as an empty chain, and this method
        still records the look — which is the case a route-level `record()` call would
        most easily have missed.
        """
        spec = filter_for(self._principal, ResourceKind.SALARY_RECORD)
        rows = await self._repository.chain(spec, query)
        total = await self._repository.count_chain(spec, query)

        reading = SalaryReading._issued(
            Reading(
                rows=tuple(rows),
                subject=query.employee_id,
                as_of=query.as_of,
                total=total,
            )
        )
        await self._record_read(query, rows=total)
        await self._repository.commit()
        return reading

    async def _record_read(self, query: RecordQuery, *, rows: int) -> None:
        """The trail entry, written for every read including the empty ones.

        `before`/`after` carry no figures: this is an event, not a change, and the facts
        it states are the subject, the day asked about when there was one, and the row
        count. The actor, the address and the request id come from the request context
        that `record()` reads — which is why auditing a read here was one line rather
        than a parameter threaded through three layers.
        """
        await record(
            self._session,
            action=AuditAction.SALARY_RECORD_READ,
            entity_type=SALARY_ENTITY,
            entity_id=query.employee_id,
            after={
                "subject_employee_id": str(query.employee_id),
                "self": query.employee_id == self._principal.employee_id,
                "as_of": query.as_of.isoformat() if query.as_of is not None else None,
                "records": rows,
            },
        )

    # --- the one append -----------------------------------------------------

    async def append(self, payload: dict[str, Any]) -> RecordView:
        """Store a new record. Nothing existing is touched.

        The rules, in the order they are asked:

        * the employee must exist — a record about nobody is a typo, and the foreign key
          would report it as a 500 rather than as the 404 it is;
        * the window must be a window, and the figures must be storable (amount, currency,
          period, reason) — refused with the field named;
        * a record for a window another record already covers is refused, **and** the
          database refuses it too: this method produces the sentence, the exclusion
          constraint is what makes it true even for a writer that skips this method;
        * the first record for a person is an `initial`, and there is only ever one — by
          the partial unique index, so two entries racing cannot both win.

        The service writes no derived value: what it stores is exactly what the caller
        stated, which is what makes the row an archive entry rather than a computed one.
        """
        employee_id = _as_uuid(payload.get("employee_id"), "employee_id")
        if not await self._repository.employee_exists(employee_id):
            raise DomainError(
                PayrollErrorCode.EMPLOYEE_NOT_FOUND,
                detail=f"no employee {employee_id}",
            )

        start, end = parse_window(payload.get("effective_from"), payload.get("effective_to"))
        amount = parse_amount(payload.get("base_salary"))
        currency = parse_currency(payload.get("currency"))
        period = parse_pay_period(payload.get("pay_period"))
        lines: tuple[AllowanceLine, ...] = parse_components(payload.get("components"))
        reason_type = parse_reason_type(payload.get("change_reason_type"))
        reason = parse_reason(payload.get("change_reason"))

        # The opening-record rule first, and the order matters: a second `initial` whose
        # window happens to be free would otherwise be refused as "that day is taken",
        # which is the wrong sentence — nothing is in the way but the fact that this
        # person was already entered once.
        if reason_type == "initial" and await self._repository.has_initial(employee_id):
            raise DomainError(
                PayrollErrorCode.INITIAL_EXISTS,
                detail=f"employee {employee_id} already has an opening record",
            )
        covering = await self._repository.latest_covering(employee_id, start)
        if covering is not None:
            raise DomainError(
                PayrollErrorCode.RECORD_OVERLAPS,
                detail=(
                    f"{start.isoformat()} is already covered by the record in force from "
                    f"{covering.effective_from.isoformat()} (to "
                    f"{covering.effective_to.isoformat() if covering.effective_to else 'open'})"
                ),
            )

        stored = await self._repository.append(
            RecordInput(
                employee_id=employee_id,
                effective_from=start,
                effective_to=end,
                base_salary=amount,
                currency=currency,
                pay_period=period,
                components=lines,
                change_reason_type=reason_type,
                change_reason=reason,
                created_by_user_id=self._principal.user_id,
            )
        )
        # The trail entry for the write, with the facts rather than the figures: the
        # archive's own row carries the amount, and this says who put it there and why.
        await record(
            self._session,
            action=AuditAction.SALARY_RECORD_WRITTEN,
            entity_type=SALARY_ENTITY,
            entity_id=stored.id,
            after={
                "employee_id": str(employee_id),
                "effective_from": start.isoformat(),
                "effective_to": end.isoformat() if end is not None else None,
                "change_reason_type": reason_type,
                "currency": currency,
                "pay_period": period,
            },
            reason=reason,
        )
        name = await self._name_of(employee_id)
        await self._repository.commit()
        return RecordView(record=stored, employee_name=name)

    async def _name_of(self, employee_id: UUID) -> str:
        row = (
            await self._session.execute(
                select(EmployeeRow.last_name, EmployeeRow.first_name).where(
                    EmployeeRow.id == employee_id
                )
            )
        ).first()
        if row is None:  # pragma: no cover - the caller checked it exists
            return ""
        return format_name(row[0], row[1])


def _as_uuid(value: Any, field_name: str) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise _invalid(f"{field_name!r} must be a UUID") from None


__all__ = ["SalaryService"]
