"""Overtime error codes.

Derived from the catalogue rather than listed by hand, for the reason
`domain/approval/errors.py` records: a second enumerated list drifts, and the drift
shows up only when the branch that raises the missing code runs.

Four themes, and the boundary between them is what a client shows:

* the request is unusable — a date that has already passed, minutes no day holds, no
  reason stated, a period that is not a month (`..._INVALID`);
* the day already carries overtime, so nothing may be filed for it
  (`REQUEST_EXISTS`) — a 409, because the request was well-formed and the day is the
  conflict;
* the document or the record is not in a state that admits the act
  (`REQUEST_NOT_DRAFT`, `NOT_WITHDRAWABLE`, `RECORD_NOT_SETTLED`) — 409s, not 403s:
  the caller owns the document and is being told what its state is;
* the *machine* is what failed (`RESOLVE_FAILED`, `SUBMISSION_REFUSED`), and the
  settlement stands while the record is retried.
"""

from app.core.errors import ErrorCode


class OvertimeErrorCode:
    """Every code this module raises, plus the lookups it depends on."""

    REQUEST_NOT_FOUND = ErrorCode.OVERTIME_REQUEST_NOT_FOUND
    RECORD_NOT_FOUND = ErrorCode.OVERTIME_RECORD_NOT_FOUND
    #: A date before today in Madrid — overtime is applied for in advance —, minutes
    #: outside a day's bounds, or a request whose reason states nothing.
    REQUEST_INVALID = ErrorCode.OVERTIME_REQUEST_INVALID
    #: Only a draft is the requester's to change or to file. A filed document is what
    #: two people were asked to approve.
    REQUEST_NOT_DRAFT = ErrorCode.OVERTIME_REQUEST_NOT_DRAFT
    #: Somebody already has a live request — or an approved record — for that day.
    #: Overtime is counted once per person per day, and the monthly total is a sum of
    #: facts rather than of intentions.
    REQUEST_EXISTS = ErrorCode.OVERTIME_REQUEST_EXISTS
    #: HR's figure is stored beside the computed one, so there has to be one: a record
    #: whose day has not been computed has no original to confirm against.
    RECORD_NOT_SETTLED = ErrorCode.OVERTIME_RECORD_NOT_SETTLED
    #: The engine approved the request and the record could not be written. The sweep
    #: retries it, which is the honest reading of "approved and not yet recorded".
    RESOLVE_FAILED = ErrorCode.OVERTIME_RESOLVE_FAILED
    #: The engine refused to file it — one open request for this document, or a
    #: rejection that is final. Carries the engine's own code in the detail.
    SUBMISSION_REFUSED = ErrorCode.OVERTIME_SUBMISSION_REFUSED
    #: Not `YYYY-MM`, or outside the years this module holds.
    PERIOD_INVALID = ErrorCode.OVERTIME_PERIOD_INVALID
    #: Approved, rejected already, never filed: nothing left to withdraw. An approved
    #: request's record is corrected by HR's confirmation, and the refusal names it.
    NOT_WITHDRAWABLE = ErrorCode.OVERTIME_NOT_WITHDRAWABLE
    #: Grouped here because a caller reasons about "why was this overtime refused",
    #: not about which enum a code happens to live in.
    EMPLOYEE_NOT_FOUND = ErrorCode.EMPLOYEE_NOT_FOUND
    INVALID_REQUEST = ErrorCode.INVALID_REQUEST


__all__ = ["OvertimeErrorCode"]
