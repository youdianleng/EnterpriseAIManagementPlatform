"""Persistence contract for the overtime module.

Four things about this interface are load-bearing:

* **Nothing commits.** The service commits once, so a request, the record an approval
  wrote and the ledger row that records the movement land together or not at all. A
  record without its `approve` entry is a figure nobody can explain.

* **`lock_next_unresolved` and `lock_next_unsettled` carry `SKIP LOCKED`.** Both sweeps
  are passes over rows — the documents the engine has decided, and the records whose day
  has ended — and two workers running either must take different rows rather than block
  on the same one. `approval_status_of` is separate because whether a request was
  approved is the engine's question, and SQL would have to re-express the rule.

* **The settlement writes are their own methods, and neither touches the other's
  column.** `write_settlement` sets `worked_minutes`, `computed_minutes` and the flag;
  `write_confirmation` sets `confirmed_minutes`, who and why. There is deliberately no
  "write the record's minutes" method that could do both, because the ticket's whole
  guarantee is that HR's figure lands *beside* the computed one.

* **`day_minutes` is the attendance seam.** It answers one question — what the approved
  overtime for one day comes to — with one indexed query, so the attendance module can
  carry the figure in its own snapshot without learning anything about this one.
"""

from datetime import date, datetime
from typing import Protocol
from uuid import UUID

from app.domain.approval.models import ApprovalStatus
from app.domain.overtime.models import (
    MonthlyTotal,
    NewOvertimeRecord,
    OvertimeEntry,
    OvertimeEntryType,
    OvertimeExportRow,
    OvertimeRecord,
    OvertimeRecordQuery,
    OvertimeRequest,
    OvertimeRequestInput,
    OvertimeRequestPatch,
    OvertimeRequestQuery,
    OvertimeRequestState,
)


