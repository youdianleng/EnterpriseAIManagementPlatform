"""Snapshot every employee's expected hours for a month that has ended.

    python -m app.jobs.snapshot_expected_hours [YYYY-MM]

Run it from cron on the first of the month, or from the worker container. It is the
job `docs/DESIGN.md` §2.1 names ("月度应出勤快照"), and the reason it is a job rather
than something a request does: freezing a month is what makes the figure evidence,
and it has to happen whether or not anybody opened a page.

The default month is the one that has just ended, in Madrid terms. Snapshotting the
*current* month would freeze a figure that is still being decided — an override
somebody is about to add, a holiday HR is about to correct — and a frozen figure
that is wrong is worse than an unfrozen one that is right.

**Idempotent.** A month whose stored revision already carries the inputs the pass
computes again is left alone (`ScheduleService.snapshot_month`), so running this
twice, or running it again after a crash, appends nothing and rewrites nothing. A
month whose inputs have moved appends a new revision; the earlier one stays
readable with the figure it produced, which is the whole point of the table.

The exit code is 0 even when somebody could not be snapshotted: that is a data
problem for HR, it is logged with the employee it belongs to, and the next pass
retries it. Exiting non-zero would turn one uncomputable person into a scheduler
that reports failure every night for ever.
"""

import asyncio
import sys
from datetime import UTC, date, datetime

from app.config import get_settings
from app.db import dispose_engine, get_session_factory
from app.domain.attendance.business_day import madrid_today
from app.domain.schedule.service import ScheduleService
from app.logging import configure_logging, get_logger
from app.repositories.schedule import PostgresScheduleRepository

logger = get_logger(__name__)


def previous_month(today: date) -> tuple[int, int]:
    """The month before the one `today` falls in."""
    if today.month == 1:
        return today.year - 1, 12
    return today.year, today.month - 1


def parse_month(argument: str) -> tuple[int, int]:
    """`YYYY-MM` as a pair. A malformed argument raises, which is right: the only
    person who types one is somebody asking for a specific month on purpose."""
    year, month = argument.split("-")
    return int(year), int(month)


async def snapshot_month(year: int, month: int) -> dict[str, int]:
    """One pass: how many months were frozen, and how many could not be."""
    factory = get_session_factory()
    async with factory() as session:
        service = ScheduleService(PostgresScheduleRepository(session), session)
        report = await service.snapshot_all(year, month)
    for failure in report.failed:
        logger.error(
            "expected_hours_not_snapshotted",
            employee_id=str(failure.employee_id),
            year=year,
            month=month,
            code=failure.code,
            detail=failure.detail,
        )
    return {"written": report.written, "failed": len(report.failed)}


async def main(argv: list[str] | None = None) -> int:
    configure_logging(get_settings())
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments:
        year, month = parse_month(arguments[0])
    else:
        year, month = previous_month(madrid_today(datetime.now(UTC)))

    report = await snapshot_month(year, month)
    await dispose_engine()
    logger.info("expected_hours_snapshotted", year=year, month=month, **report)
    print(f"{year}-{month:02d}: {report['written']} snapshotted, {report['failed']} failed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
