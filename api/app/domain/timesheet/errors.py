"""Timesheet error codes.

Aliases of the catalogue rather than a second list, for the reason
`domain/personnel/errors.py` records: an enumerated copy drifts, and the drift
only shows up in the branch that raises the missing code.
"""

from app.core.errors import ErrorCode


class TimesheetErrorCode:
    """Every code this module raises, plus the lookups it depends on."""

    TIMESHEET_NOT_FOUND = ErrorCode.TIMESHEET_NOT_FOUND
    TIMESHEET_WEEK_NOT_MONDAY = ErrorCode.TIMESHEET_WEEK_NOT_MONDAY
    TIMESHEET_ALREADY_EXISTS = ErrorCode.TIMESHEET_ALREADY_EXISTS
    TIMESHEET_NOT_EDITABLE = ErrorCode.TIMESHEET_NOT_EDITABLE
    TIMESHEET_ENTRY_NOT_FOUND = ErrorCode.TIMESHEET_ENTRY_NOT_FOUND
    TIMESHEET_ENTRY_TASK_MISMATCH = ErrorCode.TIMESHEET_ENTRY_TASK_MISMATCH
    TIMESHEET_ENTRY_PROJECT_NOT_RECORDABLE = ErrorCode.TIMESHEET_ENTRY_PROJECT_NOT_RECORDABLE
    TIMESHEET_ENTRY_OUTSIDE_PROJECT_DATES = ErrorCode.TIMESHEET_ENTRY_OUTSIDE_PROJECT_DATES
    TIMESHEET_ENTRY_MINUTES_INVALID = ErrorCode.TIMESHEET_ENTRY_MINUTES_INVALID
    TIMESHEET_NOT_YOURS = ErrorCode.TIMESHEET_NOT_YOURS
    TIMESHEET_COPY_SOURCE_INVALID = ErrorCode.TIMESHEET_COPY_SOURCE_INVALID
    TIMESHEET_COPY_TARGET_NOT_EMPTY = ErrorCode.TIMESHEET_COPY_TARGET_NOT_EMPTY
    TIMESHEET_SUBMISSION_REFUSED = ErrorCode.TIMESHEET_SUBMISSION_REFUSED
    #: Grouped here because a caller of this module reasons about "why was this week
    #: refused", not about which enum a code happens to live in.
    INVALID_REQUEST = ErrorCode.INVALID_REQUEST


__all__ = ["TimesheetErrorCode"]
