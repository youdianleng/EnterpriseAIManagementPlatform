"""One morning's mail: who gets one, what is in it, and what is written down.

The run is `notification.models.DIGEST_CANDIDATE_TYPES` turned into mail, and it
answers three questions in this order — the order is the design:

1. **Who has something to be told.** The day's standing anomalies, routed by the
   same rule the rest of the system routes attendance notifications by
   (`attendance/notify.py`: the assignment's notification override, else the
   primary position's manager, else the department's manager — which is
   `ApprovalService._resolve_level_one`'s rule with the override in front), plus
   the addressee of any queued reminder about one of those anomalies, which is how
   an employee's own outstanding punch reaches them by mail. A recipient with no
   anomaly in their section is not a recipient at all, which *is* the ticket's
   "avoid daily noise": nothing is composed for a clean day, so nothing can be sent
   for one.
2. **Whether this day has already been mailed to them.** `daily_digests` is the
   answer and `sent_at` is the whole of it. A row that is already sent is skipped
   without composing anything — the second run of the job, the restarted container
   and the cron entry that fired twice all arrive here and all do nothing.
3. **What to write down.** Success stamps `sent_at`; failure increments `attempts`
   and stores the sender's reason. A failure is retried by the next run of the same
   date while `attempts < DIGEST_MAX_ATTEMPTS`, and after that it is left `failed`
   with the reason on it — the half of ticket 19 the ticket asks to be observable,
   and the reason an operator can re-run that date deliberately (`retry=True`).

**One message per recipient, and the mailer is called once per message.** Both
routes feed one map keyed by recipient, so a manager who is also somebody's report
gets one mail with both sections rather than two mails.

**A queued row with nothing to report is not carried.** The mail says what needs
attention, so a clean day is not a mail — its rows stay `pending` with that reason
written on them, which is the honest answer to "was this notification emailed".

**The run never raises for one recipient.** A mail server that refuses, an address
that does not exist: both are recorded against that recipient and the pass carries
on, for the reason every other job here gives — one broken row must not cost the
other ninety-nine people their morning.
"""

from dataclasses import dataclass, field
from datetime import date
from uuid import UUID

from app.domain.attendance.anomalies import ANOMALY_ORDER
from app.domain.notification.digest import (
    DIGEST_PATH,
    DigestAnomaly,
    DigestContent,
    DigestReport,
    language_of,
    render,
)
from app.domain.notification.digest_repository import (
    DigestRecipient,
    DigestRepository,
    QueuedDelivery,
)
from app.logging import get_logger
from app.mail import Mailer, MailError, MailMessage

logger = get_logger(__name__)

#: Why a queued row is still `pending` after its day's digest has run: there was
#: nothing to report, and the digest is not a daily "all is well" message.
NOTHING_TO_REPORT = "not attempted: nothing to report for this day, so no digest was sent"

#: Why a queued row is still `pending` when the sender is switched off. It says
#: which switch, which is more than "not attempted" on its own can.
MAIL_DISABLED = "not attempted: mail is disabled (MAIL_ENABLED=false)"


@dataclass(slots=True, frozen=True)
class DigestReceipt:
    """One mail that went out."""

    recipient_employee_id: UUID
    email: str
    anomaly_count: int
    report_count: int


@dataclass(slots=True, frozen=True)
class DigestFailure:
    """One mail that did not, and the attempt it failed on."""

    recipient_employee_id: UUID
    email: str
    attempts: int
    error: str


@dataclass(slots=True, frozen=True)
class DigestRunReport:
    """What one pass did, in counts an operator can read in a log line.

    `unrouted` is the population worth watching: people with anomalies that no
    mail mentioned. It is not a failure — there is no recipient to invent — but it
    is the reason a manager would later say they were never told.
    """

    digest_date: date
    sent: tuple[DigestReceipt, ...] = ()
    failed: tuple[DigestFailure, ...] = ()
    #: Already mailed on an earlier run: the no-op half of the idempotency.
    already_sent: int = 0
    #: Given up on after `max_attempts`: still visible on the delivery rows.
    exhausted: int = 0
    #: True when nothing was attempted because the sender is switched off.
    disabled: bool = False
    #: Queued rows whose reason this run rewrote, and which it did not attempt.
    held: int = 0
    #: People with anomalies that no mail this run mentioned.
    unrouted: tuple[UUID, ...] = ()

    @property
    def sent_count(self) -> int:
        return len(self.sent)

    @property
    def failed_count(self) -> int:
        return len(self.failed)

    @property
    def anomaly_count(self) -> int:
        return sum(receipt.anomaly_count for receipt in self.sent)


