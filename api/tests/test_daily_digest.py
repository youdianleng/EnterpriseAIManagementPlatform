"""Ticket 20: the daily digest, the mail seam, and what a clean day gets.

No mocks and no in-memory repository, for the reason every other module here gives:
what this ticket has to get right is largely a *query and a constraint* — which
recipient a report's anomalies resolve to, whether a second run of the same day
writes a second mail, whether a queued delivery row moves only when a mail actually
went out, whether the database refuses a digest with nothing to report — and a
substitute would answer those with the test's own assumptions. The one double is
`app/mail.py`'s transport, which is a seam by design: nothing in this file reaches a
socket, and Mailpit's part is verified against the running stack instead.

**Every date is fixed.** A Monday and the Tuesday after it, a department that works
Monday to Friday, punches built by hand: nothing here depends on the day the suite
runs on, and nothing waits for 08:00 — the job takes the date it is about, which is
what the ticket's "triggerable without waiting" line asks for.

**The line most easily lost has the bluntest test.** `test_a_clean_day_gets_no_mail`
asserts the empty mail list, the absent digest row *and* the reason left on the
queued delivery row, because "avoid daily noise" is a rule about what does *not*
happen.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, time
from uuid import UUID

import pytest
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.domain.attendance.anomalies import AnomalyType
from app.domain.attendance.anomaly_service import AnomalyService
from app.domain.attendance.business_day import MADRID, madrid_today
from app.domain.attendance.models import EventSource, EventType, NewEvent, utc_now
from app.domain.attendance.notify import AnomalyReminder, AttendanceNotifier
from app.domain.attendance.service import AttendanceService
from app.domain.notification.digest import (
    DIGEST_COPY,
    DigestAnomaly,
    DigestContent,
    DigestLanguage,
    DigestReport,
    language_of,
    render,
)
from app.domain.notification.digest_service import (
    MAIL_DISABLED,
    NOTHING_TO_REPORT,
    DailyDigest,
)
from app.domain.notification.models import NOT_ATTEMPTED
from app.domain.notification.service import NotificationService
from app.domain.schedule.models import ScheduleDayInput, ScheduleInput
from app.domain.schedule.service import ScheduleService
from app.jobs.send_daily_digests import RETRY_FLAG, main, parse_arguments, previous_day
from app.mail import Mailer, MailError, MailMessage, SmtpMailer, build_envelope, build_mailer
from app.repositories.attendance import (
    PostgresAnomalyRepository,
    PostgresAttendanceRepository,
)
from app.repositories.digest import PostgresDigestRepository
from app.repositories.notification import PostgresNotificationRepository
from app.repositories.schedule import PostgresScheduleRepository
from tests.support.platform import Platform

#: A Monday and the Tuesday after it. Fixed rather than "today": a test whose
#: result depends on the day it runs on is not evidence.
MONDAY = date(2026, 9, 21)
TUESDAY = date(2026, 9, 22)

OPENS = time(9, 0)
CLOSES = time(17, 0)
FULL_DAY = 480

#: What `WEB_BASE_URL` defaults to, asserted through the link in both parts.
BASE_URL = "http://localhost:3000"

#: What a relay having a bad morning says, verbatim, onto the delivery row.
REFUSAL = "SMTPRecipientsRefused: 451 4.3.0 try again later"


def at(day: date, hour: int, minute: int = 0) -> datetime:
    """An instant as somebody in Madrid would say it, through `ZoneInfo`."""
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=MADRID)


# --- the seam's doubles -------------------------------------------------------


class RecordingMailer:
    """What would have gone out, and nothing else.

    The seam `app/mail.py` opens: the suite drives the whole pass — the query, the
    composition, the rows it writes — with this in place of a socket.
    """

    def __init__(self, *, enabled: bool = True) -> None:
        self._enabled = enabled
        #: Every message the digest handed over, in order. For a refusing mailer
        #: this is the record of the *attempts*, which is what a retry test reads.
        self.messages: list[MailMessage] = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def send(self, message: MailMessage) -> None:
        self.messages.append(message)

    def to(self, email: str) -> list[MailMessage]:
        return [message for message in self.messages if message.to == email]


class RefusingMailer(RecordingMailer):
    """A relay that takes the message and refuses it, with a reason worth recording."""

    async def send(self, message: MailMessage) -> None:
        self.messages.append(message)
        raise MailError(REFUSAL)


# --- the services, on their own sessions, the way the job uses them -----------


@asynccontextmanager
async def notifier(platform: Platform) -> AsyncIterator[AttendanceNotifier]:
    async with platform.factory() as session:
        repository = PostgresAttendanceRepository(session)
        yield AttendanceNotifier(
            AttendanceService(
                repository,
                expectations=ScheduleService(PostgresScheduleRepository(session), session),
            ),
            NotificationService(PostgresNotificationRepository(session), session),
            repository,
        )


def _anomalies(session) -> AnomalyService:  # noqa: ANN001 - AsyncSession
    return AnomalyService(
        PostgresAnomalyRepository(session),
        expectations=ScheduleService(PostgresScheduleRepository(session), session),
        now=utc_now,
    )


@asynccontextmanager
async def scanned(platform: Platform) -> AsyncIterator[AnomalyService]:
    """The scan alone: what a mail has to say when nobody was told in-app."""
    async with platform.factory() as session:
        yield _anomalies(session)


@asynccontextmanager
async def reminder_pass(
    platform: Platform,
) -> AsyncIterator[tuple[AnomalyService, AnomalyReminder]]:
    """The scan and the reminder, wired as `scan_attendance_anomalies` wires them."""
    async with platform.factory() as session:
        service = _anomalies(session)
        yield service, AnomalyReminder(
            service,
            NotificationService(PostgresNotificationRepository(session), session),
        )


@asynccontextmanager
async def digests(platform: Platform, mailer: Mailer) -> AsyncIterator[DailyDigest]:
    """The module on its own session, with the transport replaced."""
    async with platform.factory() as session:
        yield DailyDigest(
            PostgresDigestRepository(session), mailer, base_url=BASE_URL
        )


# --- the organisation a digest has something to say about ---------------------


async def works_here(
    platform: Platform,
    *,
    code: str = "OPS",
    employee_id: str | None = None,
    manager_employee_id: str | None = None,
    notification_override_employee_id: str | None = None,
    department_manager: str | None = None,
    schedule: bool = True,
) -> UUID:
    """A department, a position, a schedule, and somebody assigned to it.

    The schedule belongs to the department rather than to the company default on
    purpose: the scan examines everybody a schedule reaches, and a default would
    also reach the accounts these tests create for other reasons.
    """
    department = await platform.department(code)
    position = await platform.position(department, f"{code}-P")
    subject = employee_id or await platform.employee()
    await platform.assign(
        subject,
        department,
        position,
        manager_employee_id=manager_employee_id,
        notification_override_employee_id=notification_override_employee_id,
    )
    if department_manager is not None:
        if UUID(department_manager) != UUID(subject):
            await platform.assign(department_manager, department, position)
        appointer = await platform.account(roles=("admin",))
        response = await appointer.call(
            "PUT",
            f"/api/v1/departments/{department}/manager",
            json={"employee_id": department_manager},
        )
        assert response.status_code == 200, response.text
    if schedule:
        async with platform.factory() as session:
            await ScheduleService(PostgresScheduleRepository(session), session).create_schedule(
                ScheduleInput(
                    code=f"{code}-WEEK",
                    name_es="Semana",
                    name_en="Week",
                    days=tuple(
                        ScheduleDayInput(
                            weekday=weekday,
                            expected_minutes=FULL_DAY,
                            start_time=OPENS,
                            end_time=CLOSES,
                        )
                        for weekday in range(5)
                    ),
                    department_id=UUID(department),
                )
            )
    return UUID(subject)


async def worked(
    platform: Platform, employee_id: UUID, day: date, *, start: int = 9, end: int = 17
) -> None:
    """A shift, punched through the notifier the endpoints use."""
    async with notifier(platform) as service:
        await service.clock(employee_id, EventType.CLOCK_IN, at(day, start), EventSource.WEB)
        await service.clock(employee_id, EventType.CLOCK_OUT, at(day, end), EventSource.WEB)


async def late_and_early(platform: Platform, employee_id: UUID, day: date) -> None:
    """A day with two anomalies and both kinds of queued notification.

    In at 09:30 and out at 16:30 against a 09:00–17:00 window: late *and* left
    early. The clock-out raises the manager's notification and the reminder pass
    raises the employee's, so a digest built from either source alone would list
    the same two facts — which is what makes the dedupe assertable.
    """
    async with notifier(platform) as service:
        await service.clock(employee_id, EventType.CLOCK_IN, at(day, 9, 30), EventSource.WEB)
        await service.clock(employee_id, EventType.CLOCK_OUT, at(day, 16, 30), EventSource.WEB)


async def forgotten_clock_out(platform: Platform, employee_id: UUID, day: date) -> None:
    """A shift nobody closed: one anomaly, and no clock-out notification to anybody."""
    async with notifier(platform) as service:
        await service.clock(employee_id, EventType.CLOCK_IN, at(day, 9), EventSource.WEB)


async def email_of(platform: Platform, employee_id: UUID) -> str:
    address = await platform.scalar(
        "SELECT email FROM employees WHERE id = :id", {"id": employee_id}
    )
    assert address is not None
    return str(address)


async def delivery_rows(platform: Platform, recipient: UUID | None = None) -> list[tuple]:
    """Email delivery tracking: status, attempts, reason, time.

    `recipient` narrows it to what one person was addressed; None is the whole
    queue, which is what a test about the switch or about a clean day asks for.
    """
    where = "AND n.recipient_employee_id = :id" if recipient is not None else ""
    return await platform.sql(
        "SELECT d.status, d.attempts, d.error, d.sent_at FROM notification_deliveries d "
        "JOIN notifications n ON n.id = d.notification_id "
        f"WHERE d.channel = 'email' {where} ORDER BY n.type",
        {"id": recipient} if recipient is not None else None,
    )


async def digest_rows(platform: Platform, recipient: UUID | None = None) -> list[tuple]:
    """The digest rows: recipient, date, count, attempts, reason, sent time."""
    where = "WHERE recipient_employee_id = :id" if recipient is not None else ""
    return await platform.sql(
        "SELECT recipient_employee_id, digest_date, anomaly_count, attempts, error, sent_at "
        f"FROM daily_digests {where} ORDER BY recipient_employee_id, digest_date",
        {"id": recipient} if recipient is not None else None,
    )


# --- the copy, and the two parts of one message -------------------------------


def test_every_anomaly_kind_is_named_in_both_languages() -> None:
    """The catalogue cannot half-exist, and a sixth kind cannot ship unnamed.

    The backend writes a sentence exactly here, because a mail client has no
    dictionary to resolve a key against; walking the anomaly catalogue is what makes
    "we added a kind and forgot the mail" a failing test rather than a blank line in
    somebody's inbox.
    """
    spanish, english = DIGEST_COPY[DigestLanguage.ES], DIGEST_COPY[DigestLanguage.EN]

    assert set(spanish) == set(english)
    for language in DigestLanguage:
        for kind in AnomalyType:
            assert DIGEST_COPY[language][f"anomaly.{kind}"].strip(), (language, kind)


def test_the_language_is_the_stored_preference_and_then_the_default() -> None:
    """DESIGN §10.4's preference when there is one, the deployment's language otherwise.

    An unrecognised stored value falls through instead of raising: one account with
    odd data must not stop the other ninety-nine people's morning.
    """
    assert language_of(None, default="es") is DigestLanguage.ES
    assert language_of("en", default="es") is DigestLanguage.EN
    assert language_of("ES", default="en") is DigestLanguage.ES
    assert language_of("fr", default="es") is DigestLanguage.ES
    assert language_of(None, default="en") is DigestLanguage.EN


def _content(language: DigestLanguage = DigestLanguage.ES) -> DigestContent:
    """One recipient with one report, hand-built: the renderer needs no database."""
    return DigestContent(
        recipient_employee_id=UUID("11111111-1111-1111-1111-111111111111"),
        recipient_email="jefa@empresa.es",
        digest_date=MONDAY,
        language=language,
        link=f"{BASE_URL}/{language}/notifications",
        reports=(
            DigestReport(
                employee_id=UUID("22222222-2222-2222-2222-222222222222"),
                name="Lucía Gómez",
                anomalies=(
                    DigestAnomaly(type=AnomalyType.MISSING_CLOCK_OUT, business_date=MONDAY),
                    DigestAnomaly(type=AnomalyType.LATE, business_date=MONDAY),
                ),
            ),
        ),
    )


def test_the_plain_text_part_says_the_same_thing_without_markup() -> None:
    """Both halves of the ticket's "HTML and plain text" line, on the text side."""
    written = render(_content())

    assert written.subject == "Resumen diario de anomalías — 21/09/2026"
    assert "Lucía Gómez" in written.text
    assert "Falta el fichaje de salida · 21/09/2026" in written.text
    assert "Retraso · 21/09/2026" in written.text
    assert f"{BASE_URL}/es/notifications" in written.text
    assert "<" not in written.text, "no markup in the part that is not markup"


