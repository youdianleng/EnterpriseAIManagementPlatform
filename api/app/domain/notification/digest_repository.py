"""Persistence contract for the morning digest.

Four reads and four writes, and the split is the ticket's two hard requirements:

* **`digests_on` is how "already mailed" is decided.** The unique key on
  `(recipient_employee_id, digest_date)` is what makes a second run a no-op rather
  than a second mail; this read is what lets the service *say* that it was a no-op
  instead of silently re-composing and colliding.
* **The delivery writes are the other half of ticket 19.** The queued email rows
  are the reason "was this person told" is answerable, so the digest moves them —
  `delivered`, `failed` or `held` — rather than only recording its own success.

The two value objects here carry a *recipient*, which is a manager in the ordinary
case and the employee themself for the personal reminder. Both come out of the same
join (`employees` for the address, `users` for the language), which is why one
shape answers both.

Nothing commits but `commit`: one run writes a whole morning's mail, and a run that
failed half way through must leave what it already sent sent — `attempts` and
`sent_at` are only meaningful if they survive the next failure.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol
from uuid import UUID

from app.domain.attendance.anomalies import AnomalyType


@dataclass(slots=True, frozen=True)
class DigestRecipient:
    """Who a mail goes to: the address it is sent to and the language it is in."""

    employee_id: UUID
    email: str
    #: `users.locale`, or None when the account has never chosen (DESIGN §10.4).
    locale: str | None


@dataclass(slots=True, frozen=True)
class DayAnomaly:
    """One standing anomaly of the day, and who is told about the person it belongs to.

    `recipient` is None when the person's route names nobody — no primary position,
    or a position and department that both name no manager. That is not an error (a
    department of one is a real configuration) but it is a fact the run reports,
    because "nobody was mailed" and "nothing was wrong" must not look alike.
    """

    anomaly_id: UUID
    employee_id: UUID
    employee_name: str
    type: AnomalyType
    business_date: date
    recipient: DigestRecipient | None


@dataclass(slots=True, frozen=True)
class QueuedDelivery:
    """One candidate notification whose email has not gone out yet.

    `subject_employee_id` is the person the notification is *about*, which is not
    always the person it is addressed to: the anomaly reminder is addressed to the
    employee themself, and the clock-out notification to their manager.
    """

    delivery_id: UUID
    notification_id: UUID
    recipient: DigestRecipient
    subject_employee_id: UUID


@dataclass(slots=True, frozen=True)
class DigestRecord:
    """One day's row for one recipient, as the run needs to read it."""

    recipient_employee_id: UUID
    attempts: int
    sent_at: datetime | None
    error: str | None

    @property
    def is_sent(self) -> bool:
        return self.sent_at is not None


class DigestRepository(Protocol):
    async def day_anomalies(self, digest_date: date) -> list[DayAnomaly]:
        """The day's anomalies that still stand, with the route of the person each belongs to.

        Resolved ones are excluded: the mail lists what still needs attention, and
        a manager told about a punch that was already made up is being sent to look
        at nothing.
        """
        ...

    async def queued_deliveries(self, digest_date: date) -> list[QueuedDelivery]:
        """Candidate notifications about this day whose email has not gone out.

        Selected by *type* (`DIGEST_CANDIDATE_TYPES`), by the day their payload
        names and by the email row still being open — which is the queue ticket 19
        left at `pending` and ticket 20 exists to drain.
        """
        ...

    async def digests_on(self, digest_date: date) -> list[DigestRecord]:
        """What this day's runs have already done, one row per recipient."""
        ...

    async def forget_attempts(self, digest_date: date) -> int:
        """Clear the attempt count of the day's unsent rows. Returns how many.

        An operator's retry, after fixing whatever refused the mail. A sent row is
        never touched: the alternative is mailing somebody a second time, which is
        the failure this whole table exists to prevent.
        """
        ...

    async def record_sent(
        self,
        recipient_employee_id: UUID,
        digest_date: date,
        *,
        payload: dict[str, Any],
        anomaly_count: int,
    ) -> None:
        """Write the row, stamped as sent, counting this attempt.

        An upsert against `uq_daily_digests_recipient_date`, so a first attempt and
        a successful retry are one statement. `sent_at` comes from the database's
        clock for the reason notifications do: a row must not claim a time the
        deliveries written beside it disagree with.
        """
        ...

    async def record_failure(
        self,
        recipient_employee_id: UUID,
        digest_date: date,
        *,
        payload: dict[str, Any],
        anomaly_count: int,
        error: str,
    ) -> int:
        """Write the attempt and why it failed. Returns the attempt count it reached.

        The row is written *before* the outcome is known for the same reason the
        notification dupe-suppression record exists: a crash between composing and
        sending must leave a trace, and an absent row would be indistinguishable
        from a day with nothing to report.
        """
        ...

    async def delivered(self, delivery_ids: Sequence[UUID]) -> int:
        """Mark the carried email rows sent, stamped with the same clock."""
        ...

    async def failed(self, delivery_ids: Sequence[UUID], error: str) -> int:
        """Mark the carried email rows failed, with the sender's own reason."""
        ...

    async def held(self, delivery_ids: Sequence[UUID], reason: str) -> int:
        """Rewrite the reason on rows that are still pending, and nothing else.

        Status and attempts are left alone: this is what a run does when it
        attempted nothing (`MAIL_ENABLED=false`) or when a row turned out to have
        nothing to report, and neither is an attempt.
        """
        ...

    async def commit(self) -> None: ...


__all__ = [
    "DayAnomaly",
    "DigestRecord",
    "DigestRecipient",
    "DigestRepository",
    "QueuedDelivery",
]
