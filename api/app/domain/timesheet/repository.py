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

* **`entries_in_week` is the week, `entries_in_sheet` is the document.** They differ
  once a correction exists: a supplement's reversal rows belong to the supplement and
  are part of the week's net, so the grid reads by week and the filing reads by sheet.
  Both exist because "what does this week come to" and "what am I about to file" are
  different questions with different right answers.

* **`report_rows` takes a `FilterSpec`, and it is not optional (ticket 30).** Every
  other read here is scoped by the caller's own `employee_id`; the report is the one
  question that is legitimately *about* somebody else, and the spec is what makes it
  safe. A signature that allowed the filter without the permission is how a
  filter-free query gets written — the argument `ProjectRepository.list_selectable`
  already records.

* **The report's totals are a second statement, over the same predicate.** The two
  calls share one `WHERE`, so the totals row cannot describe different rows from the
  table above it, and the two `count(distinct)` figures stay exact rather than being
  the sum of per-group counts.
"""

from datetime import date
from typing import Protocol
from uuid import UUID

from app.domain.access.kernel import FilterSpec
from app.domain.timesheet.models import (
    EntryInput,
    EntryPatch,
    Timesheet,
    TimesheetEntry,
    TimesheetPage,
    TimesheetStatus,
)
from app.domain.timesheet.report import ReportFilter, ReportRow, ReportTotals


class TimesheetRepository(Protocol):
    async def get(self, timesheet_id: UUID) -> Timesheet | None: ...

    async def get_week(self, employee_id: UUID, week_start: date) -> Timesheet | None:
        """The week's *original* sheet, or nothing when the week has never been written.

        Keyed by the Monday, and deliberately not "any sheet of this week": this is the
        row a week is identified by, and a correction filed against it is read with
        `sheets_in_week`. The partial unique index behind this lookup is what makes
        "one original sheet per person per week" a database fact rather than a service
        check that a second concurrent request can pass twice.
        """
        ...

    async def sheets_in_week(self, employee_id: UUID, week_start: date) -> list[Timesheet]:
        """Every sheet of one person's week: the original first, then its supplements.

        One query for the whole week, because the grid needs all of them at once — the
        original's status and each supplement's — and a per-sheet read would be a round
        trip per correction somebody filed.
        """
        ...

    async def create_week(self, employee_id: UUID, week_start: date) -> Timesheet:
        """Write the week's original sheet. The caller has already refused a duplicate
        politely; a race that reaches this despite that is refused by the constraint, and
        the service turns the constraint into the same catalogued answer."""
        ...

    async def create_supplement(
        self, employee_id: UUID, week_start: date, supersedes_id: UUID
    ) -> Timesheet:
        """Write a correction's own sheet, linked to the week it corrects.

        The link is written here and never on the original: the original is the record
        the approver signed, and a correction must not be able to change it even by
        adding a pointer to it.
        """
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
        a row that the read itself must not create. This is also what makes a reversal
        part of the week it corrects: a supplement shares the original's Monday, so
        the week's net is one sum over both sheets.
        """
        ...

    async def entries_in_sheet(self, timesheet_id: UUID) -> list[TimesheetEntry]:
        """Every entry *of one sheet*, ordered day then creation.

        The filing path's read: an approval is about a document, so the rows that
        travel to the approver are that sheet's, not the week's.
        """
        ...

    async def get_entry(self, entry_id: UUID) -> TimesheetEntry | None: ...

    async def reversal_for(self, entry_id: UUID) -> TimesheetEntry | None:
        """The reversal that cancels this entry, if one has been written.

        One indexed lookup rather than the sheet's whole row set, because the question
        is asked before every edit: an entry that has been reversed is frozen — it is
        what the reversal negates — and a service check that loaded a dozen rows to
        answer it would be the reason somebody moved the check.
        """
        ...

    async def report_rows(self, spec: FilterSpec, report_filter: ReportFilter) -> list[ReportRow]:
        """The report's table: one row per group, with billable split from non-billable.

        Three things the statement has to get right, and they are the ticket's three
        numeric lines rather than an implementation detail:

        * **It joins the entry's own sheet and requires it to be `approved`.** Per
          *sheet*, not per week: a corrected week has an approved original and a
          supplement of its own, and the correction's reversal and replacement both
          count the moment the correction is decided and neither counts before. A
          draft or a pending sheet reaches no row at all — it is absent rather than a
          row of zeros.
        * **`is_billable` groups**, so the two subtotals are sum of `minutes` over one
          boolean. A reversal carries its original's flag (the migration's trigger
          refuses one that does not), which is what makes the two columns add up to
          the net.
        * **`spec` is applied in full.** `allow_all` is HR; otherwise the rows are the
          caller's reports *or* the projects they manage, and a spec that names
          neither matches nothing rather than everything.

        Every field of `report_filter` is applied as well, and the grouping is the one
        the filter asked for — see `ReportFilter`. Nothing commits and nothing is
        written: this is a read.
        """
        ...

    async def report_totals(self, spec: FilterSpec, report_filter: ReportFilter) -> ReportTotals:
        """The same figures over the whole period, in one row.

        Its own statement rather than the rows added up, because `entries` and `weeks`
        are `count(distinct)` and a sum of per-group counts is not the count over the
        period — a week that booked time on two projects appears in two rows. The
        predicate is the one `report_rows` used, which is what makes the totals line
        agree with the table above it by construction rather than by coincidence.
        """
        ...

    async def add_entry(
        self, timesheet_id: UUID, employee_id: UUID, week_start: date, data: EntryInput
    ) -> TimesheetEntry:
        """Append one entry. The employee and week are passed as well as the sheet id
        because the table's composite foreign key checks all three against the same
        row, which is what stops an entry from landing in somebody else's week.

        The entry's kind and the row it reverses travel inside `EntryInput`, because
        they are the server's decision: a reversal is written by the supplementary
        flow, never by a request.
        """
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

    async def week_is_locked(self, week_start: date) -> bool:
        """Whether the week is globally closed — the record, not the clock.

        The service decides the window; this is the fact that outlives the decision,
        and it is the same row `time_entries_guard_week_lock` refuses a console over.
        """
        ...

    async def lock_week(
        self,
        week_start: date,
        *,
        reason: str | None = None,
        locked_by_employee_id: UUID | None = None,
    ) -> bool:
        """Close a week. Returns whether this call is the one that closed it.

        Idempotent: a week that is already locked stays as it was, with the timestamp
        and the reason of the call that closed it — a second sweep must not rewrite
        when a week was closed.
        """
        ...

    async def lock_expired_weeks(
        self, employee_id: UUID, *, before: date, reason: str
    ) -> list[date]:
        """Close every week of one employee that starts before `before`.

        One statement over the sheets the employee has rather than a row per calendar
        week: a week nobody ever wrote is already unwritable by construction, and
        closing a decade of them would be rows about nothing. Returns the weeks this
        call closed, so the caller can say so in the audit record.
        """
        ...

    async def list_weeks(
        self, employee_id: UUID, *, limit: int = 50, offset: int = 0
    ) -> TimesheetPage:
        """The caller's own weeks, newest first, with the total.

        A page rather than a list because a career is hundreds of weeks, and the
        total is returned with it for the reason the project module gives: a caller
        that counted separately would repeat the filter and eventually count a
        different one. Original sheets only: a supplement is a correction *of* a week
        and appears beside it rather than as a week of its own.
        """
        ...

    async def employee_exists(self, employee_id: UUID) -> bool: ...

    async def commit(self) -> None: ...


__all__ = ["TimesheetRepository"]