def test_the_html_part_carries_the_grouped_names_the_link_and_no_stylesheet() -> None:
    """A mail client loads nothing, so every rule has to be an attribute.

    The assertions are what a rendered message has to satisfy: the report's name,
    the anomaly labels, a link that works, and no `<link>` or `<style>` element a
    client would drop.
    """
    written = render(_content())

    assert written.html.startswith("<!doctype html>")
    assert 'lang="es"' in written.html
    assert "Lucía Gómez" in written.html
    assert "Falta el fichaje de salida · 21/09/2026" in written.html
    assert f'href="{BASE_URL}/es/notifications"' in written.html
    assert "<style" not in written.html and "<link" not in written.html
    assert written.html.count("Lucía Gómez") == 1, "a report is named once, not per anomaly"


def test_the_subject_names_the_digest_and_the_date_and_nothing_else() -> None:
    """No user data in a subject line: it is visible on a lock screen and in logs."""
    for language in DigestLanguage:
        written = render(_content(language))
        assert "Lucía" not in written.subject
        assert "Gómez" not in written.subject


def test_english_is_english_in_both_parts() -> None:
    written = render(_content(DigestLanguage.EN))

    assert written.subject == "Daily anomaly digest — 21 September 2026"
    assert "Your team's outstanding punches" in written.text
    assert "Missing clock-out · 21 September 2026" in written.text
    # The HTML says the same words, with the apostrophe as the entity a mail client
    # renders it from — which is what escaping user data costs and why it is done.
    assert "outstanding punches" in written.html
    assert "Fichajes pendientes" not in written.html
    assert f"{BASE_URL}/en/notifications" in written.html


