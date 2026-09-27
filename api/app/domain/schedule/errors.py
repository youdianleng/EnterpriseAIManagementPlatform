"""Schedule error codes.

Derived from the catalogue rather than listed by hand, for the reason
`domain/attendance/errors.py` records: a second enumerated list drifts, and the
drift only shows up when the branch that raises the missing code runs. The names
here are the catalogue's own, which is what `tests/test_error_aliases.py` asserts of
every alias class — there is nothing to strip and nothing to remember.

The boundary between these matters to a caller. `SCHEDULE_INVALID_DAY` is a
malformed pattern — a window that disagrees with its own minutes — and is the
caller's to fix. `SCHEDULE_NOT_FOUND` and `SCHEDULE_CODE_TAKEN` are ordinary
administration conflicts. `SCHEDULE_OVERRIDE_OVERLAPS` is a conflict with an
existing decision about the same person, which the database also refuses;
`SCHEDULE_HOLIDAY_EXISTS` is the same shape, and the import path treats it as an
update rather than a refusal because that is what re-importing a corrected file is.
"""

from app.core.errors import ErrorCode


class ScheduleErrorCode:
    """Every code this module raises, plus the lookups it depends on."""

    #: The schedule named does not exist — as a department's, an override's, or a
    #: company default.
    SCHEDULE_NOT_FOUND = ErrorCode.SCHEDULE_NOT_FOUND
    #: Two schedules cannot share a code: the code is what HR reads in a report and
    #: what a later import or integration would name one by.
    SCHEDULE_CODE_TAKEN = ErrorCode.SCHEDULE_CODE_TAKEN
    #: A day whose window, break and expected minutes do not agree, a weekday given
    #: twice, a schedule with no days, or a week that does not end after it starts.
    #: One code rather than one per shape: they are all "this pattern is not a
    #: pattern", and the detail says which.
    SCHEDULE_INVALID_DAY = ErrorCode.SCHEDULE_INVALID_DAY
    #: A department already has an active schedule, or the company already has a
    #: default. Resolution has one answer by construction, and this is where a
    #: second one is refused.
    SCHEDULE_ALREADY_SET = ErrorCode.SCHEDULE_ALREADY_SET
    #: An override was asked for a schedule that is deactivated. An existing
    #: override survives deactivation — that is what makes deactivating a
    #: part-time pattern safe — but a new one is not written against it.
    SCHEDULE_INACTIVE = ErrorCode.SCHEDULE_INACTIVE
    #: Two overrides for one employee covering the same day. Refused by an
    #: exclusion constraint as well: which one wins is not a question the resolver
    #: should ever have to answer.
    SCHEDULE_OVERRIDE_OVERLAPS = ErrorCode.SCHEDULE_OVERRIDE_OVERLAPS
    #: The override named does not exist, or belongs to somebody else.
    SCHEDULE_OVERRIDE_NOT_FOUND = ErrorCode.SCHEDULE_OVERRIDE_NOT_FOUND
    #: A holiday that cannot be stored as stated: no region where one is required,
    #: a region where none belongs, or a year that disagrees with its date.
    SCHEDULE_INVALID_HOLIDAY = ErrorCode.SCHEDULE_INVALID_HOLIDAY
    #: The holiday named does not exist.
    SCHEDULE_HOLIDAY_NOT_FOUND = ErrorCode.SCHEDULE_HOLIDAY_NOT_FOUND
    #: That date, scope and region is already a holiday.
    SCHEDULE_HOLIDAY_EXISTS = ErrorCode.SCHEDULE_HOLIDAY_EXISTS
    #: An import file that cannot be read as a holiday calendar. Refused whole
    #: rather than half-applied: a calendar that is partly loaded is a payroll
    #: figure that is partly wrong.
    SCHEDULE_INVALID_HOLIDAY_FILE = ErrorCode.SCHEDULE_INVALID_HOLIDAY_FILE
    #: Grouped here because a caller reasons about "why was this refused", not
    #: about which enum a code happens to live in.
    EMPLOYEE_NOT_FOUND = ErrorCode.EMPLOYEE_NOT_FOUND
    INVALID_REQUEST = ErrorCode.INVALID_REQUEST


__all__ = ["ScheduleErrorCode"]
