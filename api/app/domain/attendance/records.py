"""Reading one's own record: the punches, the chain they form, and the file.

The Spanish working-time obligation has two halves that are not the day's snapshot:
the employee may **see** their own record, and the record has to be **producible** in
a form an accountant or a labour inspector can read. This module is both, and
neither is a new source of truth — the day comes from `AttendanceService` (the
snapshot if one was written, otherwise derived here and not written down), the
punches come from the stream, and the anomalies are ticket 23's rows.

Three decisions worth reading:

* **A punch is shown as a chain, not as a row.** What somebody asks is "what does
  my day say, and why does it say that" — so each punch travels with every
  correction that restated it, in the order they were written, and with the instant
  the day actually reads (`derivation.effective_punches`). A made-up punch — one
  approval appended for a shift nobody clocked — is a punch like any other and
  carries `source=correction`, which is how the screen knows to say so.
* **Nothing here has a lower date bound.** "Any historical date" is the ticket's
  requirement and four years is the obligation behind it, so the queries are bounded
  by the request and not by a retention cutoff: a date nobody has punched for two
  years is an answer (a day with no events), and a date six years ago is answered
  too. What *is* bounded is one request's width — `MAX_RANGE_DAYS`, the same
  constant the range read refuses on — because an export is a file about a period,
  and a period longer than the retention window is a request for the whole table.
* **The export is derived, and says so.** Every column is a fact the day's own
  arithmetic produced; the file is not a dump of the event stream. What it
  deliberately does not carry is in `EXPORT_COLUMNS` below — no staff number, no
  salary, no anomaly flags — because those are other modules' answers and a file
  that mixed them in would be a second, unauditable personnel record.
"""

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from app.domain.attendance.anomalies import Anomaly
from app.domain.attendance.anomaly_repository import AnomalyRepository
from app.domain.attendance.business_day import MADRID
from app.domain.attendance.derivation import corrections_of, effective_punches
from app.domain.attendance.models import (
    AttendanceEvent,
    DayRecord,
    EventSource,
)
from app.domain.attendance.repository import AttendanceRepository
from app.domain.attendance.service import AttendanceService

#: The header row, and the whole column contract of the export. Bilingual labels
#: with the English second: the file is read in Spain by people who work in
#: Spanish, and it is read by whoever maintains this repository in English, and a
#: single-language header would be wrong for one of the two. The *values* stay
#: language-neutral — ISO dates, Madrid-local ISO instants, whole minutes, and the
#: day statuses the module already publishes — so nothing in the file has to be
#: translated to be read.
#:
#: The last line of the file is a total, marked `TOTAL` in the first column: a range
#: total is a row like any other so that a spreadsheet sums the same numbers the
#: file states, and a parser skips one row rather than re-deriving the sum.
EXPORT_COLUMNS: tuple[str, ...] = (
    "empleado / employee",
    "empleado_id / employee_id",
    "fecha / date",
    "estado / status",
    "primera_entrada / first_in",
    "ultima_salida / last_out",
    "minutos_trabajados / worked_minutes",
    "minutos_esperados / expected_minutes",
    "minutos_extra / overtime_minutes",
)

#: What the export deliberately leaves out, stated where the columns are:
#:
#: * **No staff number and no salary.** Both live in the employee module's withheld
#:   block. A manager reading a report's attendance may not read that block, so the
#:   column would be empty for a manager's export and filled for HR's — a column
#:   that lies about the record depending on who produced it.
#: * **No anomaly flags.** The anomalies are a judgement with their own read and
#:   their own timestamps: tonight's scan may add one to a day in this file, and a
#:   correction approved tomorrow may clear it. A record whose contents depend on
#:   when it was printed is not the four-year record; the day's *status* is derived
#:   from the events themselves and is stable, which is why it is in.
#: * **No correction chain.** The chain is what the punches and the day read shows,
#:   one day at a time; the file states what each day came to.
EXPORT_EXCLUDES = ("staff_number", "salary", "anomalies", "correction_chain")