def test_a_name_that_contains_markup_is_escaped() -> None:
    """A name is data. `<script>` in a surname must reach the reader as text."""
    content = _content()
    hostile = DigestContent(
        recipient_employee_id=content.recipient_employee_id,
        recipient_email=content.recipient_email,
        digest_date=content.digest_date,
        language=content.language,
        link=content.link,
        reports=(
            DigestReport(
                employee_id=content.reports[0].employee_id,
                name='Ana <script>alert("x")</script>',
                anomalies=content.reports[0].anomalies,
            ),
        ),
    )

    written = render(hostile)

    assert "<script>" not in written.html
    assert "&lt;script&gt;" in written.html
    # The text part is not markup, so the name is written as it is.
    assert 'Ana <script>alert("x")</script>' in written.text


def test_the_digest_row_is_the_record_of_what_was_composed() -> None:
    """The payload is structure — kinds, dates, ids — so the mail can be re-rendered."""
    payload = _content().as_payload()

    assert payload["language"] == "es"
    assert payload["digest_date"] == MONDAY.isoformat()
    assert payload["anomaly_count"] == 2
    assert payload["reports"][0]["anomalies"] == [
        {"type": "missing_clock_out", "business_date": MONDAY.isoformat()},
        {"type": "late", "business_date": MONDAY.isoformat()},
    ]
    assert "Lucía" not in str(payload), "no names in the record: the employee row owns them"


