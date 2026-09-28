"""The report as a file: minutes, and nothing else.

The ticket asks for the summary 导出为表格文件供财务或客户使用, and this module is that
file. Four decisions, three of which are about what the file *refuses* to carry:

* **The column contract is stated once, in `EXPORT_COLUMNS`.** Bilingual labels with
  the English second, the convention the attendance and overtime exports established:
  the file is read in Spain by people who work in Spanish and read by whoever
  maintains this repository in English. The *values* stay language-neutral — ISO
  dates, codes, whole minutes — so nothing has to be translated to be read.

* **There is no rate, no multiplier and no amount, and `EXPORT_EXCLUDES` says so out
  loud.** This system accumulates and exports hours (DESIGN §7.3); what an hour costs
  is finance's calculation. An export is exactly where somebody would be tempted to add
  a money column, so the decision is written down where they would look.

* **There is no staff number either, and that one is not an oversight.**
  `employee_no` is a withheld field: a manager reading their reports' hours occupies
  the report surface legitimately and has no right to it, so the column would be empty
  for a manager's export and filled for HR's — a column that lies about the record
  depending on who produced it, which is the argument
  `attendance/records.py` `EXPORT_EXCLUDES` already makes. The file therefore states
  exactly what the screen states, which is also why the *export needs no action of its
  own*: `timesheet.read_report` and `timesheet.read_all` are the whole permission, and
  a file is the same rows in another shape.

* **The last line is one total, marked `TOTAL` in the first column.** The reader sums
  a *period* — a month for finance, a project for a client — and the two minute
  columns are added separately because the whole point of the report is that billable
  and non-billable are not the same number. A parser skips one row rather than
  re-deriving the sum.

**The groupings are the columns, and the file is flat.** A report grouped by two
dimensions has two key columns and the ones it did not group by are empty, which is
what a spreadsheet expects: the header is the same file to file, and a reader who
asked for one grouping does not get a differently-shaped sheet.
"""

import csv
import io
from dataclasses import dataclass

from app.domain.timesheet.report import DIMENSIONS, DimensionValue, ReportSummary

#: The audit entity an export writes its trail under. A report rather than a row: the
#: question the trail answers is "who exported which period, when, and what did the
#: file state", and there is no single row the act belongs to (the overtime export's
#: `EXPORT_ENTITY` is the same shape for the same reason).
EXPORT_ENTITY = "timesheet_report"

#: The first four columns are the dimension keys, in `DIMENSIONS` order and under
#: bilingual labels; the next five are the figures. `minutos_totales` is stated even
#: though it is the sum of the two beside it, because it is the number a client reads
#: and the one the total line repeats — and because a reader who adds the two columns
#: and finds the third disagreeing has found a defect rather than done arithmetic.
EXPORT_COLUMNS: tuple[str, ...] = (
    "proyecto / project",
    "departamento / department",
    "empleado / employee",
    "semana / week",
    "minutos_facturables / billable_minutes",
    "minutos_no_facturables / non_billable_minutes",
    "minutos_totales / total_minutes",
    "minutos_brutos / gross_minutes",
    "minutos_anulados / reversed_minutes",
    "lineas / entries",
    "semanas / weeks",
)

#: What the file deliberately leaves out, stated where the columns are:
#:
#: * **No rate, no multiplier, no amount, no currency.** See the module docstring:
#:   hours are this system's subject and pay is finance's calculation.
#: * **No staff number.** A withheld field, and a manager's export would state it
#:   empty — see the module docstring.
#: * **No task, no note and no approver names.** The report is the summary; the task
#:   and the note are on the entry, where somebody reading one week wants them, and
#:   who approved a week is the approval engine's record. A second copy in a
#:   downloaded file is a copy that eventually disagrees with it.
EXPORT_EXCLUDES = (
    "rate",
    "multiplier",
    "amount",
    "currency",
    "staff_number",
    "task",
    "note",
    "approvers",
)

#: The label the totals line carries in its first column, and the shape the file
#: promises: a row of the right width is what makes it readable by a spreadsheet.
TOTAL_LABEL = "TOTAL"

#: The dimension columns, in the order `EXPORT_COLUMNS` states them. A tuple rather
#: than a slice of the header, so a column added to the figures cannot silently shift
#: which header a dimension lands under.
DIMENSION_COLUMNS: tuple[str, ...] = tuple(str(dimension) for dimension in DIMENSIONS)


@dataclass(slots=True, frozen=True)
class ExportFile:
    """A file, ready to be sent.

    In memory rather than streamed, for the reason the overtime export gives: a
    company's hours for a year are a few hundred kilobytes, and streaming would buy
    nothing but a way to fail halfway through a download.
    """

    filename: str
    content: str

    @property
    def content_type(self) -> str:
        return "text/csv; charset=utf-8"


def filename(summary: ReportSummary) -> str:
    """What the download is called: the period it states, so two files cannot collide.

    ISO dates and nothing else, and no grouping in the name: a finance mailbox holds
    one file per period, and `timesheet-report-2026-01-05-2026-03-29.csv` says which
    period it is without anybody having to open it.
    """
    return (
        f"timesheet-report-{summary.filter.from_date.isoformat()}"
        f"-{summary.filter.to_date.isoformat()}.csv"
    )


def render(summary: ReportSummary) -> str:
    """The file, as text. Pure: a summary in, a string out.

    `csv` rather than a hand-built join, so a name with a comma in it — and every
    Spanish full name has one, because the name is written the official way round —
    is quoted by the writer rather than by a rule somebody has to remember.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(EXPORT_COLUMNS)
    for row in summary.rows:
        writer.writerow([*_keys(row.dimensions), *_figures(row.totals)])
    if summary.rows:
        writer.writerow(
            [TOTAL_LABEL, *[""] * (len(DIMENSION_COLUMNS) - 1), *_figures(summary.totals)]
        )
    return buffer.getvalue()


def _keys(dimensions: tuple[DimensionValue, ...]) -> list[str]:
    """One row's four key columns, empty where the report did not group by one.

    Positional against `DIMENSION_COLUMNS`, and the value is the *code* where there is
    one and the period's Monday otherwise: the code is what a client's own systems
    name a project by, and a period has no code, so its key is the date it starts on.
    """
    by_kind = {value.kind: value for value in dimensions}
    keys: list[str] = []
    for dimension in DIMENSIONS:
        value = by_kind.get(dimension)
        if value is None:
            keys.append("")
        elif value.week_start is not None:
            keys.append(value.week_start.isoformat())
        else:
            keys.append(value.code or "")
    return keys


def _figures(totals) -> list[int]:  # noqa: ANN001 - ReportTotals
    """The five minute columns and the two counts, in `EXPORT_COLUMNS` order."""
    return [
        totals.billable_minutes,
        totals.non_billable_minutes,
        totals.total_minutes,
        totals.gross_minutes,
        totals.reversal_minutes,
        totals.entries,
        totals.weeks,
    ]


__all__ = [
    "DIMENSION_COLUMNS",
    "EXPORT_COLUMNS",
    "EXPORT_ENTITY",
    "EXPORT_EXCLUDES",
    "TOTAL_LABEL",
    "ExportFile",
    "filename",
    "render",
]
