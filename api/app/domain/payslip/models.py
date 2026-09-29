"""The payslip module as the rest of the system sees it.

Five things in this file carry the ticket's weight, and each is a shape rather than a
rule somebody has to remember:

1. **`UnmatchedReason` is a closed vocabulary, and every uploaded file carries one.**
   「不静默丢弃」 is not "log the ones you could not place": it is that a file which was
   not attributed appears in the result **with its reason**, and the reason is one of a
   fixed set so the screen, the audit trail and a test can all agree about what happened.
   A file that vanished would be a payslip somebody never receives, which is the failure
   the whole screen exists to prevent — so the batch's answer is built from the files it
   was given and the two lists partition them.

2. **The matching rule is one function, and its precedence is stated once.** The employee
   number parsed out of the filename is the ordinary route, and an explicit selection
   settles what the filename could not; where the two *disagree* the selection wins and
   the reason records both, so nothing is attributed against the filename's own evidence
   without saying so. `matching.py` is that rule in Python, and it takes the candidate
   employee numbers as data rather than a session — the one thing it cannot decide by
   itself is "which staff numbers exist", and that is the repository's answer.

3. **`MAX_PAYSLIP_BYTES` is 10 MiB, and it is the module's own bound rather than the
   document ceiling.** A payslip is one or two pages, so a twenty-megabyte "payslip" is a
   mistake worth naming; reporting it as `oversized` in the result is more useful than
   accepting a file nobody meant to send. It is deliberately **not**
   `document.files.MAX_UPLOAD_BYTES`: that number is the knowledge base's, and a payslip
   is not a document (see `app/models/payslip.py` for why it is not one).

4. **`EmployeeRef` carries the staff number, and that is the point of the screen.** A
   payslip's filename is matched on `employee_no`, and both lists state it so finance can
   see *which* number a file carried and which number a missing person has. The number is
   a withheld field (`domain/employee/visibility.py`), so it reaches this surface only
   because the surface is `finance`'s: `payslip.manage` is the action, and the module
   never serves these rows to anybody else.

5. **There is no amount anywhere in this file, and none may be added.** `Payslip` holds a
   storage key, a size, a checksum and a month; `MissingEmployee` holds an identity, a
   name, a department and the salary record's *window*. Not one of them is money, because
   D9 makes 西班牙工资单计算 the system's explicit non-goal: a `net_pay` field on this
   dataclass would be the first line of one.
"""

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from app.core.errors import ErrorCode
from app.domain.errors import DomainError
from app.domain.payslip.errors import PayslipErrorCode

#: The entity type the audit trail files this module's acts under. One string, so
#: `audit_log` can be filtered to the payslips with an equality rather than a list.
PAYSLIP_ENTITY = "payslip"
BATCH_ENTITY = "payslip_batch"

#: How big a payslip may be, in bytes. See the module docstring: this is the module's own
#: bound, not the knowledge base's, and a file over it is *reported* rather than stored.
MAX_PAYSLIP_BYTES = 10 * 1024 * 1024

#: How many files one batch may carry. A ceiling rather than a target: a request with a
#: thousand files is a client that has lost track of what it is sending, and the bound is
#: what keeps one upload from being an unbounded transaction.
MAX_BATCH_FILES = 400

#: The longest `YYYY-MM` value, and the two digit groups the regex reads. The range is
#: bounded to years a payslip could plausibly be for — the same shape
#: `overtime.OVERTIME_PERIOD` guards, and for the same reason: `0000-01` is a typo with a
#: valid format, and answering it with an empty list would hide the typo.
PERIOD_PATTERN = re.compile(r"^(?P<year>\d{4})-(?P<month>\d{2})$")
MIN_PERIOD_YEAR = 2000
MAX_PERIOD_YEAR = 2100

#: The PDF magic. Checked on the first bytes as well as the extension, because the
#: interesting mistake is a file that was renamed — and because the extension is what the
#: *client* chose. Both have to say PDF: a `.pdf` whose bytes are a JPEG is refused, and so
#: is a PDF named `.txt`.
PDF_MAGIC = b"%PDF-"


class PayslipStatus(StrEnum):
    """The two states a payslip can be in (§3.5's `status`).

    `published` is what an upload writes, and it is the only value that reaches the
    employee: the row-level policy on `payslips` admits the owner for a `published` row
    and for nothing else, so 只有已发布的对员工可见 is a property of the table rather than
    of a query somebody remembered to filter. `withdrawn` is ticket 46's act — this ticket
    stores the column, the CHECK that a withdrawal states when and why, and no way to
    write one.
    """

    PUBLISHED = "published"
    WITHDRAWN = "withdrawn"


