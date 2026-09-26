"""Account error codes.

Derived from the catalogue rather than listed by hand. An earlier version
enumerated the names a second time here, and four of them were missing by the
time the auth module needed them — a failure that only appears when that exact
branch runs. Deriving means a code added to `ErrorCode` is available here
immediately, and one removed cannot linger.
"""

from app.core.errors import ErrorCode


class AccountErrorCode:
    """Every account and session code, plus the shared authentication ones."""

    # Session and authentication codes are grouped here because a caller of this
    # module reasons about "why did sign-in fail", not about which enum a code
    # happens to live in.
    ACCOUNT_NOT_FOUND = ErrorCode.ACCOUNT_NOT_FOUND
    ACCOUNT_USERNAME_TAKEN = ErrorCode.ACCOUNT_USERNAME_TAKEN
    ACCOUNT_EMPLOYEE_HAS_ACCOUNT = ErrorCode.ACCOUNT_EMPLOYEE_HAS_ACCOUNT
    ACCOUNT_EMPLOYEE_NOT_ACTIVE = ErrorCode.ACCOUNT_EMPLOYEE_NOT_ACTIVE
    ACCOUNT_ALREADY_IN_STATE = ErrorCode.ACCOUNT_ALREADY_IN_STATE
    ACCOUNT_PASSWORD_POLICY = ErrorCode.ACCOUNT_PASSWORD_POLICY
    ACCOUNT_INVALID_CREDENTIALS = ErrorCode.ACCOUNT_INVALID_CREDENTIALS
    ACCOUNT_DISABLED = ErrorCode.ACCOUNT_DISABLED
    ACCOUNT_PASSWORD_REUSED = ErrorCode.ACCOUNT_PASSWORD_REUSED
    ACCOUNT_LOCKED = ErrorCode.ACCOUNT_LOCKED
    SESSION_INVALID = ErrorCode.SESSION_INVALID
    PASSWORD_CHANGE_REQUIRED = ErrorCode.PASSWORD_CHANGE_REQUIRED
    ACCOUNT_ROLE_UNKNOWN = ErrorCode.ACCOUNT_ROLE_UNKNOWN
    ACCOUNT_LAST_ADMINISTRATOR = ErrorCode.ACCOUNT_LAST_ADMINISTRATOR


__all__ = ["AccountErrorCode"]
