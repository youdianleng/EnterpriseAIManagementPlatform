"""The missing list as a file: who finance has to chase, and nothing about what they earn.

The ticket's checklist line is 「缺失清单可导出，便于财务跟进」 and this module is that file.
Four decisions, three of which are about what the file *refuses* to carry:

* **The column contract is stated once, in `EXPORT_COLUMNS`.** Bilingual labels with the
  English second — the convention the attendance, overtime and timesheet exports
  established: the file is read in Spain by people who work in Spanish and read by whoever
  maintains this repository in English. The *values* stay language-neutral, so nothing has
  to be translated to be read.

* **`employee_no` is the first column, and the two exports that withhold it are the
  argument for including it here rather than against.** `timesheet/export.py` leaves the
  staff number out because a manager's report carries the same columns as HR's and the
  column would be empty for one and filled for the other; `overtime/export.py` includes it
  because finance owns the file. This export is finance's alone — `payslip.export` is the
  action, and the module refuses everybody else at the route — and the number is
  *precisely* what the file is for: it is what a payslip's filename is matched on, so a
  list of missing people that did not state it would send somebody hunting through the
  payroll system for a number the file could have told them.

* **There is no amount, no currency, no allowance and no salary figure, and
  `EXPORT_EXCLUDES` says so out loud.** The missing list is derived from the payroll
  archive, so this is the one file in the product where somebody would be tempted to add
  "and here is what they should have been paid". `docs/DESIGN.md` D9 makes 西班牙工资单计算
  an explicit non-goal, and §8.3 lists it among the things this system will not do; the
  file states the *dates* the salary record was in force, which is why the person is
  expected, and nothing about the figure.

* **No total line, because there is nothing to total.** The timesheet export ends with a
  `TOTAL` row because a reader sums minutes; a reader of this file counts rows, and a
  totals line over a list of names would be a number that means nothing. What the file
  does carry is the month in every row, so a finance mailbox holding several months can
  tell them apart after the filename is lost.

**The rows are the missing list, in the order the API serves it** — by staff number, then
name — so the file and the screen state the same list in the same order. A file that
reordered itself would make "row 3" mean two things.
"""

import csv
import io
from dataclasses import dataclass

from app.domain.payslip.models import ExportFile, MissingEmployee

#: The audit entity an export writes its trail under. A list rather than a row: the
#: question the trail answers is "who exported which month, when, and what did the file
#: state", and there is no single row the act belongs to (the same shape
#: `timesheet.export.EXPORT_ENTITY` and `overtime.export.EXPORT_ENTITY` use).
EXPORT_ENTITY = "payslip_missing_export"

#: The columns. See the module docstring for why the staff number leads and why the month
#: is repeated on every row.
EXPORT_COLUMNS: tuple[str, ...] = (
    "numero_empleado / employee_no",
    "empleado / employee",
    "departamento / department",
    "periodo / period",
    "retribucion_vigente_desde / salary_effective_from",
    "retribucion_vigente_hasta / salary_effective_to",
)

#: What the file deliberately leaves out, stated where the columns are — which is where
#: somebody tempted to add a money column would be looking.
EXPORT_EXCLUDES = (
    "amount",
    "base_salary",
    "currency",
    "components",
    "total",
    "net_pay",
    "tax",
    "social_security",
)


@dataclass(frozen=True, slots=True)
class ExportRow:
    """One missing employee, as the file states them.

    A separate value object from `MissingEmployee` so the export owns the *rendering*
    decision — which of the two department names, what a blank staff number looks like —
    and the domain value stays about the person.
    """

    employee_no: str
    employee_name: str
    department: str
    period: str
    salary_effective_from: str
    salary_effective_to: str


def filename(period: str) -> str:
    """What the download is called: the month it states, so two files cannot collide.

    ISO and nothing else, the convention every other export follows: a finance mailbox
    holds one missing list per period, and `nomina-faltantes-2026-03.csv` says which month
    it is without anybody opening it.
    """
    return f"nomina-faltantes-{period}.csv"


def rows(period: str, missing: tuple[MissingEmployee, ...]) -> list[ExportRow]:
    """The month's missing employees as the file's rows.

    A person with no staff number gets an empty first cell rather than being dropped: they
    are genuinely missing a payslip (`employee_private.employee_no` is nullable, and
    matching by filename cannot reach them at all), and a file that silently omitted them
    would make the list it is drawn from wrong.
    """
    return [
        ExportRow(
            employee_no=entry.employee.employee_no or "",
            employee_name=entry.employee.employee_name,
            department=entry.employee.department_name or "",
            period=period,
            salary_effective_from=entry.salary_effective_from.isoformat(),
            salary_effective_to=(
                entry.salary_effective_to.isoformat()
                if entry.salary_effective_to is not None
                else ""
            ),
        )
        for entry in missing
    ]


def render(period: str, missing: tuple[MissingEmployee, ...]) -> str:
    """The file, as text. Pure: a month and a list in, a string out.

    `csv` rather than a hand-built join, so a Spanish full name — which is written the
    official way round and therefore contains a comma — is quoted by the writer rather
    than by a rule somebody has to remember.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(EXPORT_COLUMNS)
    for row in rows(period, missing):
        writer.writerow(
            [
                row.employee_no,
                row.employee_name,
                row.department,
                row.period,
                row.salary_effective_from,
                row.salary_effective_to,
            ]
        )
    return buffer.getvalue()


def file_for(period: str, missing: tuple[MissingEmployee, ...]) -> ExportFile:
    """The rendered file with the name it is served under. One call, so the two agree."""
    return ExportFile(filename=filename(period), content=render(period, missing))


__all__ = [
    "EXPORT_COLUMNS",
    "EXPORT_ENTITY",
    "EXPORT_EXCLUDES",
    "ExportRow",
    "file_for",
    "filename",
    "render",
    "rows",
]
