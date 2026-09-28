"""Weekly timesheet domain: one week per person, and the entries it holds.

The module owns `timesheets` and `timesheet_entries` and nothing else. What it
deliberately does *not* own, and reads through a collaborator instead:

* whether a project may receive time, and whether the time is billable —
  `app.domain.project`, which ticket 27 built for exactly this;
* what a day was expected to hold — `app.domain.schedule`;
* whether a filed week was approved — `app.domain.approval`, reached through
  `ApprovalNotifier` so the notifications cannot be forgotten;
* **whose** hours a caller may read — `app.domain.access`'s kernel, which answers that
  as a `FilterSpec` the report's query is written against (ticket 30). Nothing in this
  module tests a role or compares an employee id, which is what makes "a manager sees
  their reports, a project manager their projects, HR everybody" one rule rather than
  three query branches.

`Ticket 27 left a third obligation here`, and it is honoured at the database level
rather than only in the service: migration 0015's trigger refuses a `time_entries`
row against a project that is not `active`, so a console, a script or a future
endpoint cannot write one either.
"""

from app.domain.timesheet.export import ExportFile
from app.domain.timesheet.models import (
    DAYS_PER_WEEK,
    EDITABLE_STATUSES,
    MAX_ENTRY_MINUTES,
    MINUTES_PER_DAY,
    UNSET,
    DayTotal,
    EntryInput,
    EntryPatch,
    OverBudgetDay,
    ProjectLabel,
    Timesheet,
    TimesheetEntry,
    TimesheetPage,
    TimesheetStatus,
    WeekInput,
    WeekView,
    assert_monday,
    monday_of,
)
from app.domain.timesheet.report import (
    DEFAULT_GROUPING,
    DIMENSIONS,
    MAX_REPORT_DAYS,
    DimensionValue,
    ReportDimension,
    ReportFilter,
    ReportRow,
    ReportSummary,
    ReportTotals,
)
from app.domain.timesheet.repository import TimesheetRepository
from app.domain.timesheet.service import ENTITY_TYPE, MAX_ENTRIES_PER_DAY, TimesheetService

__all__ = [
    "DAYS_PER_WEEK",
    "DEFAULT_GROUPING",
    "DIMENSIONS",
    "EDITABLE_STATUSES",
    "ENTITY_TYPE",
    "MAX_ENTRIES_PER_DAY",
    "MAX_ENTRY_MINUTES",
    "MAX_REPORT_DAYS",
    "MINUTES_PER_DAY",
    "UNSET",
    "DayTotal",
    "DimensionValue",
    "EntryInput",
    "EntryPatch",
    "ExportFile",
    "OverBudgetDay",
    "ProjectLabel",
    "ReportDimension",
    "ReportFilter",
    "ReportRow",
    "ReportSummary",
    "ReportTotals",
    "Timesheet",
    "TimesheetEntry",
    "TimesheetPage",
    "TimesheetRepository",
    "TimesheetService",
    "TimesheetStatus",
    "WeekInput",
    "WeekView",
    "assert_monday",
    "monday_of",
]
