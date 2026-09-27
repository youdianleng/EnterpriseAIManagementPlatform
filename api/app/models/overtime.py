"""Overtime: the request made before the hours, the record it becomes, its ledger.

Three tables (`docs/DESIGN.md` §3.2, §7.3, Q12), and the decisions worth reading:

* **The request and the record are two rows, not one.** An `overtime_requests` row is
  an intention — a date, the minutes somebody expects to work and why — and it exists
  while two people decide it. An `overtime_records` row is a fact: it appears when the
  engine approves, and it is what the month is summed from. One table would have to be
  both, and then "how many requests were rejected" and "how many hours did March come
  to" would be the same table with a status column that everything downstream has to
  remember to filter on.
* **`month_bucket` is derived from the business day, never from a timestamp.** The
  day is already Madrid by construction (`domain/attendance/business_day.py`), so
  `YYYY-MM` of that date *is* the Madrid month; converting an instant again is the
  confusion that module exists to prevent. It is a column rather than an expression
  because the monthly summary and the export are group-bys over it, and it is
  constrained to the shape so a hand-written row cannot put a day into a bucket no
  month matches.
* **The computed figure and HR's figure are two columns.** `computed_minutes` is
  `LEAST(approved_minutes, worked_minutes)` and the database states that equation as
  a CHECK: the smaller of the two is the rule, not a convention. HR's confirmation
  goes in `confirmed_minutes` *beside* it — with the reason and the instant — so an
  adjustment never rewrites what the day came to, which is what makes the two
  comparable afterwards. `needs_confirmation` is the queue rather than the history:
  it is set when the two figures differ by more than the configured threshold and
  cleared when HR has looked, and the ledger below keeps what it was.
* **`overtime_entries` is append-only in the database** (`REVOKE UPDATE, DELETE`,
  migration 0020) and each entry carries the record's three figures *after* the
  movement it describes. It is the "确认或调整的值必须留痕（保留原值与原因）" the ticket
  asks for: the reason a figure changed, who changed it, and what the record read
  before and after, in a table the role that serves requests cannot rewrite.

**There is no rate, no multiplier and no amount in any of these columns, and that is
the ticket's requirement rather than an omission.** This system accumulates and
exports hours; what an hour costs is finance's calculation (Q12), and a module that
held a rate would be a second payroll record — one nobody reconciles — the moment the
convenio changed. The export carries the same six facts for the same reason.

**Nothing here is retroactive.** A request's `business_date` must be today or later,
checked by the service, and there is no endpoint that writes a record for a day that
has passed: the only way an `overtime_records` row comes into existence is an approved
request. That is why the record has no `entered_by` column — the engine's decisions
carry who approved it and the request carries who asked.
"""

from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db_metadata import Base

#: The ledger's closed vocabulary. Three movements, and each is a different kind of
#: answer to "where did this figure come from": `approve` is the pre-approved minutes
#: arriving, `settle` is the day's arithmetic producing the computed figure, and
#: `confirm` is a person overruling it with a reason. Written out here and in
#: migration 0020's CHECK, so an unknown movement is refused rather than stored as a
#: row nobody can interpret.
OVERTIME_ENTRY_TYPES_SQL = "('approve', 'settle', 'confirm')"

#: How many minutes a day holds. A request for more than a day cannot be worked, and
#: the bound exists to refuse a typo (`1200` for `120`) rather than to express policy:
#: how much overtime a company allows is a conversation, not a constraint.
MAX_DAY_MINUTES = 1440

#: `YYYY-MM`, the shape of a month bucket. Mirrored by
#: `ck_overtime_records_bucket`, so a hand-written row cannot file a day under a
#: bucket that is not a month.
MONTH_BUCKET_SQL = r"^[0-9]{4}-(0[1-9]|1[0-2])$"


