"""The salary archive's own error codes.

Derived from the catalogue rather than listed by hand, for the reason
`domain/approval/errors.py` records: a second enumerated list drifts, and the drift
shows up only when the branch that raises the missing code runs.

Two themes, and the boundary between them is what a client shows:

* **the record is unusable** — no change reason, an inverted date range, an unknown pay
  period, an amount that is not a positive figure with at most two decimal places
  (`RECORD_INVALID`);
* **the day is already claimed** — a record that would cover a date another record
  already covers (`RECORD_OVERLAPS`), which is a 409 rather than a 422: the request was
  well-formed and the archive is what collided.

There is deliberately no `..._COMPUTATION_FAILED` and no code for a currency, a tax
rate or a net figure: this module has no arithmetic to fail, and D9's non-goal is the
reason. `tests/test_salary_records.py` asserts the schema holds no computed column, so
a code that named one could not be reached even if somebody added it here.
"""

from app.core.errors import ErrorCode


class PayrollErrorCode:
    """Every code this module raises, plus the lookups it depends on."""

    #: No such record, for the caller who may read the archive at all. A caller who may
    #: not is refused before this is reached, so the two answers stay distinguishable.
    RECORD_NOT_FOUND = ErrorCode.SALARY_RECORD_NOT_FOUND
    #: The record could not be stored as written: a blank change reason, a range that
    #: ends before it starts, an unknown period, a non-positive amount, more than two
    #: decimal places, a currency that is not ISO 4217.
    RECORD_INVALID = ErrorCode.SALARY_RECORD_INVALID
    #: The window already holds a record. Refused here *and* unrepresentable in the
    #: database: the exclusion constraint is the second line, and this code is what a
    #: client reads. Its detail names the date that collided.
    RECORD_OVERLAPS = ErrorCode.SALARY_RECORD_OVERLAPS
    #: Somebody already has an `initial` record, so a second one is the same hire entered
    #: twice. A separate code from `RECORD_OVERLAPS` because the fix is different: the
    #: caller should enter an adjustment rather than move a date.
    INITIAL_EXISTS = ErrorCode.SALARY_RECORD_INITIAL_EXISTS
    #: Grouped here because a caller reasons about "why was this salary record refused",
    #: not about which enum a code happens to live in.
    EMPLOYEE_NOT_FOUND = ErrorCode.EMPLOYEE_NOT_FOUND


__all__ = ["PayrollErrorCode"]