# --- the seam -----------------------------------------------------------------


def test_the_envelope_is_a_text_part_and_an_html_part_in_that_order() -> None:
    """What Mailpit renders is decided here, so it is asserted here."""
    envelope = build_envelope(
        MailMessage(to="jefa@empresa.es", subject="Resumen", text="texto", html="<p>html</p>"),
        "no-reply@empresa.es",
    )

    assert envelope["To"] == "jefa@empresa.es"
    assert envelope["Subject"] == "Resumen"
    assert "no-reply@empresa.es" in envelope["From"]
    assert envelope.get_content_type() == "multipart/alternative"
    parts = envelope.get_payload()
    assert [part.get_content_type() for part in parts] == ["text/plain", "text/html"]
    assert parts[0].get_content().strip() == "texto"
    assert parts[1].get_content().strip() == "<p>html</p>"


def test_the_declared_defaults_are_off_and_pointed_at_mailpit() -> None:
    """Development and demo catch their mail; an unconfigured deployment sends none.

    Asserted on the declared defaults rather than on a `Settings()` instance,
    because the container this runs in may legitimately have `MAIL_ENABLED=true` in
    its environment — that is what the compose service sets.
    """
    declared = Settings.model_fields

    assert declared["mail_enabled"].default is False
    assert (declared["smtp_host"].default, declared["smtp_port"].default) == ("mailpit", 1025)
    assert declared["web_base_url"].default == BASE_URL
    assert build_mailer(Settings(mail_enabled=True)).enabled is True
    assert build_mailer(Settings(mail_enabled=False)).enabled is False


async def test_a_disabled_sender_refuses_rather_than_trying() -> None:
    """`MAIL_ENABLED=false` means no connection is opened, not "opened and ignored".

    The host cannot resolve, so a sender that tried anyway would fail with a DNS
    error instead of the switch's own reason — which is what makes this an
    assertion about *not* attempting rather than about failing.
    """
    disabled = SmtpMailer(
        enabled=False, host="smtp.invalid", port=1025, sender="no-reply@empresa.es", timeout=1
    )

    with pytest.raises(MailError) as refusal:
        await disabled.send(
            MailMessage(to="jefa@empresa.es", subject="s", text="t", html="<p>h</p>")
        )

    assert "MAIL_ENABLED" in str(refusal.value)


async def test_a_half_configured_relay_is_refused_rather_than_guessed_at() -> None:
    """A username without a password is a mistake, and one worth naming."""
    half = SmtpMailer(
        enabled=True,
        host="mailpit",
        port=1025,
        sender="no-reply@empresa.es",
        username="eam",
        timeout=1,
    )

    with pytest.raises(MailError) as refusal:
        await half.send(MailMessage(to="a@b.es", subject="s", text="t", html="<p>h</p>"))

    assert "SMTP_USER and SMTP_PASSWORD" in str(refusal.value)


# --- who gets a mail, and what is in it ---------------------------------------


async def test_a_manager_gets_one_mail_with_the_days_anomalies_grouped_by_report(
    platform: Platform,
) -> None:
    """The ticket's central line: one mail, grouped by report, type and date each."""
    manager = UUID(await platform.employee(first_name="Elena", last_name="Ruiz"))
    latecomer = await works_here(
        platform,
        code="OPS",
        employee_id=await platform.employee(first_name="Lucía", last_name="Gómez"),
        manager_employee_id=str(manager),
    )
    forgetful = await works_here(
        platform,
        code="FIN",
        employee_id=await platform.employee(first_name="Marc", last_name="Puig"),
        manager_employee_id=str(manager),
    )
    await late_and_early(platform, latecomer, MONDAY)
    await forgotten_clock_out(platform, forgetful, MONDAY)
    async with reminder_pass(platform) as (service, reminder):
        await service.scan(MONDAY)
        await reminder.remind(MONDAY)

    mailer = RecordingMailer()
    async with digests(platform, mailer) as service:
        summary = await service.send(MONDAY)

    to_manager = mailer.to(await email_of(platform, manager))
    assert len(to_manager) == 1, "one mail per manager, however many reports"
    # The manager's copy, and one personal copy for each employee whose own punch
    # is outstanding — three recipients, three messages, never four.
    assert summary.sent_count == 3 and summary.failed_count == 0
    message = to_manager[0]
    assert message.subject == "Resumen diario de anomalías — 21/09/2026"
    # Grouped by report: each name once, with that person's anomalies beneath it.
    assert message.text.count("Lucía Gómez") == 1
    assert message.text.count("Marc Puig") == 1
    assert "Retraso · 21/09/2026" in message.text
    assert "Salida anticipada · 21/09/2026" in message.text
    assert "Falta el fichaje de salida · 21/09/2026" in message.text
    assert f"{BASE_URL}/es/notifications" in message.text
    # Both parts, and the HTML one carries the same facts with a link that works.
    assert "Lucía Gómez" in message.html and "Marc Puig" in message.html
    assert f'href="{BASE_URL}/es/notifications"' in message.html
    assert message.html.startswith("<!doctype html>")
    # Nobody is told the same fact twice: this report's lateness arrives as both a
    # clock-out notification and the employee's own reminder, and it is one line.
    assert message.text.count("Retraso") == 1


