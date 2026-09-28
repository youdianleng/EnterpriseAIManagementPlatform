"""The report: approved hours, aggregated four ways, with billable split out.

Ticket 30's whole subject, and six decisions are worth reading before the code —
every one of them is about *which rows a number is made of*:

* **A row counts when its own sheet is approved, and not before.** Not when the
  *week* is approved — a week has several sheets once it has been corrected — and
  not "when the original is approved", which would count a correction the moment it
  was drafted. `timesheets.status` is the column, read per sheet, so a supplement in
  flight changes nothing and the reversal plus its replacement both arrive together
  the moment the correction is decided. This is also why the report does not ask the
  approval engine per row: the engine's answer is what `apply_decision` writes onto
  this column, and a report over a hundred employees is not a place for a round trip
  per week. The consequence is stated rather than hidden: a week whose decision has
  been taken in the engine and never read back is still whatever this column says.

* **Drafts and pending weeks are absent, not zero.** The filter is a predicate and
  not a `sum(...)` over everything with a conditional: a report that listed a draft
  week as a row of zeros would be telling a reader that the week was counted and came
  to nothing, which is the opposite of 草稿与审批中的不计入.

* **Reversals subtract, and they subtract from the same column they were added to.**
  A reversal inherits its original's `is_billable` (`timesheet_entries_guard_reversal`
  refuses one that does not), so `billable + non_billable = net` per row and in the
  total. It is asserted in the tests rather than assumed, because a reversal carrying
  the other flag would move minutes between the two subtotals while every total
  stayed right — the one defect this split exists to make impossible.

* **Four dimensions, combinable, and they are the *grouping* as well as the facets.**
  `group_by` is an ordered tuple, so `("period", "project")` is a table of weeks by
  project and the same filter is one dimension of it. The dimensions are project,
  department, employee and period; a facet narrows and a grouping partitions, and
  both run through the same `ReportFilter`.

* **`period` is the week, and `department` is the *project's* department.** Both are
  forced by what the rows are. A week is what two people approve, so it is the finest
  period the report can state that corresponds to a document; and the department that
  owns work is the project's, while the department a *person* was in on the day is a
  fact about an assignment that has to be resolved as of that date — a different
  question, and one this module does not answer silently by picking today's.

* **`from_date`/`to_date` are days, inclusive, and bounded.** They are not snapped to
  weeks: a month is not a whole number of weeks, and a report for March is what
  finance asks for. A range that cuts a week counts the days inside it, and a
  `period` row then states the part of the week the range covers — which is why the
  bound is a period the record is kept for rather than an arbitrary width.
"""

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from uuid import UUID

from app.domain.errors import DomainError
from app.domain.timesheet.errors import TimesheetErrorCode

#: The widest period one report may state. The number is the four years the Spanish
#: working-time record is kept for (the same window `attendance.models.MAX_RANGE_DAYS`
#: bounds its own read to), chosen because it is a fact about the obligation rather
#: than a tuning knob: beyond it there is nothing to report, and an unbounded range
#: would make "sum the whole table" a request anybody could send.
MAX_REPORT_DAYS = 1461


class ReportDimension(StrEnum):
    """What one line of the report is *about*.

    All four are both a facet ("just this project") and a grouping ("one line per
    project"), which is why they are one enum rather than a filter shape and a
    grouping shape: the ticket asks for 四个维度，并可组合筛选, and two vocabularies
    for one idea would let a caller filter by something they cannot group by.
    """

    PROJECT = "project"
    DEPARTMENT = "department"
    EMPLOYEE = "employee"
    #: The week, keyed by its Monday — the unit two people approve, and therefore the
    #: finest period this report can state. See the module docstring.
    PERIOD = "period"


#: The four dimensions, in the order they are documented. A caller's `group_by`
#: carries its own order, because the column order of a table is the caller's
#: question; this tuple is what "all four" means.
DIMENSIONS: tuple[ReportDimension, ...] = (
    ReportDimension.PROJECT,
    ReportDimension.DEPARTMENT,
    ReportDimension.EMPLOYEE,
    ReportDimension.PERIOD,
)

#: The grouping a report gets when the caller names none. One line per project: the
#: question the ticket opens with (按项目) and the one a client-facing file answers.
DEFAULT_GROUPING: tuple[ReportDimension, ...] = (ReportDimension.PROJECT,)