TOTAL_LABEL = "TOTAL"


@dataclass(slots=True, frozen=True)
class PunchChain:
    """One punch and everything that restated it, oldest first.

    `effective_at` is the instant the day reads, which is the newest correction's
    value — the derivation's answer, not a second computation: `effective_punches`
    is what the day's own numbers are built from, so the screen and the record
    cannot disagree about which correction is in force.
    """

    punch: AttendanceEvent
    effective_at: datetime
    corrections: tuple[AttendanceEvent, ...] = ()

    @property
    def is_corrected(self) -> bool:
        return bool(self.corrections)

    @property
    def is_made_up(self) -> bool:
        """Whether this punch exists because an approval appended it.

        A shift nobody clocked is made up by the correction flow rather than
        corrected, and the row says so by its source. The screen shows it; the
        export's status is derived from the events either way.
        """
        return self.punch.source is EventSource.CORRECTION


@dataclass(slots=True, frozen=True)
class DayDetail:
    """One person's one day: the punches, the chain, the day, and what is wrong.

    Complete for the question it answers. The derived day is the *stored* snapshot
    when there is one and a derivation when there is not, exactly as `day_view`
    answers it, and the anomalies are the rows ticket 23's pass wrote — resolved
    ones included, because "this was flagged and here is what cleared it" is part
    of reading a corrected day.
    """

    employee_id: UUID
    business_date: date
    day: DayRecord
    punches: tuple[PunchChain, ...] = ()
    anomalies: tuple[Anomaly, ...] = ()


@dataclass(slots=True, frozen=True)
class ExportFile:
    """A file, ready to be sent.

    The content is in memory rather than streamed, and that is not laziness: the
    range is bounded to `MAX_RANGE_DAYS`, so the largest file this can produce is
    four years of one person's days — a few hundred kilobytes — and streaming it
    would buy nothing but a way to fail halfway through a download. The totals a
    reader wants are *in* the file, on its last line, rather than beside it: a
    response header would be a second copy of a number the file already states.
    """

    filename: str
    content: str

    @property
    def content_type(self) -> str:
        return "text/csv; charset=utf-8"


class AttendanceRecords:
    """The day read and the export, over the module's own repositories.

    `attendance` is the service itself rather than a copy of its arithmetic: the day
    a reader sees here is the day the module has already agreed, snapshot first, and
    a second derivation beside it is exactly the drift `derivation.py` exists to
    prevent.
    """

    def __init__(
        self,
        punches: AttendanceRepository,
        attendance: AttendanceService,
        anomalies: AnomalyRepository,
    ) -> None:
        self._punches = punches
        self._attendance = attendance
        self._anomalies = anomalies

    # --- one day ------------------------------------------------------------

    async def day_detail(self, employee_id: UUID, business_date: date) -> DayDetail:
        """A day's punches, their chains, the derived day and its anomalies."""
        day = await self._attendance.day_view(employee_id, business_date)
        events = await self._punches.events_for_day(employee_id, business_date)
        return DayDetail(
            employee_id=employee_id,
            business_date=business_date,
            day=day,
            punches=tuple(chains(events)),
            anomalies=tuple(await self._anomalies.day_anomalies(employee_id, business_date)),
        )

    # --- the export ---------------------------------------------------------

    async def export(
        self, employee_id: UUID, from_date: date, to_date: date
    ) -> ExportFile:
        """The record for one person over one period, as a file.

        The range rules are `range_view`'s — ordered, at most `MAX_RANGE_DAYS`, and
        refused with the same catalogued code — because the export is a wider *view*
        of the same days and not a different question. Every day in the range is a
        row, including the ones nobody worked: a month with three days off is part
        of what the record has to show, and a file that listed only the days with
        punches would answer "what did they do" rather than "what does the record
        say".
        """
        days = await self._attendance.range_view(employee_id, from_date, to_date)
        name = await self._punches.employee_name(employee_id) or str(employee_id)
        return ExportFile(
            filename=_filename(employee_id, from_date, to_date),
            content=render(name, employee_id, days),
        )


