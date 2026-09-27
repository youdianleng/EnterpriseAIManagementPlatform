"""Notifications, and what happened to each of their deliveries.

Two tables, split along the question each answers (DESIGN §3.7):

* `notifications` is *the message and who it is for*: a type, a bilingual
  `title_key`, structured `payload` fields, the entity it is about, and whether
  it has been read. It stores no sentence — the client renders the key in the
  reader's language (D2), so the same row reads correctly in Spanish and English
  and a wording change needs no migration.
* `notification_deliveries` is *how it travelled*: one row per channel, with its
  own status, attempt count and failure reason. "I never got it" is answered from
  here rather than by guessing.

**The dedupe key is a unique index, not a convention.** `(recipient_employee_id,
dedupe_key)` is unique, so the same event raised twice for the same person cannot
produce two rows — a retry, a double submission or a worker that ran twice all
land on the same notification. Checking first in code would be a race; this is the
database refusing the second write.

**Expiry hides a row; it does not delete it.** `expires_at` is a read filter, so
an expired notification leaves the list while the record stays for the question
that always follows: "was that person told, and when".

`recipient_employee_id` is a plain UUID rather than a foreign key to `employees`,
for the reason `approval_requests.requester_employee_id` is: what somebody was
told has to stay readable after they leave, and a notification needs no account
to exist — it is addressed to a person.
"""

from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db_metadata import Base