class OvertimeRepository(Protocol):
    # --- requests -----------------------------------------------------------

    async def save_request(self, data: OvertimeRequestInput) -> OvertimeRequest:
        """Write a draft. Nothing is reserved and nothing is approved yet."""
        ...

    async def get_request(self, request_id: UUID) -> OvertimeRequest | None: ...

    async def list_requests(
        self, query: OvertimeRequestQuery
    ) -> list[tuple[OvertimeRequest, OvertimeRequestState]]:
        """A page, newest first, each row with its derived state.

        The state comes from the query — one join for the whole page — so listing does
        not ask the engine once per row.
        """
        ...

    async def count_requests(self, query: OvertimeRequestQuery) -> int: ...

    async def write_draft(
        self, request_id: UUID, *, patch: OvertimeRequestPatch
    ) -> OvertimeRequest:
        """Change what the patch states and leave the rest alone. Drafts only."""
        ...

    async def mark_filed(
        self, request_id: UUID, *, approval_request_id: UUID, at: datetime
    ) -> OvertimeRequest:
        """Record that the document was handed to the engine."""
        ...

    async def mark_approved(self, request_id: UUID, at: datetime) -> OvertimeRequest:
        """Record that the engine's approval was resolved into a record."""
        ...

    async def mark_withdrawn(self, request_id: UUID, at: datetime) -> OvertimeRequest: ...

    async def mark_settled(self, request_id: UUID, at: datetime) -> OvertimeRequest:
        """Record that this module has finished with the document.

        Its own fact rather than something read from the engine: the sweep that retries
        a crashed resolve has to tell "this document is finished with" from "the
        engine's answer is not in yet".
        """
        ...

    async def lock_next_unresolved(
        self, *, exclude: frozenset[UUID] = frozenset(), only: UUID | None = None
    ) -> OvertimeRequest | None:
        """The oldest filed request whose record is not written yet, locked.

        `SKIP LOCKED` so a second worker takes a different document, and oldest first
        so a crash is caught up in the order things were filed.
        """
        ...

    async def approval_status_of(self, request_id: UUID) -> ApprovalStatus | None:
        """What the engine says about this request. The engine's rule, asked of it."""
        ...

    async def live_request_for_day(
        self, employee_id: UUID, business_date: date, *, excluding: UUID | None = None
    ) -> OvertimeRequest | None:
        """An open request of this person's for that day.

        "Open" is anything not yet resolved: a draft is the same intention and filing
        two would be two records for one day.
        """
        ...

    # --- records ------------------------------------------------------------

    async def save_record(self, record: NewOvertimeRecord) -> OvertimeRecord:
        """Write the record an approval produced, in its month bucket.

        Collides on `uq_overtime_records_request` if the resolve sweep already wrote
        one, which is what makes a second pass over the same document harmless.
        """
        ...

    async def get_record(self, record_id: UUID) -> OvertimeRecord | None: ...

    async def record_for_request(self, request_id: UUID) -> OvertimeRecord | None:
        """The record a request became, which is how a document view links to it."""
        ...

    async def record_for_day(self, employee_id: UUID, business_date: date) -> OvertimeRecord | None:
        """That person's overtime record for that day. At most one, by construction."""
        ...

    async def list_records(self, query: OvertimeRecordQuery) -> list[OvertimeRecord]:
        """A page of records, newest day first."""
        ...

    async def count_records(self, query: OvertimeRecordQuery) -> int: ...

    async def lock_next_unsettled(
        self,
        *,
        month: str | None = None,
        exclude: frozenset[UUID] = frozenset(),
        only: UUID | None = None,
    ) -> OvertimeRecord | None:
        """The oldest record whose day has not been computed, locked.

        Oldest business date first: the day that has been waiting longest is the one
        whose figure somebody is most likely to be looking for.
        """
        ...

    async def write_settlement(
        self,
        record_id: UUID,
        *,
        worked_minutes: int,
        computed_minutes: int,
        needs_confirmation: bool,
        at: datetime,
    ) -> OvertimeRecord:
        """Write the day's arithmetic. Never touches `confirmed_minutes`."""
        ...

    async def write_confirmation(
        self,
        record_id: UUID,
        *,
        confirmed_minutes: int,
        note: str,
        confirmed_by_employee_id: UUID | None,
        at: datetime,
    ) -> OvertimeRecord:
        """Write HR's figure beside the computed one. Never touches it."""
        ...

    async def append_entry(
        self,
        *,
        record_id: UUID,
        entry_type: OvertimeEntryType,
        approved_minutes: int,
        computed_minutes: int | None,
        confirmed_minutes: int | None,
        note: str | None = None,
        created_by_employee_id: UUID | None = None,
    ) -> OvertimeEntry:
        """Append one movement with the figures that followed it. Never an update."""
        ...

    async def entries_for_record(self, record_id: UUID) -> list[OvertimeEntry]:
        """The history, oldest first. It is what a record explains itself with."""
        ...

    async def day_minutes(self, employee_id: UUID, business_date: date) -> int | None:
        """The overtime the day's records come to, or nothing when there are none.

        The attendance module's one question. The figure is each record's *effective*
        minutes — HR's, else the settled smaller-of, else the approved — because the
        day a reader opens and the month finance exports must not disagree.
        """
        ...

    async def minutes_by_date(
        self, employee_id: UUID, from_date: date, to_date: date
    ) -> dict[date, int]:
        """The same question for a range, grouped by day.

        One query for the whole range, the shape `ExpectationSource.day_expectations`
        takes: the attendance range read asks about a month of gaps at once, and a query
        per day would turn a calendar view into thirty round trips. Days with no
        overtime are absent from the mapping rather than present as zero.
        """
        ...

    async def month_totals(self, month: str) -> list[MonthlyTotal]:
        """One row per employee with overtime in that month, by employee name.

        The group-by `month_bucket` exists for. `employee_name` is read here rather
        than joined by the caller so the month's arithmetic and the name it belongs to
        come out of one query.
        """
        ...

    async def export_rows(self, month: str) -> list[OvertimeExportRow]:
        """The month's records with the six facts the file states, in file order.

        Ordered by staff number and then date — the order an accountant reads a
        payroll list in — with the name and the department joined from the employee
        module. The staff number lives in the withheld block, so this query returns
        rows only to a caller whose principal made `employee_private` readable.
        """
        ...

    # --- plumbing -----------------------------------------------------------

    async def employee_exists(self, employee_id: UUID) -> bool: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


__all__ = ["OvertimeExportRow", "OvertimeRepository"]