class OvertimeRequest(Base):
    """A request to work overtime on a date, before the date.

    The document: which day, how many minutes are expected, why, and where the
    approval engine got to. Its *state* is derived from the row and the engine
    (`domain/overtime/models.state_of_request`), never stored, for the reason
    `attendance/corrections.py` gives — a copied status is the one that goes stale.

    `reason` is free text and it is the ticket's own field (事由): unlike a leave,
    which this system records by type and dates because of the AEPD position §8
    states, overtime has no special category to leak — "cierre de inventario" is
    what the field is for.
    """

    __tablename__ = "overtime_requests"
    __table_args__ = (
        CheckConstraint(
            f"expected_minutes BETWEEN 1 AND {MAX_DAY_MINUTES}",
            name="ck_overtime_requests_minutes",
        ),
        CheckConstraint("length(btrim(reason)) > 0", name="ck_overtime_requests_reason"),
        Index("ix_overtime_requests_employee_date", "employee_id", "business_date"),
        # One live intention per person per day. Partial, because a request that was
        # rejected or withdrawn must not block the day for ever: `settled_at` is what
        # closes a document, and the index holds only the open ones. The rule it
        # enforces is "overtime is counted once per day", at the layer a race cannot
        # pass.
        Index(
            "uq_overtime_requests_open_day",
            "employee_id",
            "business_date",
            unique=True,
            postgresql_where=text("settled_at IS NULL"),
        ),
        # The resolve sweep's queue, the shape `leave_requests` uses: filed, and the
        # record not yet written.
        Index(
            "ix_overtime_requests_unsettled",
            "submitted_at",
            postgresql_where=text("approval_request_id IS NOT NULL AND settled_at IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    employee_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="RESTRICT"),
        nullable=False,
    )
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    #: What the person expects to work. Copied onto the record at approval, so an
    #: edit to a request returned for correction cannot change what a settled record
    #: was measured against.
    expected_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    #: The engine's request. Null until the document is filed, which is also what
    #: makes it a draft.
    approval_request_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Stamped when the engine's approval was resolved into a record.
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    withdrawn_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: The document is finished with: a record was written, or the engine refused it
    #: and nothing will be. Its own fact, because the engine's status does not say
    #: whether *this module* has written the record yet.
    settled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<OvertimeRequest {self.employee_id} {self.business_date}>"


class OvertimeRecord(Base):
    """Approved overtime for one person's one day, and what it came to.

    The three figures are deliberately three columns and not one:

    * `approved_minutes` is what two people agreed to in advance;
    * `computed_minutes` is `LEAST(approved_minutes, worked_minutes)` — the ticket's
      "取较小值" — written when the day was settled, and never rewritten;
    * `confirmed_minutes` is HR's, stored beside them with the reason it changed.

    The row is what the monthly summary sums and the export lists. `month_bucket` is
    the group-by, `needs_confirmation` is HR's queue.
    """

    __tablename__ = "overtime_records"
    __table_args__ = (
        # One record per request: the resolve sweep's idempotency, as a constraint. A
        # second pass over an approved document collides here rather than writing the
        # day twice.
        UniqueConstraint("request_id", name="uq_overtime_records_request"),
        CheckConstraint(f"month_bucket ~ '{MONTH_BUCKET_SQL}'", name="ck_overtime_records_bucket"),
        CheckConstraint(
            f"approved_minutes BETWEEN 1 AND {MAX_DAY_MINUTES}",
            name="ck_overtime_records_approved",
        ),
        CheckConstraint(
            f"worked_minutes IS NULL OR worked_minutes BETWEEN 0 AND {MAX_DAY_MINUTES}",
            name="ck_overtime_records_worked",
        ),
        CheckConstraint(
            f"computed_minutes IS NULL OR computed_minutes BETWEEN 0 AND {MAX_DAY_MINUTES}",
            name="ck_overtime_records_computed",
        ),
        # The computed figure and its stamp are one fact with two witnesses.
        CheckConstraint(
            "(settled_at IS NULL) = (computed_minutes IS NULL)",
            name="ck_overtime_records_settled",
        ),
        # The ticket's settlement rule, as an equation: the smaller of the two. A
        # write that chose the approved figure while the worked one was smaller — the
        # mistake this rule exists to prevent — is refused by the database, not only by
        # the service that computes it.
        CheckConstraint(
            "computed_minutes IS NULL OR (worked_minutes IS NOT NULL "
            "AND computed_minutes = LEAST(approved_minutes, worked_minutes))",
            name="ck_overtime_records_smaller",
        ),
        CheckConstraint(
            f"confirmed_minutes IS NULL OR confirmed_minutes BETWEEN 0 AND {MAX_DAY_MINUTES}",
            name="ck_overtime_records_confirmed_minutes",
        ),
        # HR's figure, when it was written and why are one act: a confirmation with no
        # reason is a figure nobody can explain, and a reason with no figure is a note.
        CheckConstraint(
            "(confirmed_minutes IS NULL) = (confirmed_at IS NULL)",
            name="ck_overtime_records_confirmation",
        ),
        CheckConstraint(
            "confirmed_at IS NULL OR length(btrim(confirmation_note)) > 0",
            name="ck_overtime_records_confirmation_note",
        ),
        # The monthly summary's and the export's query: one bucket, in employee and
        # date order.
        Index("ix_overtime_records_month", "month_bucket", "employee_id", "business_date"),
        Index("ix_overtime_records_employee_date", "employee_id", "business_date"),
        # The settle sweep's queue: records whose day has not been computed yet.
        Index(
            "ix_overtime_records_unsettled",
            "business_date",
            postgresql_where=text("settled_at IS NULL"),
        ),
        # HR's queue: what is waiting for a person to look at it.
        Index(
            "ix_overtime_records_needs_confirmation",
            "month_bucket",
            postgresql_where=text("needs_confirmation"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    #: The request this record is the outcome of. One approval, one record, and the
    #: reason it exists — which is why there is no second `reason` column here.
    request_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("overtime_requests.id", ondelete="RESTRICT"),
        nullable=False,
    )
    employee_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="RESTRICT"),
        nullable=False,
    )
    #: The Madrid business day the overtime counts against, copied from the request.
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    #: `YYYY-MM` of `business_date`, and the whole of the monthly grouping.
    month_bucket: Mapped[str] = mapped_column(String(7), nullable=False)
    approved_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The day's actual worked minutes, read from the attendance module when the
    #: record was settled. Null until then: the day may not have happened yet.
    worked_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: `LEAST(approved_minutes, worked_minutes)`. Null while the day is open.
    computed_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: HR's queue: the two figures differ by more than the configured threshold and
    #: nobody has looked yet. The history of the flag is in the ledger.
    needs_confirmation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: What HR confirmed, or adjusted the record to. Never written over
    #: `computed_minutes`, which is the point of the pair.
    confirmed_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confirmed_by_employee_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Why the figure was confirmed or changed. About hours, never about money: this
    #: system does not hold a rate.
    confirmation_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<OvertimeRecord {self.employee_id} {self.business_date} "
            f"approved={self.approved_minutes} computed={self.computed_minutes}>"
        )


class OvertimeEntry(Base):
    """One movement of a record, with the figures that followed it.

    Append-only in the database, like the expected-hours snapshot and the leave
    ledger: "what was this figure before HR changed it" is evidence, and evidence the
    role that serves requests can edit is not. `seq` is the reading order, because
    `created_at` is the transaction's start time and every entry one settlement
    writes shares it.
    """

    __tablename__ = "overtime_entries"
    __table_args__ = (
        CheckConstraint(
            f"entry_type IN {OVERTIME_ENTRY_TYPES_SQL}", name="ck_overtime_entries_type"
        ),
        CheckConstraint(
            f"approved_minutes BETWEEN 1 AND {MAX_DAY_MINUTES}",
            name="ck_overtime_entries_approved",
        ),
        CheckConstraint(
            f"computed_minutes IS NULL OR computed_minutes BETWEEN 0 AND {MAX_DAY_MINUTES}",
            name="ck_overtime_entries_computed",
        ),
        CheckConstraint(
            f"confirmed_minutes IS NULL OR confirmed_minutes BETWEEN 0 AND {MAX_DAY_MINUTES}",
            name="ck_overtime_entries_confirmed",
        ),
        UniqueConstraint("seq", name="uq_overtime_entries_seq"),
        Index("ix_overtime_entries_record", "record_id", "seq"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    #: Monotonic, assigned by the database. The ledger's reading order.
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), nullable=False)
    record_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("overtime_records.id", ondelete="RESTRICT"),
        nullable=False,
    )
    entry_type: Mapped[str] = mapped_column(String(16), nullable=False)
    #: The record's three figures *after* this movement, so the row reads on its own.
    approved_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    computed_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confirmed_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Why HR changed the figure, on the entry that changed it. The record's own
    #: `confirmation_note` is the same sentence; this one cannot be edited away.
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_employee_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<OvertimeEntry {self.entry_type} record={self.record_id}>"


__all__ = [
    "MAX_DAY_MINUTES",
    "MONTH_BUCKET_SQL",
    "OVERTIME_ENTRY_TYPES_SQL",
    "OvertimeEntry",
    "OvertimeRecord",
    "OvertimeRequest",
]
