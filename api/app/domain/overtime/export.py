"""The month as a file: minutes, and nothing else.

The ticket asks for one report — 员工编号、姓名、部门、日期、审批时长、确认时长 — and this
module is that report and nothing more. The two decisions worth reading:

* **The column contract is stated here, once, in `EXPORT_COLUMNS`.** Bilingual labels
  with the English second, the convention the attendance export established: the file
  is read in Spain by people who work in Spanish and read by whoever maintains this
  repository in English, so a single-language header would be wrong for one of them.
  The *values* stay language-neutral — ISO dates, staff numbers, whole minutes — so
  nothing in the file has to be translated to be read.
* **There is no rate, no multiplier and no amount, and `EXPORT_EXCLUDES` says so out
  loud.** What an hour costs is finance's calculation (Q12): this system accumulates
  and exports hours. A `rate` column here would be a second payroll record, computed
  from a convenio that changes without this repository hearing about it, and the
  export is the place somebody would be tempted to add one — so the decision is
  written down where they would look.

**The last line of the file is a single total, not one per employee.** The ticket allows
either; one at the end is chosen because the reader sums a *period* — the month's
payroll — and a per-employee total would be a row whose first column is a staff number
and whose second is the word TOTAL, which a spreadsheet then has to be told to ignore.
The total line is marked `TOTAL` in the first column, so a parser skips one row rather
than re-deriving the sum, and the two minute columns are added: approved minutes and the
minutes in force.
"""

import csv
import io
from dataclasses import dataclass

from app.domain.overtime.models import OvertimeExportRow

#: The audit entity an export writes its trail under. A period rather than a row: the
#: question the trail answers is "who exported March, when, and how many lines did it
#: state", and there is no single row the act belongs to.
EXPORT_ENTITY = "overtime_export"

#: The header row, and the whole column contract of the file. Six columns, and the
#: ticket names all six: the employee the hours belong to, the day they were worked, and
#: the two figures — what two people approved in advance, and what the day came to once
#: it was compared with the actual punches (HR's confirmation when there is one, the
#: settled smaller-of when there is not, and empty for a day that is still open).
EXPORT_COLUMNS: tuple[str, ...] = (
    "numero_empleado / employee_no",
    "empleado / employee",
    "departamento / department",
    "fecha / date",
    "minutos_aprobados / approved_minutes",
    "minutos_confirmados / confirmed_minutes",
)

#: What the file deliberately leaves out, stated where the columns are:
#:
#: * **No rate, no multiplier, no amount.** See the module docstring: overtime pay is
#:   finance's calculation, and a column here would be a payroll figure this system
#:   cannot keep correct.
#: * **No reason.** 事由 is on the request, where the approvers read it; a month's file
#:   is a list of hours, and free text about why somebody stayed late is not what a
#:   payroll reader asked for.
#: * **No approver names.** Who approved a day is the approval engine's record, and a
#:   second copy in a downloaded file is a copy that eventually disagrees with it.
#: * **No confirmation flag.** Whether HR has looked at a record is a queue, and a queue
#:   is a screen rather than a file: the *figure* in the confirmed column is what the
#:   file states, and a month is exported when the queue is empty.
EXPORT_EXCLUDES = ("rate", "multiplier", "amount", "reason", "approvers", "needs_confirmation")

#: The label the total line carries in the first column, and the column count the file
#: promises: a row of the right width is what makes it readable by a spreadsheet.
TOTAL_LABEL = "TOTAL"


@dataclass(slots=True, frozen=True)
class ExportFile:
    """A file, ready to be sent.

    The content is in memory rather than streamed, for the reason the attendance export
    gives: a month of one company's overtime is a few dozen kilobytes, and streaming it
    would buy nothing but a way to fail halfway through a download.
    """

    filename: str
    content: str

    @property
    def content_type(self) -> str:
        return "text/csv; charset=utf-8"


def render(rows: list[OvertimeExportRow]) -> str:
    """The file, as text. Pure: values in, a string out.

    `csv` rather than a hand-built join, so a name with a comma in it — and every
    Spanish full name has one, because the name is written the official way round — is
    quoted by the writer rather than by a rule somebody has to remember.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(EXPORT_COLUMNS)
    for row in rows:
        writer.writerow(
            [
                row.employee_no or "",
                row.employee_name,
                row.department or "",
                row.business_date.isoformat(),
                row.approved_minutes,
                "" if row.confirmed_minutes is None else row.confirmed_minutes,
            ]
        )
    if rows:
        writer.writerow(
            [
                TOTAL_LABEL,
                "",
                "",
                "",
                _sum(row.approved_minutes for row in rows),
                _total(rows),
            ]
        )
    return buffer.getvalue()


def _sum(values) -> int:  # noqa: ANN001 - an iterable of ints
    """The approved minutes of the period: a figure, always present."""
    return sum(values)


def _total(rows: list[OvertimeExportRow]) -> int | str:
    """The minutes in force, or empty when no line has a figure yet.

    Empty rather than zero when nobody has said, the convention the attendance export
    established: a total of `0` would state that the period came to nothing, while a
    period whose days are all still open simply has no confirmed figure yet.
    """
    present = [row.confirmed_minutes for row in rows if row.confirmed_minutes is not None]
    return sum(present) if present else ""


__all__ = [
    "EXPORT_COLUMNS",
    "EXPORT_ENTITY",
    "EXPORT_EXCLUDES",
    "TOTAL_LABEL",
    "ExportFile",
    "render",
]
