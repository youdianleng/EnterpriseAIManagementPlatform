"""Reading a holiday calendar out of a file, and refusing a bad one whole.

**The format is CSV, with a header row**, because the file an HR team actually has
is a spreadsheet, and `holidays_madrid_2026.csv` beside this module is the one that
ships as the example:

    date,name_es,name_en,scope,region_code
    2026-01-01,Año Nuevo,New Year's Day,national,
    2026-04-02,Jueves Santo,Maundy Thursday,regional,ES-MD
    2026-05-15,San Isidro,Saint Isidore,local,ES-MD

* `date` is ISO 8601. The `year` column the table carries is derived from it and is
  deliberately not in the file: a year that can be typed is a year that can
  disagree with the date beside it.
* `scope` is `national`, `regional` or `local`. National rows leave `region_code`
  empty; the other two require it.
* `region_code` is ISO 3166-2 (`ES-MD`), and for `local` rows the same code space,
  optionally down to the municipality (`ES-MD-28079`).

**Every row is validated before any is written, and one bad row refuses the file.**
The columns are checked, then each row, and the failure names the line numbers so
somebody can fix the spreadsheet in one pass. A holiday calendar that is half
loaded is a month's expected hours that is half wrong, and nothing downstream can
tell which half — which is why `ScheduleService.import_holidays` is also one
transaction.

The module is pure: text in, value objects out, no session. The command
(`app/jobs/import_holidays.py`) and any future upload endpoint share it, which is
what keeps the format documented in exactly one place.
"""

import csv
from datetime import date
from io import StringIO
from pathlib import Path

from app.domain.errors import DomainError
from app.domain.schedule.errors import ScheduleErrorCode
from app.domain.schedule.models import HolidayInput, HolidayScope

#: The header the file must carry, in this order. DictReader is used, so the order
#: only matters for the error message a human reads.
COLUMNS: tuple[str, ...] = ("date", "name_es", "name_en", "scope", "region_code")

#: A file with no header at all is the common mistake — a spreadsheet exported
#: without one — and it is worth its own sentence rather than "column date missing".
_MAX_REPORTED_ERRORS = 10


def parse_holidays_csv(text: str, *, source: str = "the file") -> tuple[HolidayInput, ...]:
    """Every row of a calendar, or a refusal naming every bad line."""
    reader = csv.DictReader(StringIO(text))
    header = tuple(reader.fieldnames or ())
    missing = [column for column in COLUMNS if column not in header]
    if missing:
        raise DomainError(
            ScheduleErrorCode.SCHEDULE_INVALID_HOLIDAY_FILE,
            detail=(
                f"{source} must start with the header {','.join(COLUMNS)}; "
                f"missing {', '.join(missing)}"
            ),
        )

    rows: list[HolidayInput] = []
    errors: list[str] = []
    seen: dict[tuple, int] = {}

    # Line 1 is the header, so the first data row is line 2.
    for number, raw in enumerate(reader, start=2):
        if _blank(raw):
            continue
        try:
            holiday = _row(raw, line=number)
        except DomainError as refusal:
            errors.append(str(refusal.detail or refusal))
            continue
        identity = (holiday.date, holiday.scope, holiday.region_code)
        if identity in seen:
            errors.append(
                f"line {number}: {holiday.date} {holiday.scope} "
                f"{holiday.region_code or ''} is already on line {seen[identity]}"
            )
            continue
        seen[identity] = number
        rows.append(holiday)

    if errors:
        shown = errors[:_MAX_REPORTED_ERRORS]
        more = len(errors) - len(shown)
        raise DomainError(
            ScheduleErrorCode.SCHEDULE_INVALID_HOLIDAY_FILE,
            detail=(
                f"{source} has {len(errors)} problem(s): "
                + "; ".join(shown)
                + (f"; and {more} more" if more else "")
            ),
        )
    if not rows:
        raise DomainError(
            ScheduleErrorCode.SCHEDULE_INVALID_HOLIDAY_FILE, detail=f"{source} has no holiday rows"
        )
    return tuple(rows)


def read_holidays_csv(path: Path) -> tuple[HolidayInput, ...]:
    """The same, from a path. Refuses a file that is not there as a bad file.

    A missing path is reported in the file's own vocabulary rather than as an
    `OSError`: the caller is a command whose whole job is "load this calendar", and
    "the file is not there" and "the file is wrong" are the same answer to whoever
    typed the path.
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as error:
        raise DomainError(
            ScheduleErrorCode.SCHEDULE_INVALID_HOLIDAY_FILE,
            detail=f"{path} could not be read: {error.strerror or error}",
        ) from error
    return parse_holidays_csv(text, source=str(path))


def _blank(raw: dict[str, str | None]) -> bool:
    return not any((value or "").strip() for value in raw.values())


def _row(raw: dict[str, str | None], *, line: int) -> HolidayInput:
    def field(name: str) -> str:
        return (raw.get(name) or "").strip()

    try:
        on_date = date.fromisoformat(field("date"))
    except ValueError as error:
        raise _bad(line, f"date {field('date')!r} is not an ISO date (2026-01-01)") from error

    try:
        scope = HolidayScope(field("scope").lower())
    except ValueError as error:
        values = ", ".join(item.value for item in HolidayScope)
        raise _bad(line, f"scope {field('scope')!r} is not one of {values}") from error

    name_es, name_en = field("name_es"), field("name_en")
    if not name_es or not name_en:
        raise _bad(line, "a holiday is named in both Spanish and English")

    region_code = field("region_code") or None
    if scope is HolidayScope.NATIONAL and region_code:
        raise _bad(line, f"{on_date} is national, so it names no region")
    if scope is not HolidayScope.NATIONAL and not region_code:
        raise _bad(line, f"{on_date} is {scope.value}, so it names its region (ES-MD)")

    return HolidayInput(
        date=on_date,
        name_es=name_es,
        name_en=name_en,
        scope=scope,
        region_code=region_code,
    )


def _bad(line: int, detail: str) -> DomainError:
    return DomainError(
        ScheduleErrorCode.SCHEDULE_INVALID_HOLIDAY_FILE, detail=f"line {line}: {detail}"
    )


__all__ = ["COLUMNS", "parse_holidays_csv", "read_holidays_csv"]
