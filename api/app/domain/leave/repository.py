"""Persistence contract for the leave module.

Four things about this interface are load-bearing:

* **Nothing commits.** The service commits once, so a request, the days it reserves
  and the ledger row that records the movement land together or not at all. A
  balance that moved without a request behind it is a deduction nobody can explain.

* **`ensure_balance` is the materialisation, and it is race-safe.** A year's row is
  created the first time it is needed rather than by a job, so two requests filed in
  the same second must not both think they created it — the second would write a
  second `grant` entry and the history would show an allowance granted twice.

* **`lock_balance` is why the allowance holds under concurrency.** The service takes
  the row `FOR UPDATE` before it reads the remainder and reserves against it, so two
  submissions cannot both see the same 3 remaining days and both take them. The
  database's check constraint is the second line; this is the first.

* **`lock_next_unsettled` carries `SKIP LOCKED`.** The settle sweep is a pass over
  filed requests, and two workers running it must take different documents rather
  than block on the same one. `approval_status_of` is separate because whether an
  approved request was approved is the engine's question, and SQL would have to
  re-express the rule.
"""

from datetime import date, datetime
from typing import Protocol
from uuid import UUID

from app.domain.approval.models import ApprovalStatus
from app.domain.leave.models import (
    LeaveBalance,
    LeaveBalanceEntry,
    LeaveEntryType,
    LeaveRequest,
    LeaveRequestInput,
    LeaveRequestQuery,
    LeaveRequestState,
    LeaveType,
    LeaveTypeInput,
    LeaveTypePatch,
)


