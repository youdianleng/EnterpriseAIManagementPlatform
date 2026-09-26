"""Notification error codes.

Derived from the catalogue, for the reason `domain/account/errors.py` records: a
second hand-written list drifts, and the drift only shows when the branch that
raises the missing code runs.
"""

from app.core.errors import ErrorCode


class NotificationErrorCode:
    """The one code this module raises.

    There is deliberately no `NOTIFICATION_NOT_FOUND`. Whether an id exists is
    not the caller's business when the row is not theirs, and a distinct code for
    each case is precisely how an endpoint becomes an existence oracle.
    """

    NOTIFICATION_NOT_YOURS = ErrorCode.NOTIFICATION_NOT_YOURS


__all__ = ["NotificationErrorCode"]
