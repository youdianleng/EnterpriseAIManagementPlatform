"""Notification value objects.

The vocabulary is deliberately small, and it is a *vocabulary*: a notification is
a type, a key the client renders, structured fields, and the entity it is about.
There is no sentence anywhere in it, because the readers of these rows read
Spanish and English (D2) and a sentence stored once cannot be both.

**The dedupe key is derived, never spelled at a call site.** Two raises are the
same event when the recipient, the type and the discriminator match, so the key is
composed here from those three parts. A caller cannot get it subtly wrong — and a
key a caller *could* get wrong is a key that eventually produces two of the same
notification, which is exactly what the unique index exists to prevent.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


class NotificationType(StrEnum):
    """What happened. Named `domain.verb` like the audit catalogue.

    A catalogue rather than a CHECK constraint in the database: this vocabulary
    grows with every module that learns to notify somebody (`attendance.clock_out`
    in ticket 23, `payslip.withdrawn` in 46), and a constraint would make each of
    those a migration of a table they do not own. `channel` and `status` below are
    constrained, because those two really are closed.
    """

    APPROVAL_AWAITING_DECISION = "approval.awaiting_decision"
    APPROVAL_APPROVED = "approval.approved"
    APPROVAL_REJECTED = "approval.rejected"
    APPROVAL_RETURNED = "approval.returned"
    APPROVAL_WITHDRAWN = "approval.withdrawn"
    #: Somebody finished their day. Raised for the manager of the primary position —
    #: or for the assignment's notification override — by `attendance/notify.py`
    #: (ticket 23).
    ATTENDANCE_CLOCK_OUT = "attendance.clock_out"
    #: The morning reminder to the employee about their own outstanding punches of
    #: the day before (ticket 23). Raised by the same module, one per anomaly.
    ATTENDANCE_ANOMALY_REMINDER = "attendance.anomaly_reminder"


#: The bilingual key each type renders as. One table, so the backend, the
#: dictionaries and the tests cannot drift about what a type is called.
TITLE_KEY_OF: dict[NotificationType, str] = {
    NotificationType.APPROVAL_AWAITING_DECISION: "notifications.approval.awaiting_decision",
    NotificationType.APPROVAL_APPROVED: "notifications.approval.approved",
    NotificationType.APPROVAL_REJECTED: "notifications.approval.rejected",
    NotificationType.APPROVAL_RETURNED: "notifications.approval.returned",
    NotificationType.APPROVAL_WITHDRAWN: "notifications.approval.withdrawn",
    NotificationType.ATTENDANCE_CLOCK_OUT: "notifications.attendance.clock_out",
    NotificationType.ATTENDANCE_ANOMALY_REMINDER: (
        "notifications.attendance.anomaly_reminder"
    ),
}

#: What the morning digest carries (ticket 20), named here rather than in the job
#: that will select it because a *type* is the thing that set is made of: adding a
#: member is what puts a notification in the mail, and a type left out of it is
#: delivered in-app only.
#:
#: Every notification is queued on the email channel (`service.DELIVERY_PLAN`) so
#: that "was this person told" is answerable from the delivery rows; this set is
#: what says which of those queued rows the daily digest is made of, and it exists
#: so that ticket 20 does not have to guess from a status every row shares.
DIGEST_CANDIDATE_TYPES: frozenset[NotificationType] = frozenset(
    {
        NotificationType.ATTENDANCE_CLOCK_OUT,
        NotificationType.ATTENDANCE_ANOMALY_REMINDER,
    }
)


class DeliveryChannel(StrEnum):
    """Where a notification can travel. Two, and closed (DESIGN §3.7)."""

    IN_APP = "inapp"
    EMAIL = "email"


class DeliveryStatus(StrEnum):
    """How one channel's attempt stands."""

    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


@dataclass(slots=True, frozen=True)
class Notification:
    """One notification, as stored."""

    id: UUID
    recipient_employee_id: UUID
    type: NotificationType
    title_key: str
    payload: dict[str, Any]
    entity_type: str
    entity_id: UUID
    dedupe_key: str
    read_at: datetime | None
    created_at: datetime
    expires_at: datetime | None

    @property
    def is_read(self) -> bool:
        return self.read_at is not None


@dataclass(slots=True, frozen=True)
class Delivery:
    """One channel's record for one notification."""

    id: UUID
    notification_id: UUID
    channel: DeliveryChannel
    status: DeliveryStatus
    attempts: int
    error: str | None
    sent_at: datetime | None


@dataclass(slots=True, frozen=True)
class NotificationDraft:
    """A notification about to be raised.

    `event` is the discriminator: what makes *this* raise of this type about this
    entity distinct from the next one. A resubmission is a new round, a second
    decision is a new level — those are different events with different keys, and
    a retry of either is the same event with the same key.
    """

    recipient_employee_id: UUID
    type: NotificationType
    payload: dict[str, Any]
    entity_type: str
    entity_id: UUID
    event: str
    title_key: str = ""
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        # Resolved here rather than asked of every caller: the type already says
        # which key it renders as, and a caller that passed its own could pass one
        # the client has never heard of.
        if not self.title_key:
            object.__setattr__(self, "title_key", TITLE_KEY_OF[self.type])

    @property
    def dedupe_key(self) -> str:
        """Stable for one event, different for the next one about the same entity."""
        return f"{self.type}:{self.entity_type}:{self.entity_id}:{self.event}"


#: Why an email delivery row sits at `pending` with no attempts.
#:
#: Written on the row rather than left empty, because an empty reason is
#: indistinguishable from an attempt that failed without saying why — and "did it go
#: out" is the question the delivery table exists to answer. The daily digest
#: (`jobs/send_daily_digests.py`) is what carries these rows, and it rewrites this
#: line with what became of each one: sent, failed with the sender's own reason, or
#: held with the reason there was nothing to send.
NOT_ATTEMPTED = "not attempted: queued for the daily digest"


@dataclass(slots=True, frozen=True)
class DeliveryPlan:
    """One row to write into `notification_deliveries`.

    `error` is the reason a row is where it is: a failure for a `failed` row, and
    for a `pending` one the reason nothing has attempted it yet.
    """

    channel: DeliveryChannel
    status: DeliveryStatus
    attempts: int = 0
    error: str | None = None
    sent_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class RaiseOutcome:
    """What one raise did.

    Returned rather than inferred, because a duplicate is a *successful* raise: the
    event is represented exactly once, which is what the caller asked for. The two
    are told apart in the return value so a caller — and the test — can see that
    the second attempt was suppressed rather than lost.
    """

    notification_id: UUID | None
    duplicate: bool

    @property
    def created(self) -> bool:
        return self.notification_id is not None


@dataclass(slots=True, frozen=True)
class NotificationPage:
    """One page of somebody's notifications, plus what it is a page of."""

    items: list[Notification] = field(default_factory=list)
    total: int = 0
    limit: int = 50
    offset: int = 0


__all__ = [
    "DIGEST_CANDIDATE_TYPES",
    "NOT_ATTEMPTED",
    "TITLE_KEY_OF",
    "Delivery",
    "DeliveryChannel",
    "DeliveryPlan",
    "DeliveryStatus",
    "Notification",
    "NotificationDraft",
    "NotificationPage",
    "NotificationType",
    "RaiseOutcome",
]
