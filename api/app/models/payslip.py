"""The payslip tables: a month of files, and the batch that filed them.

`docs/DESIGN.md` §3.5 lists `payslips` — 「财务按月份批量上传」 — and `payslip_batches`
— 「上传清单 + 缺失清单」 — and this file is those two tables as the application sees
them. Four decisions are visible here, and each is the reason a particular column or
constraint exists rather than a description of what the columns hold:

* **A payslip is not a document, and this table is the whole of that decision.** The
  obvious alternative — reuse `documents` with ticket 36's `is_company_kb = false` —
  does not work, and the reason is a line of SQL rather than a preference:
  `repositories/retrieval.py::visible_document_clauses` clause 1 is
  `NOT d.is_company_kb AND d.owner_employee_id = :filter_employee_id`, so a personal
  document's own chunks *are* retrievable by its owner. A payslip's text must be
  retrievable by **nobody**, including the person it belongs to, and there is no flag on
  `documents` that expresses "not even its owner" — `is_company_kb` moves a document to
  a *wider* rule, never to a narrower one. So the file lives under the payroll module's
  own storage root, the row records the key, and nothing in the retrieval path can reach
  it because that path reads `document_chunks` and this is not one.

* **`checksum_sha256` and `file_size` are the ticket's 「事后核对」.** They are what makes
  a replacement detectable *afterwards*: a stored file whose bytes no longer hash to the
  row's checksum changed outside this module, and the row's checksum moving between two
  reads of one `(employee, period)` is a replacement that happened. `UNIQUE (employee_id,
  period)` is why there is one row to compare rather than a list, and a re-upload is an
  upsert of it — the checksum and the size move, the identity does not.

* **`original_filename` is the *normalised* name, and the row also keeps the raw one's
  reason.** The name is what the file is handed back as (ticket 45) and what a payroll
  reader recognises, so it is stored; it is normalised through `document.files.
  safe_filename` because a name travels into a `Content-Disposition` header and a log
  line, and the raw bytes that matched an employee number are gone by then anyway —
  matching reads the name, it does not store it.

* **`payslip_batches.unmatched` is a JSONB array of `{filename, reason}` and
  `missing_employee_ids` is an array of uuid strings.** Both are lists of *facts the
  uploader was shown*: which files matched nobody (and why), and who had no payslip that
  month. The live missing list is derived on every read — it moves when a salary record
  or a termination moves — and these two columns are the point-in-time record of what the
  upload answered, which is the question an incident review asks afterwards and a live
  query cannot answer at all.

**What this module deliberately does not have.** No amount, no currency and no total: the
file is stored and handed back, and D9 makes 西班牙工资单计算 a non-goal — a `net_pay`
column here would be the first step of one. No `filename`-derived employee number either:
`employee_id` is the answer to the matching question and the answer is all that is kept.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db_metadata import Base

#: The two statuses a payslip can be in. Spelled out as a SQL literal for migration
#: 0029's CHECK, so an unknown status is refused by the database rather than interpreted
#: here. `published` is what this ticket writes; `withdrawn` is what ticket 46 will, and
#: the database's withdrawal CHECK is already written so that it cannot be added without
#: a reason.
PAYSLIP_STATUSES_SQL = "('published', 'withdrawn')"

#: `YYYY-MM`, the month a payslip is for. A string rather than a date because the thing
#: being named is a period, not a day: "the March 2026 payslip" covers 31 days, and a
#: date would have to pick one of them.
PERIOD_SQL = r"^[0-9]{4}-(0[1-9]|1[0-2])$"

#: The length a sha256 hex digest has. Written as a CHECK because a checksum of the
#: wrong length is not a checksum, and the column is the one an after-the-fact
#: replacement check reads.
SHA256_SQL = r"^[0-9a-f]{64}$"

#: The numeric columns this table is *allowed* to have. `tests/test_payslips.py` asserts
#: the table's columns are exactly the ones this module names, which is how "no computed
#: money anywhere" stops being a promise about today's code and becomes a claim about the
#: schema: a `net_pay`, a `gross` or an `amount_eur` arriving in a later ticket fails that
#: test by name.
PAYSLIP_COLUMNS = (
    "id",
    "employee_id",
    "period",
    "storage_path",
    "file_size",
    "content_sha256",
    "original_filename",
    "uploaded_by_user_id",
    "batch_id",
    "status",
    "withdrawn_at",
    "withdraw_reason",
    "download_count",
    "created_at",
)

#: The columns the database refuses to see changed (migration 0029's trigger). They are the
#: row's **identity** — whose payslip it is, which month it is for, when the slot was opened
#: and which login opened it — rather than the file's own columns, and the difference is
#: the ticket's replacement rule: a re-upload is `INSERT … ON CONFLICT (employee_id, period)
#: DO UPDATE` and therefore *does* move `storage_path`, `content_sha256` and `file_size`. A
#: trigger that froze those as well would refuse the replacement the ticket asks for, which
#: is how the list came to be this one.
IMMUTABLE_COLUMNS = (
    "id",
    "employee_id",
    "period",
    "created_at",
    "uploaded_by_user_id",
)

#: The columns migration 0029's trigger protects. Kept beside `IMMUTABLE_COLUMNS` so a
#: reader can see that the two lists are one list, and so the migration's literal and this
#: module's cannot drift without a test noticing.
IMMUTABILITY_FUNCTION = "payslips_immutable_columns"


class PayslipBatch(Base):
    """One month's upload: what it contained, and who was missing a payslip.

    The counts are the batch's own answer rather than a query over `payslips`: a batch is
    a *moment* — finance uploaded eleven files, nine were attributed and two matched
    nobody — and asking the table "how many did this batch attribute" afterwards would
    answer with today's rows, which a re-upload of the same month has already changed.

    `missing_employee_ids` is that moment's version of the missing list. The list a
    screen shows is derived live (`domain/payslip/service.py`), because a salary record
    entered or a termination applied after the upload moves it; the column is what the
    uploader was told at the time.
    """

    __tablename__ = "payslip_batches"
    __table_args__ = (
        CheckConstraint(f"period ~ '{PERIOD_SQL}'", name="ck_payslip_batches_period"),
        CheckConstraint("total_count >= 0", name="ck_payslip_batches_total"),
        CheckConstraint("success_count >= 0", name="ck_payslip_batches_success"),
        # A batch cannot attribute more files than it received: the two counts are the
        # same upload counted two ways, and a row where they disagree is a row whose
        # reader has to guess which one to believe.
        CheckConstraint(
            "success_count <= total_count", name="ck_payslip_batches_success_within_total"
        ),
        CheckConstraint(
            "payslip_entries_are_objects(missing_employee_ids)",
            name="ck_payslip_batches_missing_are_uuids",
        ),
        CheckConstraint(
            "payslip_unmatched_are_entries(unmatched)",
            name="ck_payslip_batches_unmatched_are_entries",
        ),
        Index("ix_payslip_batches_period", "period", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    #: The month the batch is for. `YYYY-MM`, and the CHECK is what makes "2026-3" and
    #: "2026-03" impossible rather than merely discouraged.
    period: Mapped[str] = mapped_column(String(length=7), nullable=False)
    #: The *account* that uploaded it — the same question the salary archive asks of
    #: `created_by_user_id`, and the same answer: which login filed this month.
    uploaded_by_user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    total_count: Mapped[int] = mapped_column(Integer, nullable=False)
    success_count: Mapped[int] = mapped_column(Integer, nullable=False)
    missing_employee_ids: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    #: `[{filename, reason}]`, one entry per file that was not attributed. The reasons are
    #: the module's own vocabulary (`domain/payslip/models.py::UnmatchedReason`), stored as
    #: their string values so a reader can filter on them.
    unmatched: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Payslip(Base):
    """One employee's payslip for one month.

    `(employee_id, period)` is unique, so a re-upload is an upsert of this row rather than
    a second row: the file the employee will be handed is the only one there is, and
    "which payslip is March's" is a single answer. `content_sha256` and `file_size` move
    with it, which is what makes the replacement checkable afterwards.

    **Nothing on this row is computed.** `file_size` is the byte count of the file that
    was stored, `content_sha256` is its hash, and there is no amount, no total and no
    conversion anywhere (D9). The one thing a payroll system would derive from a payslip —
    its contents — is deliberately never read: the file is stored and handed back.
    """

    __tablename__ = "payslips"
    __table_args__ = (
        CheckConstraint(f"period ~ '{PERIOD_SQL}'", name="ck_payslips_period"),
        CheckConstraint(
            f"status IN {PAYSLIP_STATUSES_SQL}", name="ck_payslips_status"
        ),
        CheckConstraint("file_size > 0", name="ck_payslips_file_size"),
        CheckConstraint(f"content_sha256 ~ '{SHA256_SQL}'", name="ck_payslips_checksum"),
        CheckConstraint("download_count >= 0", name="ck_payslips_download_count"),
        # See the migration: a withdrawn payslip states when and why, and a published one
        # states neither. Written now so ticket 46's first withdrawal has to satisfy it.
        CheckConstraint(
            "(status = 'published' AND withdrawn_at IS NULL AND withdraw_reason IS NULL) "
            "OR (status = 'withdrawn' AND withdrawn_at IS NOT NULL "
            "AND length(btrim(withdraw_reason)) > 0)",
            name="ck_payslips_withdrawal_is_stated",
        ),
        # The replacement rule, as a database fact. A service that forgot to look would
        # still be unable to store two payslips for one month.
        UniqueConstraint("employee_id", "period", name="uq_payslips_employee_period"),
        Index("ix_payslips_period", "period", "employee_id"),
        Index("ix_payslips_period_status", "period", "status"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    employee_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("employees.id", ondelete="RESTRICT"), nullable=False
    )
    period: Mapped[str] = mapped_column(String(length=7), nullable=False)
    #: The key under the payroll storage root — `<sha256[0:2]>/<sha256>.pdf` — never an
    #: absolute path, so moving the root between environments changes no row.
    storage_path: Mapped[str] = mapped_column(String(length=200), nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The content hash. What it is *for* is the ticket's 「事后核对是否被替换」: compare it
    #: to the stored bytes and the row is either the file it claims or one that changed.
    content_sha256: Mapped[str] = mapped_column(String(length=64), nullable=False)
    #: The normalised filename (`document.files.safe_filename`), which is what the file is
    #: handed back as and what a payroll reader recognises.
    original_filename: Mapped[str] = mapped_column(String(length=200), nullable=False)
    uploaded_by_user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    batch_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("payslip_batches.id", ondelete="RESTRICT"),
        nullable=False,
    )
    #: `published` or `withdrawn`. Only `published` is visible to the employee, and the
    #: row-level policy is where that is true rather than a query somebody remembers.
    status: Mapped[str] = mapped_column(String(length=16), nullable=False)
    withdrawn_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    withdraw_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Ticket 45's counter: how many times the file was handed over. Present from the
    #: start because the row is the only place it can live, and a column added by the
    #: ticket that first writes it would be a migration for an integer.
    download_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = [
    "IMMUTABILITY_FUNCTION",
    "IMMUTABLE_COLUMNS",
    "PAYSLIP_COLUMNS",
    "PAYSLIP_STATUSES_SQL",
    "PERIOD_SQL",
    "SHA256_SQL",
    "Payslip",
    "PayslipBatch",
]
