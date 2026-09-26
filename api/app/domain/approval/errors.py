"""Approval error codes.

Derived from the catalogue rather than listed by hand, for the reason
`domain/account/errors.py` records: an enumerated second list drifts, and the
drift only shows up when the branch that raises the missing code runs.
"""

from app.core.errors import ErrorCode


class ApprovalErrorCode:
    """Every code the engine raises, plus the employee lookup it depends on."""

    APPROVAL_NOT_FOUND = ErrorCode.APPROVAL_NOT_FOUND
    APPROVAL_ALREADY_OPEN = ErrorCode.APPROVAL_ALREADY_OPEN
    APPROVAL_APPROVER_UNRESOLVED = ErrorCode.APPROVAL_APPROVER_UNRESOLVED
    APPROVAL_HR_UNAVAILABLE = ErrorCode.APPROVAL_HR_UNAVAILABLE
    APPROVAL_NOT_APPROVER = ErrorCode.APPROVAL_NOT_APPROVER
    APPROVAL_NOT_REQUESTER = ErrorCode.APPROVAL_NOT_REQUESTER
    APPROVAL_NOT_WITHDRAWABLE = ErrorCode.APPROVAL_NOT_WITHDRAWABLE
    APPROVAL_NOT_PENDING = ErrorCode.APPROVAL_NOT_PENDING
    APPROVAL_PREVIOUSLY_REJECTED = ErrorCode.APPROVAL_PREVIOUSLY_REJECTED
    # Grouped here because a caller of this module reasons about "why was this
    # submission refused", not about which enum a code happens to live in.
    EMPLOYEE_NOT_FOUND = ErrorCode.EMPLOYEE_NOT_FOUND
    INVALID_REQUEST = ErrorCode.INVALID_REQUEST


__all__ = ["ApprovalErrorCode"]
