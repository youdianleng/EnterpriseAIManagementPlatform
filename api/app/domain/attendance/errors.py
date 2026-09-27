"""Attendance error codes.

Derived from the catalogue rather than listed by hand, for the reason
`domain/approval/errors.py` records: a second enumerated list drifts, and the drift
only shows up when the branch that raises the missing code runs.

Six codes, and the boundary between them matters to the client. `ALREADY_CLOCKED_IN`
and `NO_OPEN_SHIFT` are the punches the state machine refuses and the employee can
act on; `EVENT_IN_FUTURE` and `RANGE_INVALID` are malformed requests; `TERMINATED`
is a closed record rather than a permission problem, which is why it is a conflict
and not a 403 — the person pressing the button has not been refused anything, their
record has been closed.
"""

from app.core.errors import ErrorCode


class AttendanceErrorCode:
    """Every code this module raises, plus the lookups it depends on."""

    #: A shift is already open. One at a time: a second clock_in is a mistake
    #: (a double click, a stale tab) rather than the start of a second shift, and
    #: the stream keeps one row so the day's arithmetic stays readable.
    ALREADY_CLOCKED_IN = ErrorCode.ATTENDANCE_ALREADY_CLOCKED_IN
    #: No shift this punch could close: none is open, or the only one that is
    #: began too long ago to be the shift a clock_out ends (`models.MAX_SHIFT`).
    NO_OPEN_SHIFT = ErrorCode.ATTENDANCE_NO_OPEN_SHIFT
    #: The instant is later than now. Working time is a record of what happened.
    EVENT_IN_FUTURE = ErrorCode.ATTENDANCE_EVENT_IN_FUTURE
    #: `employees.status` is `terminated` and the punch is a clock_in: the record
    #: is history. A clock_out is still allowed, because a shift left open when the
    #: termination was applied can be closed and refusing it would freeze an
    #: anomaly nobody can repair.
    EMPLOYEE_TERMINATED = ErrorCode.ATTENDANCE_EMPLOYEE_TERMINATED
    #: `clock` was asked to append a correction. A correction restates an event
    #: that already exists and carries a reason and a target; it is appended by the
    #: correction flow after an approval (ticket 24), never by a clock button.
    CORRECTION_NOT_A_PUNCH = ErrorCode.ATTENDANCE_CORRECTION_NOT_A_PUNCH
    #: An inverted or over-long range. Refused rather than answered with an empty
    #: list, because "nothing there" and "you asked the wrong question" must not
    #: look the same.
    RANGE_INVALID = ErrorCode.ATTENDANCE_RANGE_INVALID
    #: The correction document (ticket 24) — six codes, and the boundary between
    #: them is what the client shows. `NOT_FOUND` and `INVALID` are the request;
    #: `NOT_DRAFT` is a document somebody else has already moved on; the pair that
    #: names no single punch is `TARGET_UNRESOLVED`, the one refusal this flow makes
    #: instead of guessing; `SUBMISSION_REFUSED` and `APPLY_FAILED` are the engine's
    #: answer relayed.
    CORRECTION_NOT_FOUND = ErrorCode.ATTENDANCE_CORRECTION_NOT_FOUND
    CORRECTION_INVALID = ErrorCode.ATTENDANCE_CORRECTION_INVALID
    CORRECTION_NOT_DRAFT = ErrorCode.ATTENDANCE_CORRECTION_NOT_DRAFT
    CORRECTION_TARGET_UNRESOLVED = ErrorCode.ATTENDANCE_CORRECTION_TARGET_UNRESOLVED
    CORRECTION_SUBMISSION_REFUSED = ErrorCode.ATTENDANCE_CORRECTION_SUBMISSION_REFUSED
    CORRECTION_APPLY_FAILED = ErrorCode.ATTENDANCE_CORRECTION_APPLY_FAILED
    #: Grouped here because a caller reasons about "why was this punch refused",
    #: not about which enum a code happens to live in.
    EMPLOYEE_NOT_FOUND = ErrorCode.EMPLOYEE_NOT_FOUND
    INVALID_REQUEST = ErrorCode.INVALID_REQUEST


__all__ = ["AttendanceErrorCode"]
