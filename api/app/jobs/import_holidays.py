"""Import a holiday calendar from a CSV file.

    python -m app.jobs.import_holidays app/data/holidays_madrid_2026.csv

The format is documented in `app/domain/schedule/importer.py`, next to the sample
it ships with. Three properties this command has, and each is the reason for a
line of code below:

* **Idempotent.** Rows are matched on their identity — date, scope and region — so
  re-importing the same file reports every row `unchanged` and writes nothing. A
  calendar is re-imported whenever it is corrected, so a second run that duplicated
  anything would be a second run nobody dares make.
* **Whole or nothing.** Parsing refuses the file if any row is wrong, naming the
  lines; the write is one transaction. A half-loaded calendar is a month's expected
  hours that is half wrong.
* **Loud about what it did.** The counts go to the log and to stdout, because the
  next question is always "did that run, and what did it change".

The exit code is the distinction the caller needs: 0 when the calendar was read
and written, 2 when the file itself could not be used. Nothing else fails — a
database that is down raises, which is a different problem from a bad file and
deserves a different code.
"""

import asyncio
import sys
from pathlib import Path

from app.config import get_settings
from app.db import dispose_engine, get_session_factory
from app.domain.errors import DomainError
from app.domain.schedule.importer import read_holidays_csv
from app.domain.schedule.service import ScheduleService
from app.logging import configure_logging, get_logger
from app.repositories.schedule import PostgresScheduleRepository

logger = get_logger(__name__)

#: The default file, so the common case is one argument — or none.
SAMPLE = Path(__file__).resolve().parents[1] / "data" / "holidays_madrid_2026.csv"

BAD_FILE_EXIT = 2


def build_service(session) -> ScheduleService:  # noqa: ANN001 - AsyncSession
    return ScheduleService(PostgresScheduleRepository(session), session)


async def import_file(path: Path) -> dict[str, int]:
    """Read a calendar and write it. Raises `DomainError` for a bad file."""
    rows = read_holidays_csv(path)
    factory = get_session_factory()
    async with factory() as session:
        # The service audits the import with its counts and, here, the file they
        # came from — which is the fact it cannot know by itself.
        report = await build_service(session).import_holidays(rows, source=str(path))
    return {
        "created": report.created,
        "updated": report.updated,
        "unchanged": report.unchanged,
        "total": report.total,
    }


async def main(argv: list[str] | None = None) -> int:
    configure_logging(get_settings())
    arguments = list(sys.argv[1:] if argv is None else argv)
    path = Path(arguments[0]) if arguments else SAMPLE
    try:
        counts = await import_file(path)
    except DomainError as refusal:
        logger.error("holiday_import_refused", source=str(path), detail=refusal.detail)
        print(f"refused: {refusal.detail}")
        return BAD_FILE_EXIT
    finally:
        await dispose_engine()

    logger.info("holidays_imported", source=str(path), **counts)
    print(
        f"{path}: {counts['created']} created, {counts['updated']} updated, "
        f"{counts['unchanged']} unchanged, {counts['total']} rows"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