class UnmatchedReason(StrEnum):
    """Why a file was not attributed to an employee.

    A closed vocabulary, and every value is a *fact about the file* rather than a sentence:
    the screen renders the reason in the reader's language, and the audit trail stores the
    token. **There is deliberately no `unknown`**: every path through `matching.resolve`
    returns one of these, so "the file was dropped for a reason nobody recorded" is not a
    state the type can express — it is the state 「不静默丢弃」 exists to remove.
    """

    #: No token in the filename equals any staff number on file. The ordinary case: a file
    #: named `nomina_marzo.pdf`, or one whose number belongs to somebody who has left.
    NO_EMPLOYEE_NUMBER = "no_employee_number"
    #: A number was recognised, and no employee holds it. Separate from the value above
    #: because the two are different mistakes: one is a filename with no number in it, the
    #: other is a number finance has to check against the payroll file.
    UNKNOWN_EMPLOYEE_NUMBER = "unknown_employee_number"
    #: The filename names two different employees. Refused rather than attributed to
    #: whichever was found first — a payslip is one person's, and a guess here sends
    #: somebody else's pay to the wrong mailbox.
    AMBIGUOUS_EMPLOYEE_NUMBER = "ambiguous_employee_number"
    #: The bytes are not a PDF, whatever the name says.
    NOT_A_PDF = "not_a_pdf"
    #: Over `MAX_PAYSLIP_BYTES`. Refused rather than truncated: half a payslip that opens
    #: is worse than a refusal, because nothing downstream can tell.
    OVERSIZED_FILE = "oversized_file"
    #: Zero bytes — an upload that carried a name and nothing else.
    EMPTY_FILE = "empty_file"
    #: The selection and the filename named different employees, and a batch may not
    #: contain two files for one employee. Both are named in the detail.
    #:
    #: **This is the collision the precedence rule produces, and it is reported rather
    #: than resolved.** The selection wins (see `matching.resolve`), so the *other*
    #: employee would have no payslip in the batch — and a screen whose whole value is
    #: 「who is missing one」 must not silently make somebody missing by picking a winner.
    DUPLICATE_FOR_EMPLOYEE = "duplicate_for_employee"
    #: The same file was sent twice in one batch, under one name or two. Refused because
    #: the second copy would upsert the first: the row would carry the second's checksum
    #: and the uploader would have two lines for one employee and no way to tell which
    #: file is stored.
    DUPLICATE_FILE = "duplicate_file"


@dataclass(frozen=True, slots=True)
class UploadedFile:
    """One file as it arrived: the bytes, the name, and the employee a client named.

    `selected_employee_id` is the explicit selection the ticket's checklist allows
    (「系统通过文件名中的员工编号或界面选择完成归属匹配」). It is `None` for the ordinary
    upload, where the filename is the whole answer.
    """

    content: bytes
    filename: str | None
    selected_employee_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class EmployeeRef:
    """An employee as this module names one: the id, the staff number, the name.

    `employee_no` is nullable because it is a withheld field rather than a required one —
    `employee_private.employee_no` has no NOT NULL — and a person with no staff number is
    a real state: they cannot be matched by filename, and they appear in the missing list
    with a blank number. That is more useful than dropping them, which would make "nobody
    is missing a payslip" true by omission.
    """

    employee_id: UUID
    employee_no: str | None
    employee_name: str
    department_name: str | None = None


@dataclass(frozen=True, slots=True)
class UnmatchedFile:
    """A file that was not attributed, and why. The ticket's 「单独列出并说明原因」."""

    filename: str
    reason: UnmatchedReason
    employee_no: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class Payslip:
    """One stored payslip, as the module reads it. **No money, and no file contents.**

    `content_sha256` and `file_size` are the two facts the ticket asks to record 「用于事后
    核对是否被替换」, and `original_filename` is what the file is handed back as. What is
    deliberately absent is anything derived from the file: this row is stored and served,
    never read for its contents (D9).
    """

    id: UUID
    employee_id: UUID
    period: str
    storage_path: str
    file_size: int
    content_sha256: str
    original_filename: str
    uploaded_by_user_id: UUID
    batch_id: UUID
    status: str
    download_count: int = 0
    withdrawn_at: datetime | None = None
    withdraw_reason: str | None = None
    created_at: datetime | None = None

    @property
    def is_published(self) -> bool:
        """True while the employee may be handed this file."""
        return self.status == PayslipStatus.PUBLISHED

    @property
    def stored_size(self) -> str:
        """The size as a string, so a client cannot turn bytes into a float.

        Not a formatting convenience: a byte count is an integer, JSON's number type is
        a float, and the response carries it as a string for the same reason ticket 43's
        amounts are strings — there is one way to write a value on the wire and it is
        stated in one place.
        """
        return str(self.file_size)


