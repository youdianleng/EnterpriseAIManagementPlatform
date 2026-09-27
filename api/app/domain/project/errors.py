"""Project error codes.

Aliases of the catalogue rather than a second list, for the reason
`domain/personnel/errors.py` records: an enumerated copy drifts, and the drift
only shows up in the branch that raises the missing code.
"""

from app.core.errors import ErrorCode


class ProjectErrorCode:
    """Every code this module raises, plus the lookups it depends on."""

    PROJECT_NOT_FOUND = ErrorCode.PROJECT_NOT_FOUND
    PROJECT_CODE_TAKEN = ErrorCode.PROJECT_CODE_TAKEN
    PROJECT_DATES_INVALID = ErrorCode.PROJECT_DATES_INVALID
    PROJECT_DEPARTMENT_NOT_FOUND = ErrorCode.PROJECT_DEPARTMENT_NOT_FOUND
    PROJECT_MANAGER_NOT_FOUND = ErrorCode.PROJECT_MANAGER_NOT_FOUND
    PROJECT_ARCHIVED = ErrorCode.PROJECT_ARCHIVED
    PROJECT_NOT_ACTIVE = ErrorCode.PROJECT_NOT_ACTIVE
    PROJECT_TASK_NOT_FOUND = ErrorCode.PROJECT_TASK_NOT_FOUND
    PROJECT_TASK_CODE_TAKEN = ErrorCode.PROJECT_TASK_CODE_TAKEN
    PROJECT_TASK_NOT_RECORDABLE = ErrorCode.PROJECT_TASK_NOT_RECORDABLE
    PROJECT_NOT_MANAGEABLE = ErrorCode.PROJECT_NOT_MANAGEABLE
    PROJECT_TASK_ALREADY_INACTIVE = ErrorCode.PROJECT_TASK_ALREADY_INACTIVE
    #: Grouped here because a caller of this module reasons about "why was this
    #: project refused", not about which enum a code happens to live in.
    INVALID_REQUEST = ErrorCode.INVALID_REQUEST


__all__ = ["ProjectErrorCode"]
