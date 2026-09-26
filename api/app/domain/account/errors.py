"""Account error codes, aliased over the shared catalogue."""

from app.core.errors import ErrorCode


class AccountErrorCode:
    ACCOUNT_NOT_FOUND = ErrorCode.ACCOUNT_NOT_FOUND
    ACCOUNT_USERNAME_TAKEN = ErrorCode.ACCOUNT_USERNAME_TAKEN
    ACCOUNT_EMPLOYEE_HAS_ACCOUNT = ErrorCode.ACCOUNT_EMPLOYEE_HAS_ACCOUNT
    ACCOUNT_EMPLOYEE_NOT_ACTIVE = ErrorCode.ACCOUNT_EMPLOYEE_NOT_ACTIVE
    ACCOUNT_ALREADY_IN_STATE = ErrorCode.ACCOUNT_ALREADY_IN_STATE
    ACCOUNT_PASSWORD_POLICY = ErrorCode.ACCOUNT_PASSWORD_POLICY


__all__ = ["AccountErrorCode"]