class LeaveRepository(Protocol):
    # --- the type catalogue -------------------------------------------------

    async def list_types(self, *, include_inactive: bool = False) -> list[LeaveType]:
        """The catalogue, active ones first and by code.

        A retired type stays readable — a request filed under it refers to it, and a
        report names it — and is filtered out of the list a client offers.
        """
        ...

    async def get_type_by_code(self, code: str) -> LeaveType | None:
        """A type by the code a request names it by. The lookup every filing makes."""
        ...

    async def get_type(self, leave_type_id: UUID) -> LeaveType | None: ...

    async def save_type(
        self, data: LeaveTypeInput, *, type_id: UUID | None = None
    ) -> LeaveType:
        """Create a type, or replace every field of an existing one."""
        ...

    async def update_type(self, type_id: UUID, patch: LeaveTypePatch) -> LeaveType:
        """Change what the patch states and leave the rest alone.

        `code` is deliberately not patchable: it is what a request and a report name
        a type by, and changing it would rewrite what an old report meant.
        """
        ...

    # --- balances -----------------------------------------------------------

    async def get_balance(
        self, employee_id: UUID, year: int, leave_type_id: UUID
    ) -> LeaveBalance | None: ...

    async def get_balance_by_id(self, balance_id: UUID) -> LeaveBalance | None:
        """By its own id, which is how a ledger row names the account it moved."""
        ...

    async def ensure_balance(
        self,
        employee_id: UUID,
        year: int,
        leave_type_id: UUID,
        *,
        entitled_days: int,
    ) -> tuple[LeaveBalance, bool]:
        """The year's row, created with this entitlement if it does not exist.

        Returns the row and whether *this* call created it, which is what tells the
        service to write the `grant` entry exactly once. `INSERT ... ON CONFLICT DO
        NOTHING RETURNING` is what makes the answer true under concurrency: the
        statement waits for a conflicting transaction and reports nothing when that
        transaction's row is the one that survived.
        """
        ...

    async def lock_balance(self, balance_id: UUID) -> LeaveBalance:
        """The row `FOR UPDATE`, held until the caller's commit."""
        ...

    async def list_balances(
        self, employee_id: UUID, *, year: int | None = None
    ) -> list[LeaveBalance]:
        """One person's balances, newest year first."""
        ...

    async def list_balances_for_year(self, year: int) -> list[LeaveBalance]:
        """Everybody's, for the year: HR's company-wide read."""
        ...

    async def write_balance(
        self,
        balance_id: UUID,
        *,
        entitled_days: int,
        carried_over_days: int,
        used_days: int,
        pending_days: int,
    ) -> LeaveBalance:
        """Set all four figures. One writer for the four, because a movement that
        changed two of them is one event and a partial write is a balance that never
        existed."""
        ...

    async def append_entry(
        self,
        *,
        balance_id: UUID,
        entry_type: LeaveEntryType,
        days: int,
        entitled_days: int,
        carried_over_days: int,
        used_days: int,
        pending_days: int,
        leave_request_id: UUID | None = None,
        note: str | None = None,
        created_by_employee_id: UUID | None = None,
    ) -> LeaveBalanceEntry:
        """Append one movement with the totals that followed it. Never an update."""
        ...

    async def entries_for_balance(self, balance_id: UUID) -> list[LeaveBalanceEntry]:
        """The history, oldest first. It is what a balance read explains itself with."""
        ...

    async def entries_for_request(self, request_id: UUID) -> list[LeaveBalanceEntry]:
        """The movements one request caused — one per year it was charged to.

        This is the cross-year split read back rather than recomputed: the year each
        share went to is the year of the balance the entry belongs to.
        """
        ...

    # --- requests -----------------------------------------------------------

    async def save_request(self, data: LeaveRequestInput) -> LeaveRequest: ...

    async def get_request(self, request_id: UUID) -> LeaveRequest | None: ...

    async def list_requests(
        self, query: LeaveRequestQuery
    ) -> list[tuple[LeaveRequest, LeaveRequestState]]:
        """A page, newest first, each row with its derived state.

        The state comes from the query — one join for the whole page — so listing
        requests does not ask the engine once per row.
        """
        ...

    async def count_requests(self, query: LeaveRequestQuery) -> int: ...

    async def mark_filed(
        self,
        request_id: UUID,
        *,
        approval_request_id: UUID,
        business_days_count: int,
        at: datetime,
    ) -> LeaveRequest:
        """Record that the document was handed to the engine.

        The day count travels with it because the reservation recomputes it from the
        stored dates: a calendar edited between drafting and filing changes what the
        leave costs, and the row must state what was charged.
        """
        ...

    async def mark_approved(self, request_id: UUID, at: datetime) -> LeaveRequest:
        """Record that the balance was settled as spent — and that the leave is in
        force, which is what the attendance scan reads this row for."""
        ...

    async def mark_withdrawn(self, request_id: UUID, at: datetime) -> LeaveRequest: ...

    async def mark_settled(self, request_id: UUID, at: datetime) -> LeaveRequest:
        """Record that the reservation has been resolved, one way or the other.

        Its own fact rather than something read from the engine: the sweep that
        retries a crashed settle has to be able to tell "this balance is finished
        with" from "the engine's answer is not in yet".
        """
        ...

    async def lock_next_unsettled(
        self, *, exclude: frozenset[UUID] = frozenset(), only: UUID | None = None
    ) -> LeaveRequest | None:
        """The oldest filed request whose reservation is unresolved, locked.

        `SKIP LOCKED` so a second worker takes a different document instead of
        blocking behind this one, and oldest first so a crash is caught up in the
        order things were filed.
        """
        ...

    async def approval_status_of(self, request_id: UUID) -> ApprovalStatus | None:
        """What the engine says about this request. The engine's rule, asked of it."""
        ...

    async def live_request_overlapping(
        self, employee_id: UUID, start_date: date, end_date: date, *, excluding: UUID | None = None
    ) -> LeaveRequest | None:
        """An open request of this person's that shares a date with the range.

        "Open" is anything that is not rejected or withdrawn: a draft holds no days
        but is the same intention, and filing two overlapping ones would either
        double the deduction or silently do nothing.
        """
        ...

    async def approved_requests_covering(
        self, employee_id: UUID, from_date: date, to_date: date
    ) -> list[LeaveRequest]:
        """Approved, not withdrawn requests overlapping an inclusive range.

        One indexed query answers both the calendar read and the anomaly scan's
        per-date question; the scan filters the rows it gets.
        """
        ...

    # --- plumbing -----------------------------------------------------------

    async def employee_exists(self, employee_id: UUID) -> bool: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


__all__ = ["LeaveRepository"]
