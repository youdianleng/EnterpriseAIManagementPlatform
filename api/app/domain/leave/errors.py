"""Leave error codes.

Derived from the catalogue rather than listed by hand, for the reason
`domain/approval/errors.py` records: a second enumerated list drifts, and the drift
shows up only when the branch that raises the missing code runs.

Four themes, and the boundary between them is what a client shows:

* the request is unusable — inverted dates, a range with no working day in it, a
  type that has been retired, a payload that states nothing (`..._INVALID`);
* the *catalogue* is the caller's to change and they got it wrong
  (`TYPE_CODE_TAKEN`);
* the document is not in a state that admits the act (`REQUEST_NOT_DRAFT`,
  `NOT_WITHDRAWABLE`, `ALREADY_STARTED`), which is a 409 and not a 403: the caller
  owns the document and is being told what its state is;
* the year's allowance does not cover it (`BALANCE_INSUFFICIENT`) — a 409, because
  the request was well-formed and the balance is the conflict, and the detail
  states the remainder, which is the whole point of the refusal.
"""

from app.core.errors import ErrorCode


class LeaveErrorCode:
    """Every code this module raises, plus the lookups it depends on."""

    #: No such type. 404, and only ever a code lookup: a request names a type by
    #: the code it was filed under.
    TYPE_NOT_FOUND = ErrorCode.LEAVE_TYPE_NOT_FOUND
    #: The code is a type's identity for good — a balance refers to the row, and a
    #: report names the code — so reusing one would make two types share a name in
    #: a record that outlives both.
    TYPE_CODE_TAKEN = ErrorCode.LEAVE_TYPE_CODE_TAKEN
    #: Retired from the catalogue: nothing new may be filed under it. A request
    #: already filed under it is untouched.
    TYPE_INACTIVE = ErrorCode.LEAVE_TYPE_INACTIVE
    #: A type that states nothing usable: no code, or a name missing from one of
    #: the two languages the interface ships in.
    TYPE_INVALID = ErrorCode.LEAVE_TYPE_INVALID
    REQUEST_NOT_FOUND = ErrorCode.LEAVE_REQUEST_NOT_FOUND
    #: Inverted dates, or a range that covers no working day for this person —
    #: which is refused rather than accepted as zero days, because a leave that
    #: costs nothing is a leave the balance cannot account for.
    REQUEST_INVALID = ErrorCode.LEAVE_REQUEST_INVALID
    #: Only a draft is the requester's to change or to re-file. A filed document is
    #: what two people were asked to approve.
    REQUEST_NOT_DRAFT = ErrorCode.LEAVE_REQUEST_NOT_DRAFT
    #: This person already has a live request covering one of these dates. Two
    #: overlapping leaves would be two deductions for one absence and one of them
    #: would suppress nothing.
    REQUEST_OVERLAPS = ErrorCode.LEAVE_REQUEST_OVERLAPS
    #: The year's balance does not cover the request. The detail states the
    #: remainder, which is what the ticket asks a refusal to say.
    BALANCE_INSUFFICIENT = ErrorCode.LEAVE_BALANCE_INSUFFICIENT
    #: The leave has begun. Withdrawing it would take back a day somebody is
    #: already away for; the way to correct the record is HR's after-the-fact
    #: correction, and the refusal names it.
    ALREADY_STARTED = ErrorCode.LEAVE_ALREADY_STARTED
    #: Rejected, already withdrawn, or never filed: there is nothing left to
    #: withdraw.
    NOT_WITHDRAWABLE = ErrorCode.LEAVE_NOT_WITHDRAWABLE
    #: The engine refused to file it — one open request for this document, or a
    #: rejection that is final. Carries the engine's own code in the detail.
    SUBMISSION_REFUSED = ErrorCode.LEAVE_SUBMISSION_REFUSED
    #: The type requires an attachment and the request carries no reference to one.
    ATTACHMENT_REQUIRED = ErrorCode.LEAVE_ATTACHMENT_REQUIRED
    #: Grouped here because a caller reasons about "why was this leave refused",
    #: not about which enum a code happens to live in.
    EMPLOYEE_NOT_FOUND = ErrorCode.EMPLOYEE_NOT_FOUND
    INVALID_REQUEST = ErrorCode.INVALID_REQUEST


__all__ = ["LeaveErrorCode"]