@dataclass(frozen=True, slots=True)
class AttributedPayslip:
    """A `Payslip` with the employee it belongs to, and whether it replaced one.

    `replaced` is the batch's answer to 「重复上传被视为替换」: the row already existed for
    that `(employee, period)`, so this upload overwrote the file it named. `previous_sha256`
    is what the row held before, which is what makes the replacement checkable afterwards
    rather than merely announced — a `null` there means nothing was replaced.

    `previous` is that row, whole, and it is here so `BatchOutcome.reserved` can state the
    same thing on both halves of the flow: `_store` reads the row before it writes and keeps
    it, which is cheaper and truer than re-deriving "what was there" from two columns after
    the fact. The two scalar fields stay because a response reports the hash and the size and
    has no business serialising a whole row.
    """

    payslip: Payslip
    employee: EmployeeRef
    replaced: bool
    previous_sha256: str | None = None
    previous_file_size: int | None = None
    previous: Payslip | None = None


@dataclass(frozen=True, slots=True)
class MissingEmployee:
    """Somebody who was expected to have a payslip for this month and has none.

    Derived from the payroll archive and from employment, never from the uploaded set —
    which is the ticket's 「依据在职状态与薪酬档案判定」 and the reason this list is worth
    reading: it answers "who should have one" rather than "which files did I forget".

    `salary_effective_from` is the window of the record that was in force, carried so the
    screen can say *why* this person was expected. It is a **date**, never an amount: what
    somebody earns is the archive's business and this list does not restate it, which is
    also what keeps a downloaded file from becoming a payroll document.
    """

    employee: EmployeeRef
    salary_effective_from: date
    salary_effective_to: date | None = None


@dataclass(frozen=True, slots=True)
class BatchOutcome:
    """One upload's whole answer: what it attributed, what it refused, who is missing.

    The three lists **partition** what happened, and that is the property the ticket's
    「不静默丢弃」 turns into: every file the request carried is either in `attributed` or in
    `unmatched` with a reason, and there is no third place for one to be. `total_count` is
    the number of files the request carried, so a test can assert the partition rather than
    trust it.
    """

    batch_id: UUID
    period: str
    uploaded_by_user_id: UUID
    total_count: int
    attributed: tuple[AttributedPayslip, ...]
    unmatched: tuple[UnmatchedFile, ...]
    missing: tuple[MissingEmployee, ...]
    created_at: datetime | None = None
    #: False for the **dry run**: the matching has been done and nothing has been written,
    #: which is the half of the flow §6.3's overwrite rule needs. A client shows the two
    #: lists, asks about the overwrite, and sends the same request again to commit.
    confirmed: bool = True
    #: The payslips the rows already held for this month and these employees, whatever their
    #: status — a fact about the *month*, so **the same set on both halves of the flow**: the
    #: dry run reads it to say 「将覆盖 X 名员工的 Y 月工资单」 and the commit reports the rows its
    #: own writes replaced. Reporting it only on the dry run gave one field two meanings
    #: ("overwrites awaiting confirmation" versus "overwrites performed"), and left a client
    #: that confirmed a batch reading `reserved: 0` beside `replaced: 1` unable to tell whether
    #: the replacement had happened. What the answer *did* about these rows is `replaced_count`,
    #: which is the only field whose meaning differs between the halves.
    reserved: tuple[Payslip, ...] = ()

    @property
    def success_count(self) -> int:
        return len(self.attributed)

    @property
    def replaced_count(self) -> int:
        return sum(1 for entry in self.attributed if entry.replaced)

    @property
    def accounted_for(self) -> int:
        """How many files the two lists together account for. Equals `total_count`."""
        return len(self.attributed) + len(self.unmatched)

    def partitioned(self) -> bool:
        """Whether every uploaded file is in exactly one of the two lists.

        A method rather than a comment because it is the rule the screen's trustworthiness
        rests on, and `tests/test_payslips.py` asserts it on every batch it builds.
        """
        return self.accounted_for == self.total_count


@dataclass(frozen=True, slots=True)
class MissingList:
    """The month's missing employees, on their own.

    Its own value object because the missing list is served by an endpoint of its own —
    the screen reads it before anything is uploaded, and the export renders it — and a
    reader that had to hold a batch to see it would be reading a batch that does not
    exist yet.
    """

    period: str
    items: tuple[MissingEmployee, ...] = ()
    #: How many expected employees the live derivation *looked at*. Not the same as
    #: `len(items)`: this counts the people who were expected to have a payslip, whether
    #: or not one was found. Stated because a screen that showed only the misses would
    #: not distinguish "nobody is missing" from "nobody was expected".
    expected: int = 0

    @property
    def missing(self) -> int:
        return len(self.items)