async def test_the_queued_email_rows_move_to_sent_with_their_attempt_count(
    platform: Platform,
) -> None:
    """Ticket 19's table is where "did it go out" is answered, so it is asserted."""
    manager = UUID(await platform.employee())
    report = await works_here(
        platform, employee_id=await platform.employee(), manager_employee_id=str(manager)
    )
    await late_and_early(platform, report, MONDAY)
    async with reminder_pass(platform) as (service, reminder):
        await service.scan(MONDAY)
        await reminder.remind(MONDAY)
    assert await delivery_rows(platform, manager) == [("pending", 0, NOT_ATTEMPTED, None)]

    mailer = RecordingMailer()
    async with digests(platform, mailer) as service:
        await service.send(MONDAY)

    sent = await delivery_rows(platform, manager)
    assert len(sent) == 1
    status, attempts, error, sent_at = sent[0]
    assert (status, attempts, error) == ("sent", 1, None)
    assert sent_at is not None
    # The digest's own record agrees with the delivery, to the timestamp.
    row = (await digest_rows(platform, manager))[0]
    assert row[0] == manager and row[1] == MONDAY
    assert row[2] == 2, "the two anomalies the report had"
    assert (row[3], row[4], row[5]) == (1, None, sent_at)


async def test_an_employee_with_an_outstanding_punch_gets_their_own_reminder(
    platform: Platform,
) -> None:
    """Ticket 23's "in-app + the personal part of the digest mail", by mail.

    Two recipients out of one pass: the manager hears about the team, and the
    employee hears about their own outstanding punch — which is what makes the
    reminder's queued email row an honest `sent` rather than a hopeful one.
    """
    manager = UUID(await platform.employee(first_name="Elena", last_name="Ruiz"))
    report = await works_here(
        platform,
        employee_id=await platform.employee(first_name="Lucía", last_name="Gómez"),
        manager_employee_id=str(manager),
    )
    await forgotten_clock_out(platform, report, MONDAY)
    async with reminder_pass(platform) as (service, reminder):
        await service.scan(MONDAY)
        await reminder.remind(MONDAY)

    mailer = RecordingMailer()
    async with digests(platform, mailer) as service:
        summary = await service.send(MONDAY)

    assert summary.sent_count == 2
    own = mailer.to(await email_of(platform, report))
    assert len(own) == 1
    assert "Tus fichajes pendientes" in own[0].text
    assert "Falta el fichaje de salida · 21/09/2026" in own[0].text
    assert "Fichajes pendientes de tu equipo" not in own[0].text
    # The manager's copy says it too, and each copy says it once.
    assert own[0].text.count("Falta el fichaje de salida") == 1
    assert len(mailer.to(await email_of(platform, manager))) == 1


async def test_a_clean_day_gets_no_mail(platform: Platform) -> None:
    """The requirement most easily lost, asserted three ways.

    No message, no digest row — the database would refuse one with nothing to
    report anyway — and the queued delivery row left `pending` with the reason it
    was never carried, because "still pending" needs an answer that is not
    "waiting" once the morning has passed.
    """
    manager = UUID(await platform.employee())
    report = await works_here(
        platform, employee_id=await platform.employee(), manager_employee_id=str(manager)
    )
    await worked(platform, report, MONDAY)
    async with reminder_pass(platform) as (service, reminder):
        scanned = await service.scan(MONDAY)
        awaited = await reminder.remind(MONDAY)

    mailer = RecordingMailer()
    async with digests(platform, mailer) as service:
        summary = await service.send(MONDAY)

    assert scanned.created_count == 0 and awaited.reminded_count == 0
    assert mailer.messages == []
    assert summary.sent_count == 0 and summary.failed_count == 0
    assert summary.held == 1, "the clock-out row is still queued, and now says why"
    assert await digest_rows(platform) == []
    assert await delivery_rows(platform, manager) == [("pending", 0, NOTHING_TO_REPORT, None)]


async def test_a_resolved_anomaly_is_not_reported(platform: Platform) -> None:
    """A manager sent to look at a punch that was already made up has been wasted."""
    manager = UUID(await platform.employee())
    report = await works_here(
        platform, employee_id=await platform.employee(), manager_employee_id=str(manager)
    )
    await forgotten_clock_out(platform, report, MONDAY)
    async with reminder_pass(platform) as (service, reminder):
        await service.scan(MONDAY)
        await reminder.remind(MONDAY)

    async with platform.factory() as session:
        repository = PostgresAttendanceRepository(session)
        makeup = await repository.append_event(
            # Ticket 24's make-up punch: source=correction with no target, because
            # the punch it stands for never happened.
            NewEvent(
                employee_id=report,
                event_type=EventType.CLOCK_OUT,
                occurred_at=at(MONDAY, 17),
                business_date=MONDAY,
                source=EventSource.CORRECTION,
            )
        )
        await repository.commit()
    async with platform.factory() as session:
        cleared = await _anomalies(session).resolve_for_correction(report, MONDAY, makeup.id)
    assert cleared, "the premise: the correction resolved the missing clock_out"

    mailer = RecordingMailer()
    async with digests(platform, mailer) as service:
        summary = await service.send(MONDAY)

    assert mailer.messages == []
    assert summary.sent_count == 0
    assert await digest_rows(platform) == []


async def test_a_position_with_no_manager_falls_back_to_the_department_manager(
    platform: Platform,
) -> None:
    """The same fallback the approval route uses, asserted from the mail's side."""
    head = UUID(await platform.employee(first_name="Elena", last_name="Ruiz"))
    report = await works_here(
        platform, employee_id=await platform.employee(), department_manager=str(head)
    )
    await forgotten_clock_out(platform, report, MONDAY)
    async with reminder_pass(platform) as (service, reminder):
        await service.scan(MONDAY)
        await reminder.remind(MONDAY)

    mailer = RecordingMailer()
    async with digests(platform, mailer) as service:
        await service.send(MONDAY)

    assert await email_of(platform, head) in [message.to for message in mailer.messages]


