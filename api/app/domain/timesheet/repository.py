"""Persistence contract for the timesheet module.

Three things about this interface are load-bearing:

* **Nothing commits.** The service commits once, so an entry and the audit record of
  who wrote it land together or not at all. A timesheet nobody can account for is
  exactly the row a payroll dispute cannot resolve.

* **`week_view` is not `get_week`.** The read that builds the grid takes a
  `WeekView` and returns the entries grouped by day; the write paths take a
  `Timesheet` row. Keeping them apart is what stops a read from creating the row it
  is reading — a `GET` that inserted would make every page load a write, and would
  destroy the difference between "I have not filed this week" and "I have not opened
  this page".

* **`entries_in_week` returns every entry, not a page.** A week is at most a few
  dozen rows by construction (seven days, and a day is a working day), and the grid
  computes a total per day and per week from them. A paginated read here would be a
  total computed over a page, which is the defect that makes a timesheet disagree
  with itself.
"""

from datetime import date
from typing import Protocol
from uuid import UUID

from app.domain.timesheet.models import (
    EntryInput,
    EntryPatch,
    Timesheet,
    TimesheetEntry,
    TimesheetPage,
    TimesheetStatus,
)


class TimesheetRepository(Protocol):
    async def get(self, timesheet_id: UUID) -> Timesheet | None: ...

    async def get_week(self, employee_id: UUID, week_start: date) -> Timesheet | None:
        """The row for one person's one week, or nothing when it has not been written.

        Keyed by the Monday. The unique constraint behind this lookup is what makes
        "one timesheet per person per week" a database fact rather than a service
        check that a second concurrent request can pass twice.
        """
        ...

    async def create_week(self, employee_id: UUID, week_start: date) -> Timesheet:
        """Write the week's row. The caller has already refused a duplicate politely;
        a race that reaches this despite that is refused by the constraint, and the
        service turns the constraint into the same catalogued answer."""
        ...

    async def set_status(
        self,
        timesheet_id: UUID,
        status: TimesheetStatus,
        *,
        approval_request_id: UUID | None = None,
        submitted_at: object | None = None,
    ) -> Timesheet:
        """Move the week's status, and record the request it was filed under.

        One writer for both, because the two columns describe one event: a week that
        is `pending` with no request behind it is a week nobody can chase.
        """
        ...

    async def entries_in_week(self, employee_id: UUID, week_start: date) -> list[TimesheetEntry]:
        """Every entry of one person's week, ordered day then creation.

        By employee and week rather than by timesheet id, so entries of a week whose
        row was somehow lost are still readable — and so the read does not depend on
        a row that the read itself must not create.
        """
        ...

    async def get_entry(self, entry_id: UUID) -> TimesheetEntry | None: ...

    async def add_entry(
        self, timesheet_id: UUID, employee_id: UUID, week_start: date, data: EntryInput
    ) -> TimesheetEntry:
        """Append one entry. The employee and week are passed as well as the sheet id
        because the table's composite foreign key checks all three against the same
        row, which is what stops an entry from landing in somebody else's week."""
        ...

    async def update_entry(
        self, entry_id: UUID, patch: EntryPatch, *, is_billable: bool
    ) -> TimesheetEntry:
        """Change one entry, with the billable value the service re-resolved.

        `is_billable` travels beside the patch rather than inside it because it is
        not a field a request may state: it is the server's answer, and giving it a
        home in the patch would put it one `model_dump` away from being client-set.
        """
        ...

    async def delete_entry(self, entry_id: UUID) -> None: ...

    async def rollback(self) -> None:
        """Drop the current transaction, so a refused write leaves nothing behind.

        On the interface because a constraint violation — the unique week, the
        project-status trigger — aborts the transaction it happened in, and the
        service has to start a clean one before it can read and answer.
        """
        ...

    async def week_exists(self, employee_id: UUID, week_start: date) -> bool: ...

    async def list_weeks(
        self, employee_id: UUID, *, limit: int = 50, offset: int = 0
    ) -> TimesheetPage:
        """The caller's own weeks, newest first, with the total.

        A page rather than a list because a career is hundreds of weeks, and the
        total is returned with it for the reason the project module gives: a caller
        that counted separately would repeat the filter and eventually count a
        different one.
        """
        ...

    async def employee_exists(self, employee_id: UUID) -> bool: ...

    async def commit(self) -> None: ...


__all__ = ["TimesheetRepository"]
