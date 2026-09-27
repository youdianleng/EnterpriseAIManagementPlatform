"""Notification domain: the centre, its delivery records, and the approval events.

`docs/architecture/codebase-design.md` §1 puts notification beside approval rather
than inside it, and §3 keeps it with one condition attached: a notification sender
resolves recipients through the module that owns the rule and never re-implements
it. `approval.ApprovalNotifier` is that condition made concrete.
"""

from app.domain.notification.approval import ApprovalNotifier
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
    "TITLE_KEY_OF",
    "ApprovalNotifier",
    "Delivery",
    "DeliveryChannel",
    "DeliveryPlan",
    "DeliveryStatus",
    "Notification",
    "NotificationDraft",
    "NotificationErrorCode",
    "NotificationPage",
    "NotificationRepository",
    "NotificationService",
    "NotificationType",
    "RaiseOutcome",
]