@dataclass(frozen=True, slots=True)
class ReportFilter:
    """What the report is asked for: a period, four facets, and a grouping.

    Every facet is conjunctive with the period and with the others, and empty means
    "no narrowing on this dimension" rather than "nothing matches" — the same
    convention `ProjectQuery` uses. `group_by` names the dimensions the rows are
    partitioned by; the reach a caller has is not here at all, because it is the
    kernel's `FilterSpec` and travels beside this object rather than inside it
    (`TimesheetRepository.report_rows`).
    """

    from_date: date
    to_date: date
    group_by: tuple[ReportDimension, ...] = DEFAULT_GROUPING
    project_ids: tuple[UUID, ...] = ()
    department_ids: tuple[UUID, ...] = ()
    employee_ids: tuple[UUID, ...] = ()

    @property
    def days(self) -> int:
        """How many days the period covers, both ends included."""
        return (self.to_date - self.from_date).days + 1


@dataclass(frozen=True, slots=True)
class DimensionValue:
    """One dimension's value on one line, named.

    `code` and the two names travel rather than a rendered label, for the reason
    `ProjectLabel` records: the interface ships in two languages, and a server that
    picked one would be a server deciding what a Spanish reader sees.

    `week_start` is set on a `period` value and `id` on the three that name a row.
    They are separate fields rather than one polymorphic column because a date is not
    an identifier, and a caller reading `id` should not have to know that a week's id
    is its Monday written as text.
    """

    kind: ReportDimension
    id: UUID | None = None
    code: str | None = None
    name_es: str | None = None
    name_en: str | None = None
    week_start: date | None = None


@dataclass(frozen=True, slots=True)
class ReportTotals:
    """The figures one table states, and the arithmetic that relates them.

    `billable_minutes` and `non_billable_minutes` are **strictly separated** and add
    up to `total_minutes`, which is the net — reversals are inside the two, on the
    side their original was on. `gross_minutes` and `reversal_minutes` are what the
    net was reached from, the same pair a day of the grid carries, so a reader can
    see "eight hours minus two" rather than a silent six.

    There is no rate, no amount and no currency: this system accumulates and exports
    *minutes* (DESIGN §7.3), and a money field here would be a payroll figure computed
    from nothing.
    """

    billable_minutes: int = 0
    non_billable_minutes: int = 0
    gross_minutes: int = 0
    reversal_minutes: int = 0
    #: How many entry rows the figures were summed from, and how many *approved
    #: weeks* they came from. Neither is a total of anything: they are the two numbers
    #: that make a figure explicable — "three thousand minutes over twelve weeks" is
    #: a different report from the same minutes over one — and they are exact in a
    #: grouped row and in the report's own total, because both are a `count(distinct)`
    #: and not a sum of counts.
    entries: int = 0
    weeks: int = 0

    @property
    def total_minutes(self) -> int:
        """The net: billable plus non-billable, reversals included."""
        return self.billable_minutes + self.non_billable_minutes


@dataclass(frozen=True, slots=True)
class ReportRow:
    """One line of the table: a group's totals, and what the group is.

    `dimensions` is in `group_by` order, so the same tuple identifies a line in the
    JSON, in the CSV and in a test — a report whose columns reshuffled between two
    reads could not be compared with itself.
    """

    dimensions: tuple[DimensionValue, ...]
    totals: ReportTotals


@dataclass(frozen=True, slots=True)
class ReportSummary:
    """The report as the API and the file both state it.

    `total` is a **second statement over the same predicate object**, not the rows
    added up and not a re-written filter. The invariant a reader relies on is that the
    totals row describes the same rows as the table above it; one shared `WHERE` is
    what makes that true by construction, while a hand-summed total would be true by
    arithmetic and a re-written filter would be true by coincidence. It is also what
    makes `total.weeks` exact — a `count(distinct)` over the whole period rather than
    the sum of the rows' counts, which would double-count a week that booked time on
    two projects.
    """

    filter: ReportFilter
    rows: tuple[ReportRow, ...]
    totals: ReportTotals


def assert_report_period(report_filter: ReportFilter) -> None:
    """Refuse a period the report cannot be about, with this module's own code.

    Two ways it is unusable, and they are one refusal because the remedy is one
    thing — ask for another period: the dates are inverted, or the range is wider
    than the record is kept for. Both are the caller's mistake rather than a state
    of the data, so the refusal names the bound in the detail.
    """
    if report_filter.to_date < report_filter.from_date or report_filter.days > MAX_REPORT_DAYS:
        raise DomainError(
            TimesheetErrorCode.TIMESHEET_REPORT_RANGE_INVALID,
            detail=(
                f"{report_filter.from_date}..{report_filter.to_date} is not a period this "
                f"report will state: at most {MAX_REPORT_DAYS} days, earliest first"
            ),
        )


__all__ = [
    "DEFAULT_GROUPING",
    "DIMENSIONS",
    "MAX_REPORT_DAYS",
    "DimensionValue",
    "ReportDimension",
    "ReportFilter",
    "ReportRow",
    "ReportSummary",
    "ReportTotals",
    "assert_report_period",
]
