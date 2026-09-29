"""The payslip module's own error codes.

Derived from the catalogue rather than listed by hand, for the reason
`domain/approval/errors.py` records: a second enumerated list drifts, and the drift
shows up only when the branch that raises the missing code runs.

Four themes, and the boundary between them is what a client shows:

* **the request is unusable** — a month that is not `YYYY-MM`, or a batch with no files
  at all (`PERIOD_INVALID`, `BATCH_EMPTY`), both 422s the caller fixes by sending
  something else;
* **the request names somebody who is not there** (`EMPLOYEE_NOT_FOUND`), which is the
  family's existing 404 rather than a new code: "that employee id does not exist" is the
  same fact wherever it is asked;
* **the file cannot be a payslip** (`FILE_EMPTY`, `FILE_NOT_PDF`) — a 422 about the file
  the caller chose. These are the codes the *single-file* answer would use; the batch
  upload reports them **per file** in the result instead of failing the request, which is
  the whole of 「不静默丢弃」. They exist because a client that uploads one file through
  another surface, and ticket 45's download half, still need a code to render.
* **the corpus exclusion is not an error here**, and that is worth stating: a payslip
  being unreachable by retrieval is a property of where it is stored, not a refusal a
  caller can trip over. There is deliberately no `PAYSLIP_IN_CORPUS` code, because there
  is no path that could produce one.
"""

from app.core.errors import ErrorCode


class PayslipErrorCode:
    """Every code this module raises, plus the lookups it depends on."""

    #: The month is not `YYYY-MM`, or names a month outside the years this module holds.
    #: Its own code rather than `INVALID_REQUEST`, because the remedy is to send the right
    #: month rather than to correct the rest of the request (the shape
    #: `overtime.period_invalid` established).
    PERIOD_INVALID = ErrorCode.PAYSLIP_PERIOD_INVALID
    #: A batch with no files. Refused rather than answered with an empty result: an upload
    #: with nothing in it is a client that lost the files, and answering 201 would record a
    #: batch that silently filed nothing.
    BATCH_EMPTY = ErrorCode.PAYSLIP_BATCH_EMPTY
    #: The employee a request names does not exist.
    EMPLOYEE_NOT_FOUND = ErrorCode.EMPLOYEE_NOT_FOUND
    #: Zero bytes: not a payslip, and not something this module will store.
    FILE_EMPTY = ErrorCode.PAYSLIP_FILE_EMPTY
    #: The file is not a PDF. The extension decides (`document.files.accept`'s rule), and
    #: the first bytes are checked as well, because a renamed file is the shape of mistake
    #: this produces.
    FILE_NOT_PDF = ErrorCode.PAYSLIP_FILE_NOT_PDF
    #: The row names a file the storage root does not hold. A data error rather than a
    #: permission one, and its own code so an operator can tell the two apart — the same
    #: distinction `document.file_missing` draws.
    FILE_MISSING = ErrorCode.PAYSLIP_FILE_MISSING


__all__ = ["PayslipErrorCode"]