@dataclass(frozen=True, slots=True)
class ExportFile:
    """A file, ready to be sent. In memory, for the reason the timesheet export gives."""

    filename: str
    content: str

    @property
    def content_type(self) -> str:
        return "text/csv; charset=utf-8"


@dataclass(frozen=True, slots=True)
class BatchRecord:
    """One stored batch, as a listing reads it. The counts, and who was missing then."""

    id: UUID
    period: str
    uploaded_by_user_id: UUID
    total_count: int
    success_count: int
    missing_employee_ids: tuple[UUID, ...] = ()
    unmatched: tuple[UnmatchedFile, ...] = ()
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class BatchPage:
    """One page of a month's uploads, newest first."""

    items: list[BatchRecord] = field(default_factory=list)
    total: int = 0
    limit: int = 20
    offset: int = 0


def content_digest(content: bytes) -> str:
    """`sha256` of the bytes, lowercase hex — the value `content_sha256` stores.

    The same function `domain/document/storage.py` uses, imported from there rather than
    copied: the checksum is the same fact in both modules, and two implementations of it
    would be one place for a digest to disagree with the file it describes.
    """
    return hashlib.sha256(content).hexdigest()


def parse_period(value: Any) -> str:
    """A month as this module stores it — `YYYY-MM` — or a refusal naming the field.

    Deliberately strict: `2026-3` is refused rather than padded, because the two spellings
    would otherwise be two batches for one month and the unique constraint on `payslips`
    would not see them as the same row.
    """
    if not isinstance(value, str):
        raise _invalid(ErrorCode.PAYSLIP_PERIOD_INVALID, "'period' must be a YYYY-MM string")
    match = PERIOD_PATTERN.match(value.strip())
    if match is None:
        raise _invalid(
            ErrorCode.PAYSLIP_PERIOD_INVALID,
            f"'period' {value!r} is not YYYY-MM",
        )
    year = int(match.group("year"))
    month = int(match.group("month"))
    if not MIN_PERIOD_YEAR <= year <= MAX_PERIOD_YEAR:
        raise _invalid(
            ErrorCode.PAYSLIP_PERIOD_INVALID,
            f"'period' year {year} is outside {MIN_PERIOD_YEAR}-{MAX_PERIOD_YEAR}",
        )
    if not 1 <= month <= 12:
        # The pattern's `[0-9]{2}` admits `13`, and the database's own CHECK admits it too
        # (`(0[1-9]|1[0-2])` is what the *column* holds). Refused here rather than left to
        # `period_bounds`, where `date(2026, 13, 1)` would raise a bare `ValueError` and
        # reach the caller as a 500 — the failure mode a format check without a range check
        # always has.
        raise _invalid(
            ErrorCode.PAYSLIP_PERIOD_INVALID,
            f"'period' month {month} is not a month",
        )
    return value.strip()


def period_bounds(period: str) -> tuple[date, date]:
    """The first and last day of the month a period names, inclusive at both ends.

    This is the one piece of arithmetic in the module, and it is not a payroll
    calculation: it turns 「2026-03」 into the range the salary archive's window predicate
    is asked about (`domain/payroll/windows.py`). No amount is touched.

    The last day is computed by walking to the first of the next month and back one day,
    which is the only form that is correct for February in a leap year without a table.
    """
    match = PERIOD_PATTERN.match(period)
    if match is None:  # pragma: no cover - `parse_period` ran first
        raise _invalid(ErrorCode.PAYSLIP_PERIOD_INVALID, f"period {period!r} is not YYYY-MM")
    year = int(match.group("year"))
    month = int(match.group("month"))
    first = date(year, month, 1)
    following = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last = date.fromordinal(following.toordinal() - 1)
    return first, last


def _invalid(code: ErrorCode, detail: str) -> DomainError:
    return DomainError(code, detail=detail)


__all__ = [
    "BATCH_ENTITY",
    "MAX_BATCH_FILES",
    "MAX_PAYSLIP_BYTES",
    "MAX_PERIOD_YEAR",
    "MIN_PERIOD_YEAR",
    "PAYSLIP_ENTITY",
    "PDF_MAGIC",
    "PERIOD_PATTERN",
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
    "PayslipStatus",
    "UnmatchedFile",
    "UnmatchedReason",
    "UploadedFile",
    "content_digest",
    "parse_period",
    "period_bounds",
]