async def test_the_assignments_notification_override_hears_about_the_report(
    platform: Platform,
) -> None:
    """The route is the notifier's, not a second rule invented for the mail.

    Somebody typed the override for this person, and whoever hears about their
    punch in the application is who the morning mail goes to.
    """
    manager = UUID(await platform.employee(first_name="Elena", last_name="Ruiz"))
    covering = UUID(await platform.employee(first_name="Cover", last_name="Holder"))
    report = await works_here(
        platform,
        employee_id=await platform.employee(),
        manager_employee_id=str(manager),
        notification_override_employee_id=str(covering),
    )
    await late_and_early(platform, report, MONDAY)
    async with reminder_pass(platform) as (service, reminder):
        await service.scan(MONDAY)
        await reminder.remind(MONDAY)

    mailer = RecordingMailer()
    async with digests(platform, mailer) as service:
        await service.send(MONDAY)

    recipients = [message.to for message in mailer.messages]
    assert await email_of(platform, covering) in recipients
    assert await email_of(platform, manager) not in recipients


async def test_a_manager_who_is_also_a_report_gets_one_mail_with_both_sections(
    platform: Platform,
) -> None:
    """One message per recipient: two sections, never two messages."""
    head = UUID(await platform.employee(first_name="Elena", last_name="Ruiz"))
    middle = await works_here(
        platform,
        code="OPS",
        employee_id=await platform.employee(first_name="Marc", last_name="Puig"),
        manager_employee_id=str(head),
    )
    report = await works_here(
        platform,
        code="FIN",
        employee_id=await platform.employee(first_name="Lucía", last_name="Gómez"),
        manager_employee_id=str(middle),
    )
    # The middle manager is late themself, and their own report never clocked out.
    await late_and_early(platform, middle, MONDAY)
    await forgotten_clock_out(platform, report, MONDAY)
    async with reminder_pass(platform) as (service, reminder):
        await service.scan(MONDAY)
        await reminder.remind(MONDAY)

    mailer = RecordingMailer()
    async with digests(platform, mailer) as service:
        await service.send(MONDAY)

    own = mailer.to(await email_of(platform, middle))
    assert len(own) == 1, "one mail, not one per section"
    assert "Tus fichajes pendientes" in own[0].text
    assert "Fichajes pendientes de tu equipo" in own[0].text
    assert "Retraso · 21/09/2026" in own[0].text
    assert "Falta el fichaje de salida · 21/09/2026" in own[0].text
    # The head hears about the middle manager's day and nothing about the report
    # two levels down: the route is the direct one.
    to_head = mailer.to(await email_of(platform, head))
    assert len(to_head) == 1
    assert "Marc Puig" in to_head[0].text and "Lucía Gómez" not in to_head[0].text


async def test_somebody_with_anomalies_and_no_route_is_reported_not_invented(
    platform: Platform,
) -> None:
    """A position and a department that both name nobody: there is no mail to send.

    Not an error — a department of one is a real configuration — but the run says
    so, because "nobody was told" and "nothing was wrong" must not look alike.
    """
    report = await works_here(platform, employee_id=await platform.employee())
    await forgotten_clock_out(platform, report, MONDAY)
    async with scanned(platform) as service:
        await service.scan(MONDAY)

    mailer = RecordingMailer()
    async with digests(platform, mailer) as service:
        summary = await service.send(MONDAY)

    assert mailer.messages == []
    assert summary.unrouted == (report,)
    assert await digest_rows(platform) == []


# --- once per day, and a failure that is visible ------------------------------


async def test_a_second_run_of_the_same_day_sends_nothing(platform: Platform) -> None:
    """Idempotent by day, which is the unique key's whole purpose.

    The third run opens a new session, which is all a restarted container is from
    the database's side.
    """
    manager = UUID(await platform.employee())
    report = await works_here(
        platform, employee_id=await platform.employee(), manager_employee_id=str(manager)
    )
    await late_and_early(platform, report, MONDAY)
    async with reminder_pass(platform) as (service, reminder):
        await service.scan(MONDAY)
        await reminder.remind(MONDAY)

    mailer = RecordingMailer()
    async with digests(platform, mailer) as service:
        first = await service.send(MONDAY)
        second = await service.send(MONDAY)
    async with digests(platform, mailer) as service:
        third = await service.send(MONDAY)

    assert first.sent_count == 2
    assert second.sent_count == 0 and second.already_sent == 1
    assert third.sent_count == 0 and third.already_sent == 1
    assert len(mailer.messages) == 2, "the manager and the employee, once each"
    assert len(await digest_rows(platform)) == 2
    assert {row[1] for row in await delivery_rows(platform, manager)} == {1}


async def test_the_unique_key_is_the_idempotency(platform: Platform) -> None:
    """A convention would be checked in code; this is PostgreSQL refusing."""
    recipient = UUID(await platform.employee())
    insert = (
        "INSERT INTO daily_digests (id, recipient_employee_id, digest_date, payload, "
        "anomaly_count) VALUES (gen_random_uuid(), :id, :day, '{}'::jsonb, 1)"
    )
    await platform.sql(insert, {"id": recipient, "day": MONDAY})

    with pytest.raises(IntegrityError) as refusal:
        await platform.sql(insert, {"id": recipient, "day": MONDAY})

    assert "uq_daily_digests_recipient_date" in str(refusal.value)