#: Duplicated in the migration as a literal, because a migration describes the
#: schema it applied. A constraint that read this constant would silently follow a
#: later edit and the two would disagree only on a fresh database.
TITLE_KEY_PATTERN = r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$"


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        # "A key, never a sentence" as something the database refuses rather than
        # something a reviewer has to notice. A dotted lowercase path is what the
        # dictionaries in `web/lib/i18n/messages` are keyed by.
        CheckConstraint(f"title_key ~ '{TITLE_KEY_PATTERN}'", name="ck_notifications_title_key"),
        CheckConstraint("length(btrim(type)) > 0", name="ck_notifications_type"),
        CheckConstraint("length(btrim(dedupe_key)) > 0", name="ck_notifications_dedupe_key"),
        # Structured fields only. `jsonb_typeof` refuses a bare string, which is
        # what a pre-composed sentence would be stored as.
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="ck_notifications_payload"),
        CheckConstraint("length(btrim(entity_type)) > 0", name="ck_notifications_entity_type"),
        UniqueConstraint(
            "recipient_employee_id", "dedupe_key", name="uq_notifications_recipient_dedupe"
        ),
        # The list: one person's notifications, newest first.
        Index("ix_notifications_recipient_created", "recipient_employee_id", "created_at"),
        # The badge. Partial, because the unread count never looks at read rows
        # and those are the ones that accumulate.
        Index(
            "ix_notifications_recipient_unread",
            "recipient_employee_id",
            postgresql_where=text("read_at IS NULL"),
        ),
        Index("ix_notifications_entity", "entity_type", "entity_id"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    recipient_employee_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    #: `approval.approved`, `attendance.clock_out`, … — the event, not the wording.
    type: Mapped[str] = mapped_column(String(60), nullable=False)
    #: The dictionary key the client renders, e.g. `notifications.approval.approved`.
    #: Stored on the row as well as derivable from `type`, so renaming a type later
    #: cannot change the wording of a notification that was already sent.
    title_key: Mapped[str] = mapped_column(String(120), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    #: What the notification is about, so a reader can follow it back. Free-form
    #: like `approval_requests.entity_type`: the notifier never interprets it.
    entity_type: Mapped[str] = mapped_column(String(60), nullable=False)
    entity_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    #: What makes two raises one event. Unique per recipient.
    dedupe_key: Mapped[str] = mapped_column(String(200), nullable=False)
    #: A timestamp rather than an `is_read` flag: "when" is the question an
    #: incident review asks, and it is the same column.
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    #: Null means it never expires: a decision about somebody's request stays in
    #: their centre until they have seen it.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Notification {self.type} -> {self.recipient_employee_id}>"


class NotificationDelivery(Base):
    """One channel's attempt to deliver one notification.

    The unique constraint is the requirement's "one row per channel per
    notification" stated as a rule of the schema: a retry updates `attempts` and
    `sent_at`, it does not append a second row to be reconciled later.
    """

    __tablename__ = "notification_deliveries"
    __table_args__ = (
        CheckConstraint("channel IN ('inapp', 'email')", name="ck_notification_deliveries_channel"),
        CheckConstraint(
            "status IN ('pending', 'sent', 'failed')", name="ck_notification_deliveries_status"
        ),
        CheckConstraint("attempts >= 0", name="ck_notification_deliveries_attempts"),
        UniqueConstraint(
            "notification_id", "channel", name="uq_notification_deliveries_channel"
        ),
        Index("ix_notification_deliveries_notification", "notification_id"),
        # The worker's queue: what still has to go out, oldest first.
        Index(
            "ix_notification_deliveries_pending",
            "channel",
            "status",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    notification_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("notifications.id", ondelete="CASCADE"),
        nullable=False,
    )
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    #: Why it failed, as the sender reported it. Never a rendered message.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<NotificationDelivery {self.channel} {self.status}>"


class DailyDigest(Base):
    """One morning's mail to one recipient, and how the sending of it went.

    `(recipient_employee_id, digest_date)` is unique, and that index *is* the
    ticket's "the same day is mailed once": a second run of the job, a restarted
    container and a cron entry that fired twice all land on this row instead of on
    a second message. The row is written **before** the message is handed to the
    sender, so a crash between the two leaves a failed attempt that the next run
    retries rather than an untraceable hole.

    `anomaly_count > 0` is the "avoid daily noise" rule stated where it cannot be
    forgotten: a mail is only ever composed about something, so a row with nothing
    to report is a row PostgreSQL refuses.

    `attempts` and `error` live here rather than only on the notification
    deliveries: *this* is the unit that is retried, and it can be the only record
    of an attempt when the day's queued notifications have none (a recipient whose
    anomalies were reported but whose reminders were suppressed as duplicates).

    `payload` is the structured content that was composed — the reports, the
    anomaly kinds and the date — never the rendered sentences. What was sent is
    reconstructible in either language, which is what a mail that has already left
    the building needs.
    """

    __tablename__ = "daily_digests"
    __table_args__ = (
        CheckConstraint("anomaly_count > 0", name="ck_daily_digests_anomaly_count"),
        CheckConstraint("attempts >= 0", name="ck_daily_digests_attempts"),
        # The mechanism, not a convention: one mail per recipient per day.
        UniqueConstraint(
            "recipient_employee_id", "digest_date", name="uq_daily_digests_recipient_date"
        ),
        Index("ix_daily_digests_date", "digest_date"),
        # What a run still has to deal with: rows that have not gone out.
        Index(
            "ix_daily_digests_unsent",
            "digest_date",
            postgresql_where=text("sent_at IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    #: Who the mail is addressed to. DESIGN §3.7 calls this `manager_employee_id`,
    #: which is who it is in the ordinary case; it is a plain UUID for the reason
    #: `notifications.recipient_employee_id` is — the mail is addressed to a
    #: person, and remains readable after they leave.
    recipient_employee_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    digest_date: Mapped[date] = mapped_column(Date, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    anomaly_count: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Attempts *made*, so 0 is "composed, never handed to a sender" — which is
    #: what a run with `MAIL_ENABLED=false` would leave if it wrote rows at all.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    #: Why the last attempt failed, as the sender reported it. Cleared on success.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<DailyDigest {self.digest_date} -> {self.recipient_employee_id}>"


__all__ = ["TITLE_KEY_PATTERN", "DailyDigest", "Notification", "NotificationDelivery"]
