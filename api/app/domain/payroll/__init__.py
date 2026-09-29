"""薪酬档案 — the salary archive (ticket 43).

Two modules matter and the rest is plumbing:

* `models.py` holds the value types, and `SalaryReading` — the sealed container that
  salary rows can only travel in, and the reason "every read writes an audit entry" is a
  property of the type system rather than a convention three routes keep.
* `service.py` holds the one read and the one append, and is the only place this package
  touches the database.

**This package computes nothing.** No tax, no social security, no net pay, no proration
and no currency conversion — `docs/DESIGN.md` D9 makes 不做计算 the decision and §8.3
lists 西班牙工资单计算 among the things the system will not do. Every amount here is a
figure somebody entered, kept exactly as it was entered.
"""

from app.domain.payroll.errors import PayrollErrorCode
from app.domain.payroll.models import (
    ARCHIVE_NOTICE_KEY,
    SALARY_ENTITY,
    AllowanceLine,
    ChangeReasonType,
    PayPeriod,
    Reading,
    RecordInput,
    RecordQuery,
    RecordView,
    SalaryReading,
    SalaryRecord,
)
from app.domain.payroll.service import SalaryService
from app.domain.payroll.windows import in_force

__all__ = [
    "ARCHIVE_NOTICE_KEY",
    "SALARY_ENTITY",
    "AllowanceLine",
    "ChangeReasonType",
    "PayPeriod",
    "PayrollErrorCode",
    "Reading",
    "RecordInput",
    "RecordQuery",
    "RecordView",
    "SalaryReading",
    "SalaryRecord",
    "SalaryService",
    "in_force",
]