async def test_the_database_refuses_a_digest_with_nothing_to_report(platform: Platform) -> None:
    """"Avoid daily noise" as a constraint rather than as a discipline."""
    recipient = UUID(await platform.employee())

    with pytest.raises(IntegrityError) as refusal:
        await platform.sql(
            "INSERT INTO daily_digests (id, recipient_employee_id, digest_date, payload, "
            "anomaly_count) VALUES (gen_random_uuid(), :id, :day, '{}'::jsonb, 0)",
            {"id": recipient, "day": MONDAY},
        )

    assert "ck_daily_digests_anomaly_count" in str(refusal.value)


async def test_the_database_refuses_a_language_it_does_not_ship(platform: Platform) -> None:
    """`users.locale` is the two languages the interface has, or nothing at all."""
    actor = await platform.account()

    with pytest.raises(IntegrityError) as refusal:
        await platform.sql(
            "UPDATE users SET locale = 'fr' WHERE employee_id = :id", {"id": actor.employee_id}
        )

    assert "ck_users_locale" in str(refusal.value)


async def test_a_failure_is_recorded_retried_and_then_left_failed(
    platform: Platform,
) -> None:
    """Bounded retry, and the reason stays readable when the retries run out.

    Three attempts — `DIGEST_MAX_ATTEMPTS` — and the fourth run does nothing at all.
    What it leaves behind is the half of ticket 19 the ticket asks for: a `failed`
    delivery row carrying the sender's own words.

    Scanned without the reminder, so this recipient is the only one: one refusal
    per run, and the attempt count is the run count.
    """
    manager = UUID(await platform.employee())
    report = await works_here(
        platform, employee_id=await platform.employee(), manager_employee_id=str(manager)
    )
    await late_and_early(platform, report, MONDAY)
    async with scanned(platform) as service:
        await service.scan(MONDAY)

    refuser = RefusingMailer()
    for attempt in (1, 2, 3):
        async with digests(platform, refuser) as service:
            summary = await service.send(MONDAY)
        assert summary.failed_count == 1, attempt
        assert summary.failed[0].recipient_employee_id == manager
        assert summary.failed[0].error == REFUSAL
        assert summary.failed[0].attempts == attempt

    async with digests(platform, refuser) as service:
        given_up = await service.send(MONDAY)

    assert given_up.exhausted == 1
    assert given_up.failed_count == 0
    assert len(refuser.messages) == 3, "the fourth run attempted nothing"
    row = (await digest_rows(platform, manager))[0]
    assert (row[3], row[4], row[5]) == (3, REFUSAL, None)
    assert await delivery_rows(platform, manager) == [("failed", 3, REFUSAL, None)]


async def test_a_retry_after_the_relay_comes_back_still_sends(platform: Platform) -> None:
    """The other half of the same rule: a failed mail is not a lost one.

    `--retry` is the operator's tool. It resets the attempt budget and cannot reach
    a row that was already sent — one is recoverable, the other is not.
    """
    manager = UUID(await platform.employee())
    report = await works_here(
        platform, employee_id=await platform.employee(), manager_employee_id=str(manager)
    )
    await late_and_early(platform, report, MONDAY)
    async with scanned(platform) as service:
        await service.scan(MONDAY)

    refuser = RefusingMailer()
    for _ in range(3):
        async with digests(platform, refuser) as service:
            await service.send(MONDAY)

    working = RecordingMailer()
    async with digests(platform, working) as service:
        retried = await service.send(MONDAY, retry=True)

    assert retried.sent_count == 1
    assert len(working.messages) == 1
    row = (await digest_rows(platform, manager))[0]
    assert row[3] == 1, "the retry budget starts again"
    assert row[4] is None and row[5] is not None
    # The delivery row counts what actually happened: three refusals and a send.
    status, attempts, error, sent_at = (await delivery_rows(platform, manager))[0]
    assert (status, attempts, error) == ("sent", 4, None)
    assert sent_at is not None

    # And a retry cannot re-send what already went out.
    async with digests(platform, working) as service:
        after = await service.send(MONDAY, retry=True)
    assert after.sent_count == 0 and after.already_sent == 1
    assert len(working.messages) == 1


async def test_with_mail_disabled_nothing_is_attempted_and_the_rows_say_why(
    platform: Platform,
) -> None:
    """`MAIL_ENABLED=false`: no composition, no attempt, no row claiming either.

    The queued delivery rows stay `pending` with the switch's own reason on them, so
    "did the email go out" is answered by the row rather than by somebody's memory
    of a deployment.
    """
    manager = UUID(await platform.employee())
    report = await works_here(
        platform, employee_id=await platform.employee(), manager_employee_id=str(manager)
    )
    await late_and_early(platform, report, MONDAY)
    async with reminder_pass(platform) as (service, reminder):
        await service.scan(MONDAY)
        await reminder.remind(MONDAY)

    disabled = RecordingMailer(enabled=False)
    async with digests(platform, disabled) as service:
        summary = await service.send(MONDAY)

    assert disabled.messages == []
    assert summary.disabled is True and summary.sent_count == 0
    # The manager's clock-out row and the employee's two reminders: three queued
    # rows, none of them attempted, all of them saying which switch is off.
    assert summary.held == 3
    assert await digest_rows(platform) == []
    assert await delivery_rows(platform, manager) == [("pending", 0, MAIL_DISABLED, None)]
    assert await platform.scalar(
        "SELECT count(*) FROM notification_deliveries "
        "WHERE channel = 'email' AND status = 'pending' AND error = :reason",
        {"reason": MAIL_DISABLED},
    ) == 3