def chains(events: list[AttendanceEvent]) -> list[PunchChain]:
    """One day's events, grouped into the chain each punch became.

    The order is `effective_punches`', so the screen lists shifts the way the day
    counted them. Every correction in the list belongs to some punch in the list —
    `events_for_day` groups a correction under its target's business date, so the
    target travelled with it, and the walk below therefore reaches every row rather
    than dropping the ones it cannot attach.
    """
    corrections = corrections_of(events)
    grouped: list[PunchChain] = []
    for punch, instant in effective_punches(events):
        lineage = [punch]
        # Breadth-first in write order: the chain is read oldest first, which is the
        # order it was argued about, and a hand-written row that branched would
        # still appear rather than being hidden behind the branch that won.
        pending = list(corrections.get(punch.id, ()))
        while pending:
            row = min(pending, key=lambda event: (event.created_at, str(event.id)))
            pending.remove(row)
            pending.extend(corrections.get(row.id, ()))
            lineage.append(row)
        grouped.append(
            PunchChain(punch=punch, effective_at=instant, corrections=tuple(lineage[1:]))
        )
    return grouped


def render(name: str, employee_id: UUID, days: list[DayRecord]) -> str:
    """The file, as text. Pure: values in, a string out.

    `csv` rather than a hand-built join, so a name with a comma in it — and every
    Spanish full name has one, because `employee_name` writes them the official way
    round — is quoted by the writer rather than by a rule somebody has to remember.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(EXPORT_COLUMNS)
    for record in days:
        writer.writerow(
            [
                name,
                str(employee_id),
                record.business_date.isoformat(),
                str(record.status),
                _local(record.first_in),
                _local(record.last_out),
                record.worked_minutes,
                _blank(record.expected_minutes),
                _blank(record.overtime_minutes),
            ]
        )
    if days:
        writer.writerow(
            [
                TOTAL_LABEL,
                "",
                f"{days[0].business_date}..{days[-1].business_date}",
                "",
                "",
                "",
                sum(record.worked_minutes for record in days),
                _blank(_total(record.expected_minutes for record in days)),
                _blank(_total(record.overtime_minutes for record in days)),
            ]
        )
    return buffer.getvalue()


def _filename(employee_id: UUID, from_date: date, to_date: date) -> str:
    """A name an accountant can file: ASCII, no spaces, and the period in it."""
    return f"attendance-{employee_id}-{from_date}-{to_date}.csv"


def _local(instant: datetime | None) -> str:
    """An instant as Madrid reads it — the calendar the record is kept in.

    The business date is Madrid by construction, so an inspector comparing a punch
    to the day it counts against has to see the same clock. The offset is written
    into every row: the same wall time is two different instants a year apart, and
    the two DST transitions are exactly what an audit of a night shift asks about.
    """
    return "" if instant is None else instant.astimezone(MADRID).isoformat()


def _blank(value: int | None) -> int | str:
    """Empty rather than zero for "nobody said".

    Zero is "the rules expect nobody to work today" and null is "no schedule reaches
    this person"; a file that wrote `0` for both would state something about
    somebody that nobody agreed to.
    """
    return "" if value is None else value


def _total(values) -> int | None:  # noqa: ANN001 - an iterable of optional ints
    """The range total, or nothing when no day in it has a figure.

    Empty rather than zero when nobody said, for the reason `_blank` gives: a total
    of `0` would state that the period expected nothing of anybody. A day without a
    figure contributes nothing to the sum — the alternative, dropping the whole
    total because one day in four years has no schedule, would answer a question
    nobody asked.
    """
    present = [value for value in values if value is not None]
    return sum(present) if present else None


__all__ = [
    "EXPORT_COLUMNS",
    "EXPORT_EXCLUDES",
    "TOTAL_LABEL",
    "AttendanceRecords",
    "DayDetail",
    "ExportFile",
    "PunchChain",
    "chains",
    "render",
]
