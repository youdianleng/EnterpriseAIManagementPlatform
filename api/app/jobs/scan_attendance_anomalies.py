"""Find what a day is missing, and remind the people it belongs to.

    python -m app.jobs.scan_attendance_anomalies [YYYY-MM-DD]

Run it from cron shortly after midnight, or from the worker container. It is the
nightly pass `docs/DESIGN.md` §7.1 names, and it runs in **one pass with two
phases** rather than as two commands:

* the *scan* derives the day's anomalies from the event stream and the schedule and
  records them;
* the *reminder* tells each employee about their own outstanding ones and stamps
  them, so a second run reminds nobody twice.

One command because the reminder is about rows the scan has just written: two cron
entries are two things to install, and the half that gets left out would be the scan
— leaving a reminder pass that finds nothing, for ever, while looking healthy. A
single pass also cannot remind anybody about a day it did not examine.

**The default date is yesterday in Madrid**, which is the day that has just ended
when the pass runs. `date.fromisoformat` reads the optional argument, and a
malformed one raises: the only person who types a date is somebody deliberately
re-examining a day, and they should be told they typed it wrong rather than handed
yesterday's answer. Re-running it with a date is how a day somebody has since
corrected is brought up to date; it is idempotent, so nothing is written twice.

**Nobody is reminded about a day with no anomalies**, and a holiday, a rest day and
a day of approved leave produce none: the rules are in
`domain/attendance/anomalies.py`, and the leave half is ticket 25's `LeaveCalendar`,
which the endpoints build the same way.

**Both notifications are digest candidates** (ticket 20): the reminder is raised
in-app immediately and its email row waits at `pending`, so an employee opening the
application at the start of their day finds it, and the 08:00 digest carries the
mail. Which types the digest selects is
`notification.models.DIGEST_CANDIDATE_TYPES`, not a rule in this file.

The exit code is 0 even when somebody's day could not be examined or a notification
could not be raised. Those are data problems; they are logged with the employee they
belong to, and the next pass retries them. Exiting non-zero would turn one broken
row into a scheduler that reports failure every night for ever, which is how a real
failure stops being noticed.
"""

import asyncio
import sys
from datetime import UTC, date, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import dispose_engine, get_session_factory
from app.domain.approval.service import ApprovalService
from app.domain.attendance.anomalies import AnomalyReminderReport, AnomalyScanReport
from app.domain.attendance.anomaly_service import AnomalyService
from app.domain.attendance.business_day import madrid_today
from app.domain.attendance.notify import AnomalyReminder
from app.domain.leave.service import LeaveCalendar, LeaveService
from app.domain.notification.approval import ApprovalNotifier
from app.domain.notification.service import NotificationService
from app.domain.schedule.service import ScheduleService
from app.logging import configure_logging, get_logger
from app.repositories.approval import PostgresApprovalRepository
from app.repositories.attendance import PostgresAnomalyRepository
from app.repositories.leave import PostgresLeaveRepository
from app.repositories.notification import PostgresNotificationRepository
from app.repositories.schedule import PostgresScheduleRepository

logger = get_logger(__name__)


def previous_day(today: date) -> date:
    """The day before, which is the day a pass running after midnight is about."""
    return today - timedelta(days=1)


def anomaly_service(session: AsyncSession) -> AnomalyService:
    """The module, with the scheduling module and the leave calendar behind it.

    Built here rather than inside the service, exactly as the attendance endpoints
    build it: the scan asks "what did the schedule expect" and "is this person on
    leave", and it never learns what a schedule or a leave request is. The leave half
    is ticket 25's `LeaveLookup`, which replaces ticket 23's `AssumeNoLeave`; the
    endpoints build the same object, so a day a correction re-examines and a day the
    nightly pass examines are judged against the same calendar.
    """
    return AnomalyService(
        PostgresAnomalyRepository(session),
        expectations=ScheduleService(PostgresScheduleRepository(session), session),
        leave=LeaveCalendar(PostgresLeaveRepository(session)),
    )


def leave_service(session: AsyncSession) -> LeaveService:
    """The leave module, wired as the leave endpoints wire it."""
    approvals = PostgresApprovalRepository(session)
    return LeaveService(
        PostgresLeaveRepository(session),
        session,
        expectations=ScheduleService(PostgresScheduleRepository(session), session),
        approvals=ApprovalNotifier(
            engine=ApprovalService(approvals, session),
            notifications=NotificationService(PostgresNotificationRepository(session), session),
            approvals=approvals,
        ),
        annual_leave_days=get_settings().annual_leave_days,
    )


async def scan_day(business_date: date) -> AnomalyScanReport:
    """Phase one: record what this day is missing, for everybody it was owed by.

    **Balances are settled first.** A leave the engine approved and a crash left
    unsettled is not yet *in force* as far as the calendar is concerned, and the pass
    would flag the day as an absence for somebody who is away. The settle is
    idempotent and touches only documents whose engine answer is already in, so the
    ordinary case is one indexed query and nothing written.
    """
    factory = get_session_factory()
    async with factory() as session:
        await leave_service(session).settle_decided()
        report = await anomaly_service(session).scan(business_date)
    for failure in report.failed:
        logger.error(
            "attendance_anomaly_not_recorded",
            employee_id=str(failure.employee_id),
            business_date=business_date,
            code=failure.code,
            detail=failure.detail,
        )
    return report


async def remind_day(business_date: date) -> AnomalyReminderReport:
    """Phase two: tell each employee about their own, on its own session.

    A fresh session because the scan has committed by now and every notification
    commits on its own: a reminder that fails half way through a company must leave
    the ones already sent stamped and sent, not rolled back into a second attempt.
    """
    factory = get_session_factory()
    async with factory() as session:
        reminder = AnomalyReminder(
            anomaly_service(session),
            NotificationService(PostgresNotificationRepository(session), session),
        )
        report = await reminder.remind(business_date)
    for failure in report.failed:
        logger.error(
            "attendance_anomaly_not_reminded",
            employee_id=str(failure.employee_id),
            business_date=business_date,
            code=failure.code,
            detail=failure.detail,
        )
    return report


async def main(argv: list[str] | None = None) -> int:
    configure_logging(get_settings())
    arguments = list(sys.argv[1:] if argv is None else argv)
    business_date = (
        date.fromisoformat(arguments[0])
        if arguments
        else previous_day(madrid_today(datetime.now(UTC)))
    )

    try:
        scanned = await scan_day(business_date)
        logger.info(
            "attendance_anomalies_scanned",
            business_date=business_date,
            examined=scanned.examined,
            created=scanned.created_count,
            existing=scanned.existing,
            failed=len(scanned.failed),
        )
        reminded = await remind_day(business_date)
        logger.info(
            "attendance_anomalies_reminded",
            business_date=business_date,
            reminded=reminded.reminded_count,
            duplicates=reminded.duplicates,
            failed=len(reminded.failed),
        )
        print(
            f"{business_date}: {scanned.created_count} anomalies for "
            f"{scanned.examined} employees, {reminded.reminded_count} reminded"
        )
    finally:
        await dispose_engine()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