# --- the recipient's language, end to end -------------------------------------


async def test_the_mail_is_written_in_the_language_the_account_stored(
    platform: Platform,
) -> None:
    """DESIGN §10.4's preference, read from `users.locale`, decided per recipient.

    No profile screen writes that column yet — this test is what proves the
    mechanism is ready for it, and that an account which has never chosen gets the
    deployment's language rather than whichever one the code was written in.
    """
    english = await platform.account(email="eamon@empresa.es")
    spanish = await platform.account(email="spanish@empresa.es")
    await platform.sql(
        "UPDATE users SET locale = 'en' WHERE employee_id = :id", {"id": english.employee_id}
    )
    for actor, code in ((english, "OPS"), (spanish, "FIN")):
        report = await works_here(
            platform,
            code=code,
            employee_id=await platform.employee(),
            manager_employee_id=actor.employee_id,
        )
        await forgotten_clock_out(platform, report, MONDAY)
    async with reminder_pass(platform) as (service, reminder):
        await service.scan(MONDAY)
        await reminder.remind(MONDAY)

    mailer = RecordingMailer()
    async with digests(platform, mailer) as service:
        await service.send(MONDAY)

    to_english = mailer.to(english.email)
    to_spanish = mailer.to(spanish.email)
    assert len(to_english) == 1 and len(to_spanish) == 1
    assert to_english[0].subject == "Daily anomaly digest — 21 September 2026"
    assert "Your team's outstanding punches" in to_english[0].text
    assert to_english[0].text.count("Missing clock-out") == 1
    assert f"{BASE_URL}/en/notifications" in to_english[0].html
    assert to_spanish[0].subject == "Resumen diario de anomalías — 21/09/2026"
    assert "Fichajes pendientes de tu equipo" in to_spanish[0].text
    assert f"{BASE_URL}/es/notifications" in to_spanish[0].html


# --- the command --------------------------------------------------------------


def test_the_command_defaults_to_yesterday_in_madrid() -> None:
    """The pass runs at 08:00, so the day it is about has just ended."""
    today, retry = parse_arguments([])

    assert today == previous_day(madrid_today(datetime.now(UTC)))
    assert retry is False


def test_the_command_reads_its_date_and_its_flag() -> None:
    """Any date, without waiting for 08:00 — the ticket's last checklist line."""
    assert parse_arguments([MONDAY.isoformat()]) == (MONDAY, False)
    assert parse_arguments([MONDAY.isoformat(), RETRY_FLAG]) == (MONDAY, True)
    assert parse_arguments([RETRY_FLAG]) == (previous_day(madrid_today(datetime.now(UTC))), True)
    with pytest.raises(SystemExit):
        parse_arguments([MONDAY.isoformat(), TUESDAY.isoformat()])
    with pytest.raises(ValueError):
        parse_arguments(["the day before yesterday"])


async def test_the_command_runs_the_whole_pass_through_the_restricted_role(
    platform: Platform, capsys: pytest.CaptureFixture
) -> None:
    """The job, as cron calls it: its own session, the runtime role, exit code 0.

    Driving the service directly is not enough — the query runs as `eam_app`, and a
    table that role cannot read is exactly the mistake this catches. The printed
    line is the operator's summary, so it is asserted too.
    """
    manager = UUID(await platform.employee())
    report = await works_here(
        platform, employee_id=await platform.employee(), manager_employee_id=str(manager)
    )
    await late_and_early(platform, report, MONDAY)
    async with reminder_pass(platform) as (service, reminder):
        await service.scan(MONDAY)
        await reminder.remind(MONDAY)

    mailer = RecordingMailer()
    assert await main([MONDAY.isoformat()], mailer=mailer) == 0

    printed = capsys.readouterr().out
    assert MONDAY.isoformat() in printed
    assert "2 digests sent" in printed and "0 failed" in printed
    assert len(mailer.messages) == 2
    assert len(await digest_rows(platform)) == 2

    # A second run of the same command is the no-op the ticket asks for.
    assert await main([MONDAY.isoformat()], mailer=mailer) == 0
    assert "0 digests sent" in capsys.readouterr().out
    assert len(mailer.messages) == 2


async def test_the_command_says_so_when_mail_is_switched_off(
    platform: Platform, capsys: pytest.CaptureFixture
) -> None:
    """The operator's line, and the exit code that keeps cron quiet.

    Mail off is a decision, not a failure: nothing is attempted, everything is left
    for the next enabled run, and the command still exits 0.
    """
    manager = UUID(await platform.employee())
    report = await works_here(
        platform, employee_id=await platform.employee(), manager_employee_id=str(manager)
    )
    await forgotten_clock_out(platform, report, MONDAY)
    async with reminder_pass(platform) as (service, reminder):
        await service.scan(MONDAY)
        await reminder.remind(MONDAY)

    disabled = RecordingMailer(enabled=False)
    assert await main([MONDAY.isoformat()], mailer=disabled) == 0

    assert "mail is disabled" in capsys.readouterr().out
    assert disabled.messages == []
    # The employee's own reminder, since a shift nobody closed raises no clock-out
    # notification: one queued row, still pending, and it says which switch is off.
    assert await delivery_rows(platform) == [("pending", 0, MAIL_DISABLED, None)]
