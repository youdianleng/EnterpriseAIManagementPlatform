"""Notification domain: the centre, its delivery records, and the approval events.

`docs/architecture/codebase-design.md` §1 puts notification beside approval rather
than inside it, and §3 keeps it with one condition attached: a notification sender
resolves recipients through the module that owns the rule and never re-implements
it. `approval.ApprovalNotifier` is that condition made concrete.

`digest` and `digest_service` are the module's third part: the one thing here that
renders a sentence, because its reader is a mail client with no dictionary (ticket
20). The recipient rule is not re-implemented there either — the digest routes by
the same `COALESCE(override, position's manager, department's manager)` that
`PostgresAttendanceRepository.notification_route` resolves for one punch.
"""

from app.domain.notification.approval import ApprovalNotifier
from app.domain.notification.digest import (
    DIGEST_PATH,
    DigestContent,
    DigestLanguage,
    DigestReport,
    language_of,
    render,
)
from app.domain.notification.digest_repository import (
    DigestRecipient,
    DigestRepository,
)
from app.domain.notification.digest_service import DailyDigest, DigestRunReport
from app.domain.notification.errors import NotificationErrorCode
from app.domain.notification.models import (
    DIGEST_CANDIDATE_TYPES,
    TITLE_KEY_OF,
    Delivery,
    DeliveryChannel,
    DeliveryPlan,
    DeliveryStatus,
    Notification,
    NotificationDraft,
    NotificationPage,
    NotificationType,
    RaiseOutcome,
)
from app.domain.notification.repository import NotificationRepository
from app.domain.notification.service import NotificationService

__all__ = [
    "DIGEST_CANDIDATE_TYPES",
    "DIGEST_PATH",
    "TITLE_KEY_OF",
    "ApprovalNotifier",
    "DailyDigest",
    "Delivery",
    "DeliveryChannel",
    "DeliveryPlan",
    "DeliveryStatus",
    "DigestContent",
    "DigestLanguage",
    "DigestRecipient",
    "DigestReport",
    "DigestRepository",
    "DigestRunReport",
    "Notification",
    "NotificationDraft",
    "NotificationErrorCode",
    "NotificationPage",
    "NotificationRepository",
    "NotificationService",
    "NotificationType",
    "RaiseOutcome",
    "language_of",
    "render",
]
