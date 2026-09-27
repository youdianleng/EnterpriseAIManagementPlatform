"""Send each recipient one morning mail about the day before.

    python -m app.jobs.send_daily_digests [YYYY-MM-DD] [--retry]

Run it from cron at 08:00 Madrid — `docs/DESIGN.md` §7.1's "次日 08:00 → 汇总邮件给
经理" — from a systemd timer, or by hand:

    0 8 * * *  docker compose exec -T api python -m app.jobs.send_daily_digests

CRON_TZ is a cron daemon's setting rather than this program's, and a deployment
that has none passes the date explicitly. **Nothing here waits for 08:00**: the
date is an argument, the default is yesterday in Madrid, and a test drives any
date it likes without a clock. That is also what makes a failed morning
recoverable — the operator re-runs the same date, and the recipient who was
already mailed stays mailed (`daily_digests.sent_at`).

**`--retry` is for after the mail server came back.** It puts the day's unsent
digests back to zero attempts, so one that had given up after
`DIGEST_MAX_ATTEMPTS` is attempted again. It cannot reach a digest that was sent:
the one thing worse than a mail that did not arrive is a mail that arrived twice.

The exit code is 0 even when a message failed. A refusal is one recipient's mail
server having a bad morning; it is logged with the address it belongs to, it stays
visible as `failed` with its reason, and the next pass retries it. Exiting non-zero
would turn one broken address into a scheduler that reports failure every morning
for ever, which is how a real failure stops being noticed.
"""

import asyncio
import sys
from datetime import UTC, date, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import dispose_engine, get_session_factory
from app.domain.attendance.business_day import madrid_today
from app.domain.notification.digest_service import DailyDigest, DigestRunReport
from app.logging import configure_logging, get_logger
from app.mail import Mailer, build_mailer
from app.repositories.digest import PostgresDigestRepository

logger = get_logger(__name__)

RETRY_FLAG = "--retry"


def previous_day(today: date) -> date:
    """The day before: what the pass at 08:00 is about."""
    return today - timedelta(days=1)


def parse_arguments(arguments: list[str]) -> tuple[date, bool]:
    """The date to send, and whether the operator asked for a retry.

    A malformed date raises: the only person who types one is somebody
    deliberately re-examining a day, and they should be told they typed it wrong
    rather than handed yesterday's answer.
    """
    retry = RETRY_FLAG in arguments
    dates = [argument for argument in arguments if argument != RETRY_FLAG]
    if len(dates) > 1:
        raise SystemExit(
            f"usage: python -m app.jobs.send_daily_digests [YYYY-MM-DD] [{RETRY_FLAG}]"
        )
    return (
        date.fromisoformat(dates[0]) if dates else previous_day(madrid_today(datetime.now(UTC))),
        retry,
    )


def digest_service(session: AsyncSession, mailer: Mailer) -> DailyDigest:
    """The module, wired to PostgreSQL and to the configured sender."""
    settings = get_settings()
    return DailyDigest(
        PostgresDigestRepository(session),
        mailer,
        base_url=settings.web_base_url,
        default_language=settings.digest_default_language,
        max_attempts=settings.digest_max_attempts,
    )


async def send_day(
    digest_date: date, *, mailer: Mailer | None = None, retry: bool = False
) -> DigestRunReport:
    """One pass over one day, on its own session.

    `mailer` is injectable for the reason `apply_personnel_changes`'s `on_date` is:
    a test drives the whole pass — the query, the composition, the rows it writes —
    with a recording double in place of the one thing that cannot be exercised for
    real (see `app/mail.py`).
    """
    factory = get_session_factory()
    async with factory() as session:
        return await digest_service(session, mailer or build_mailer(get_settings())).send(
            digest_date, retry=retry
        )


async def main(argv: list[str] | None = None, *, mailer: Mailer | None = None) -> int:
    configure_logging(get_settings())
    digest_date, retry = parse_arguments(list(sys.argv[1:] if argv is None else argv))

    try:
        report = await send_day(digest_date, mailer=mailer, retry=retry)
        for failure in report.failed:
            logger.error(
                "daily_digest_not_sent",
                recipient_employee_id=str(failure.recipient_employee_id),
                email=failure.email,
                attempts=failure.attempts,
                error=failure.error,
            )
        logger.info(
            "daily_digests_sent",
            digest_date=digest_date,
            sent=report.sent_count,
            failed=report.failed_count,
            already_sent=report.already_sent,
            exhausted=report.exhausted,
            held=report.held,
            unrouted=len(report.unrouted),
        )
        if report.disabled:
            print(f"{digest_date}: mail is disabled, nothing sent ({report.held} rows held)")
        else:
            print(
                f"{digest_date}: {report.sent_count} digests sent, "
                f"{report.failed_count} failed, {report.already_sent} already sent"
            )
    finally:
        await dispose_engine()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