@dataclass
class _Address:
    """One recipient's mail while it is being assembled."""

    recipient: DigestRecipient
    reports: dict[UUID, list[DigestAnomaly]] = field(default_factory=dict)
    names: dict[UUID, str] = field(default_factory=dict)
    deliveries: list[UUID] = field(default_factory=list)


class DailyDigest:
    """Composing and sending one day's digest, once per recipient."""

    def __init__(
        self,
        repository: DigestRepository,
        mailer: Mailer,
        *,
        base_url: str,
        default_language: str = "es",
        max_attempts: int = 3,
    ) -> None:
        self._repository = repository
        self._mailer = mailer
        self._base_url = base_url.rstrip("/")
        self._default_language = default_language
        self._max_attempts = max_attempts

    async def send(self, digest_date: date, *, retry: bool = False) -> DigestRunReport:
        """One pass over one day.

        `retry` is the operator's tool after fixing a mail server: it puts the
        day's unsent rows back to zero attempts, so a digest that had given up is
        attempted again. It cannot resurrect a *sent* row — mailing somebody twice
        is the failure this job exists to prevent.
        """
        queued = await self._repository.queued_deliveries(digest_date)
        if not self._mailer.enabled:
            # Nothing is attempted, and that is a decision rather than a failure:
            # the rows say which switch is off, they stay `pending` with no
            # attempts, and the next enabled run picks them up.
            held = await self._repository.held([row.delivery_id for row in queued], MAIL_DISABLED)
            await self._repository.commit()
            return DigestRunReport(digest_date=digest_date, disabled=True, held=held)

        if retry:
            await self._repository.forget_attempts(digest_date)

        addresses, unrouted = await self._assemble(digest_date, queued)
        records = {
            record.recipient_employee_id: record
            for record in await self._repository.digests_on(digest_date)
        }

        sent: list[DigestReceipt] = []
        failed: list[DigestFailure] = []
        already_sent = 0
        exhausted = 0
        for address in addresses.values():
            record = records.get(address.recipient.employee_id)
            if record is not None and record.is_sent:
                already_sent += 1
                continue
            if record is not None and record.attempts >= self._max_attempts:
                # Left `failed` with its reason, deliberately: the alternative is a
                # scheduler that retries a permanently broken address for ever.
                exhausted += 1
                continue

            content = self._content(address, digest_date)
            message = render(content)
            try:
                await self._mailer.send(
                    MailMessage(
                        to=content.recipient_email,
                        subject=message.subject,
                        text=message.text,
                        html=message.html,
                    )
                )
            except MailError as error:
                attempts = await self._record_failure(address, content, digest_date, str(error))
                failed.append(
                    DigestFailure(
                        recipient_employee_id=address.recipient.employee_id,
                        email=content.recipient_email,
                        attempts=attempts,
                        error=str(error),
                    )
                )
                continue

            await self._repository.record_sent(
                address.recipient.employee_id,
                digest_date,
                payload=content.as_payload(),
                anomaly_count=content.anomaly_count,
            )
            await self._repository.delivered(address.deliveries)
            sent.append(
                DigestReceipt(
                    recipient_employee_id=address.recipient.employee_id,
                    email=content.recipient_email,
                    anomaly_count=content.anomaly_count,
                    report_count=len(content.reports),
                )
            )

        # The queue's leftovers: rows this pass did not carry, because their day
        # turned out to have nothing worth mailing. Rewriting the reason is what
        # makes "still pending" answerable instead of looking like a stuck queue.
        carried = {
            delivery_id for address in addresses.values() for delivery_id in address.deliveries
        }
        leftover = [row.delivery_id for row in queued if row.delivery_id not in carried]
        held = await self._repository.held(leftover, NOTHING_TO_REPORT) if leftover else 0

        await self._repository.commit()
        logger.info(
            "daily_digests_sent",
            digest_date=digest_date,
            sent=len(sent),
            failed=len(failed),
            already_sent=already_sent,
            exhausted=exhausted,
            held=held,
        )
        return DigestRunReport(
            digest_date=digest_date,
            sent=tuple(sent),
            failed=tuple(failed),
            already_sent=already_sent,
            exhausted=exhausted,
            held=held,
            unrouted=unrouted,
        )

    # --- internals ----------------------------------------------------------

    async def _record_failure(
        self,
        address: _Address,
        content: DigestContent,
        digest_date: date,
        error: str,
    ) -> int:
        """Write the attempt down, on the digest and on every row it carried.

        Returns the attempt count the digest row reached, which is the number the
        caller reports — read back from the write rather than computed beside it,
        because two runs racing on one day would each add one to their own idea of
        the count.
        """
        attempts = await self._repository.record_failure(
            address.recipient.employee_id,
            digest_date,
            payload=content.as_payload(),
            anomaly_count=content.anomaly_count,
            error=error,
        )
        await self._repository.failed(address.deliveries, error)
        logger.error(
            "daily_digest_not_sent",
            recipient_employee_id=str(address.recipient.employee_id),
            digest_date=digest_date,
            attempts=attempts,
            error=error,
        )
        return attempts

    async def _assemble(
        self, digest_date: date, queued: list[QueuedDelivery]
    ) -> tuple[dict[UUID, _Address], tuple[UUID, ...]]:
        """Every recipient with something to be told, and what they are told.

        Two sources, one map: the day's anomalies give each report and the route
        that names who hears about them, and the queued reminders give the people
        whose own outstanding punches are worth a mail. An employee reached by
        both lands in one address, once.
        """
        addresses: dict[UUID, _Address] = {}
        found: dict[UUID, list[DigestAnomaly]] = {}
        names: dict[UUID, str] = {}

        for row in await self._repository.day_anomalies(digest_date):
            lines = found.setdefault(row.employee_id, [])
            line = DigestAnomaly(type=row.type, business_date=row.business_date)
            if line not in lines:
                lines.append(line)
            names[row.employee_id] = row.employee_name
            if row.recipient is None:
                continue
            address = _address(addresses, row.recipient)
            _list(address, row.employee_id, row.employee_name, line)

        for delivery in queued:
            lines = found.get(delivery.subject_employee_id)
            if not lines:
                # Nothing to report about this person, so the digest does not carry
                # this row. This one condition is "no anomalies, no email".
                continue
            address = _address(addresses, delivery.recipient)
            address.deliveries.append(delivery.delivery_id)
            for line in lines:
                _list(
                    address,
                    delivery.subject_employee_id,
                    names.get(delivery.subject_employee_id, ""),
                    line,
                )

        listed = {
            employee_id for address in addresses.values() for employee_id in address.reports
        }
        unrouted = tuple(sorted(set(found) - listed, key=str))
        for employee_id in unrouted:
            logger.info("daily_digest_recipient_unresolved", employee_id=str(employee_id))
        return addresses, unrouted

    def _content(self, address: _Address, digest_date: date) -> DigestContent:
        """The recipient's own anomalies first, then the team by name."""
        language = language_of(address.recipient.locale, default=self._default_language)
        reports = [
            DigestReport(
                employee_id=employee_id,
                name=address.names[employee_id],
                # In the anomaly catalogue's own order, so two runs of the same day
                # produce the same mail and a test can assert a list, not a set.
                anomalies=tuple(sorted(lines, key=lambda line: ANOMALY_ORDER.index(line.type))),
            )
            for employee_id, lines in address.reports.items()
        ]
        reports.sort(
            key=lambda report: (report.employee_id != address.recipient.employee_id, report.name)
        )
        return DigestContent(
            recipient_employee_id=address.recipient.employee_id,
            recipient_email=address.recipient.email,
            digest_date=digest_date,
            language=language,
            link=f"{self._base_url}/{language}{DIGEST_PATH}",
            reports=tuple(reports),
        )


def _address(addresses: dict[UUID, _Address], recipient: DigestRecipient) -> _Address:
    found = addresses.get(recipient.employee_id)
    if found is None:
        found = _Address(recipient=recipient)
        addresses[recipient.employee_id] = found
    return found


def _list(
    address: _Address, employee_id: UUID, name: str, anomaly: DigestAnomaly
) -> None:
    """Add one anomaly to one report, without listing the same fact twice.

    The two sources overlap by design — an anomaly is both a row the manager is
    routed and, usually, a queued reminder — and a digest that said "late arrival"
    twice is a bug the reader would notice before any test did.
    """
    lines = address.reports.setdefault(employee_id, [])
    if anomaly not in lines:
        lines.append(anomaly)
    address.names.setdefault(employee_id, name)


__all__ = [
    "MAIL_DISABLED",
    "NOTHING_TO_REPORT",
    "DailyDigest",
    "DigestFailure",
    "DigestReceipt",
    "DigestRunReport",
]
