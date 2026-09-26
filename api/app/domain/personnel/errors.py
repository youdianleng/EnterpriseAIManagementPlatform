"""Personnel change error codes.

Derived from the catalogue rather than listed by hand, for the reason
`domain/account/errors.py` records: an enumerated second list drifts, and the
drift only shows up when the branch that raises the missing code runs.
"""

from app.core.errors import ErrorCode


class PersonnelErrorCode:
    """Every code this module raises, plus the employee lookups it depends on."""

    PERSONNEL_CHANGE_NOT_FOUND = ErrorCode.PERSONNEL_CHANGE_NOT_FOUND
    PERSONNEL_CHANGE_INVALID_PAYLOAD = ErrorCode.PERSONNEL_CHANGE_INVALID_PAYLOAD
    PERSONNEL_CHANGE_EMPLOYEE_REQUIRED = ErrorCode.PERSONNEL_CHANGE_EMPLOYEE_REQUIRED
    PERSONNEL_CHANGE_NOT_DRAFT = ErrorCode.PERSONNEL_CHANGE_NOT_DRAFT
    PERSONNEL_CHANGE_ALREADY_APPLIED = ErrorCode.PERSONNEL_CHANGE_ALREADY_APPLIED
    PERSONNEL_CHANGE_NOT_CANCELLABLE = ErrorCode.PERSONNEL_CHANGE_NOT_CANCELLABLE
    PERSONNEL_CHANGE_APPLY_FAILED = ErrorCode.PERSONNEL_CHANGE_APPLY_FAILED
    #: Grouped here because a caller of this module reasons about "why was this
    #: change refused", not about which enum a code happens to live in.
    EMPLOYEE_NOT_FOUND = ErrorCode.EMPLOYEE_NOT_FOUND
    INVALID_REQUEST = ErrorCode.INVALID_REQUEST


__all__ = ["PersonnelErrorCode"]
