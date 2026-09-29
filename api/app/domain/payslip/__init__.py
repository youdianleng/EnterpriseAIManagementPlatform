"""工资单 — the payslips (ticket 44).

Four modules matter and the rest is plumbing:

* `models.py` holds the value objects, and `UnmatchedReason` — the closed vocabulary that
  makes 「不静默丢弃」 a property of the type rather than of a reviewer's attention.
* `matching.py` holds the one matching rule: the employee number in the filename, the
  explicit selection, and what happens when the two disagree.
* `export.py` holds the missing list as a file — the columns, the bilingual header, and
  the statement of what the file refuses to carry.
* `service.py` holds the upload, the derived missing list and the export, and is the only
  place this package touches the database.

**A payslip is not a document.** It is not stored in `documents`, it produces no chunks,
and nothing here can enter the retrieval corpus — see `app/models/payslip.py` for why the
flag that keeps personal documents out of the corpus is the wrong machinery for this, and
`service.py` for the storage decision that replaces it.

**This package computes nothing.** No amount is read from a payslip's contents, no total
is produced and no tax is mentioned; the file is stored and handed back (D9). The one
piece of arithmetic is turning `2026-03` into the range `2026-03-01 .. 2026-03-31`, which
is what the salary archive's window predicate is asked about.
"""

from app.domain.payslip.errors import PayslipErrorCode
from app.domain.payslip.export import EXPORT_COLUMNS, EXPORT_EXCLUDES, file_for
from app.domain.payslip.matching import Resolution, classify, resolve, tokens
from app.domain.payslip.models import (
    BATCH_ENTITY,
    MAX_BATCH_FILES,
    MAX_PAYSLIP_BYTES,
    PAYSLIP_ENTITY,
    AttributedPayslip,
    BatchOutcome,
    BatchPage,
    BatchRecord,
    EmployeeRef,
    ExportFile,
    MissingEmployee,
    MissingList,
    Payslip,
    PayslipStatus,
    UnmatchedFile,
    UnmatchedReason,
    UploadedFile,
    parse_period,
    period_bounds,
)
from app.domain.payslip.service import PayslipService

__all__ = [
    "BATCH_ENTITY",
    "EXPORT_COLUMNS",
    "EXPORT_EXCLUDES",
    "MAX_BATCH_FILES",
    "MAX_PAYSLIP_BYTES",
    "PAYSLIP_ENTITY",
    "AttributedPayslip",
    "BatchOutcome",
    "BatchPage",
    "BatchRecord",
    "EmployeeRef",
    "ExportFile",
    "MissingEmployee",
    "MissingList",
    "Payslip",
    "PayslipErrorCode",
    "PayslipService",
    "PayslipStatus",
    "Resolution",
    "UnmatchedFile",
    "UnmatchedReason",
    "UploadedFile",
    "classify",
    "file_for",
    "parse_period",
    "period_bounds",
    "resolve",
    "tokens",
]
