"""Leave types, the year's balance, its ledger, and the request that spends it.

Four tables (`docs/DESIGN.md` §3.2, D7, Q24), and the decisions worth reading:

* **The types are data, not an enum.** 病假/事假/产假 differ by installation and by
  convenio, so `leave_types` carries the four flags that decide what a type *means*
  to the rest of the system: whether it is paid, whether it needs an attachment,
  whether it spends the annual allowance, and whether it may still be filed. The
  migration seeds a starter set so the system is usable without anybody writing
  SQL, and HR maintains it afterwards.
* **The allowance is a parameter, and the balance row is where it lands.**
  `settings.annual_leave_days` (30 by default, D7) is materialised into
  `leave_balances.entitled_days` the first time an employee's year is needed, so a
  balance reports the figure it was actually granted rather than whatever the
  parameter says today — the same reasoning as a stored expected-hours snapshot.
  `carried_over_days` is the other half: days brought from the previous year, and
  only ever written by somebody, never derived.
* **The remainder is derived, and the constraint is the guarantee.**
  `remaining = entitled + carried_over - used - pending`; `used_days` and
  `pending_days` are stored because "how many days has this person taken" is asked
  on its own, and the CHECK below is what makes "no request may exceed the
  allowance" a property of the database rather than of one service method. A race
  between two submissions cannot pass a check the database performs on the row it
  is updating.
* **`pending` is the reservation.** A filed request holds its days as pending for
  as long as it is in the approval queue; approval moves them to `used`, and a
  rejection or a withdrawal releases them. The movement is a ledger row, so the
  balance's history is what happened rather than a reconstruction of it.
* **`leave_balance_entries` is append-only in the database** (`REVOKE UPDATE,
  DELETE`, migration 0019) and every entry carries the four totals *after* it. That
  is what "看到额度计算的历史记录" asks for: each row states what moved and what the
  balance then was, so a reader four years from now reproduces the arithmetic
  instead of trusting a total nothing can explain.

**What `leave_requests` deliberately does not have.** No `reason`, no `note`, no
free-text column at all — the design's §3.2 row lists one, and it is dropped here
on purpose: a free-text field on a sick leave is an invitation to write a
diagnosis into the database, and §8 (AEPD, special-category data) says this system
records the *type* and the *dates* and nothing else. The one text column is
`attachment_reference`, which is a storage key and is constrained to look like one
(`ck_leave_requests_attachment_reference`): a sentence cannot be stored in it. The
file itself does not live here — ticket 31's document store is where the bytes go,
and until it lands the reference is an opaque string no endpoint of this module
reads back for anybody but HR.

**No half days.** The ticket's granularity is a working day and every column is an
integer, because a balance that can hold 2.5 days invites a half-day no schedule in
this system can express. When half days arrive it is a widening of these columns,
not a rewrite: the ledger already records each movement.
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
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db_metadata import Base

#: The ledger's closed vocabulary. `grant` and `carry_over` are the two an HR
#: member writes; `reserve`, `release`, `consume` and `refund` are the four a
#: request's life produces; `adjustment` is the correction of a figure somebody
#: typed wrongly. Written out here and in the migration's CHECK, so an unknown
#: movement is refused rather than silently stored as a number nobody can interpret.
BALANCE_ENTRY_TYPES_SQL = (
    "('grant', 'carry_over', 'adjustment', 'reserve', 'release', 'consume', 'refund')"
)

#: A storage key, not prose: letters, digits, dot, dash, slash and underscore. The
#: constraint exists so that a diagnosis cannot be written into the one text column
#: this module has — the failure it prevents is somebody typing a medical note into
#: the field the form left open, which is exactly what §8 forbids. Mirrors
#: `domain/leave/models.ATTACHMENT_REFERENCE_PATTERN`, which refuses the same shape
#: as a catalogued 422 before the database has to.
ATTACHMENT_REFERENCE_SQL = r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$"

#: How many days a balance of one type in one year may hold. A hundred years of a
#: very generous allowance: the bound exists to refuse a typo (`3000` days), not to
#: express policy.
MAX_BALANCE_DAYS = 366


class LeaveType(Base):
    """One kind of leave, with the four flags that decide what it means.

    `counts_against_annual` is the one that reaches the balance: a type that carries
    it gets a `leave_balances` row and its requests are refused when the year's
    allowance is spent. A type without it — a sick note, a birth — is recorded and
    approved the same way and spends nothing, because an installation cannot refuse
    somebody their statutory leave for want of an allowance.
    """

    __tablename__ = "leave_types"
    __table_args__ = (
        UniqueConstraint("code", name="uq_leave_types_code"),
        CheckConstraint("length(btrim(code)) > 0", name="ck_leave_types_code"),
        CheckConstraint(
            "length(btrim(name_es)) > 0 AND length(btrim(name_en)) > 0",
            name="ck_leave_types_names",
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    #: What a request, a report and an import name the type by. Unique for good:
    #: a balance refers to the type, and a renamed code would orphan the history.
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name_es: Mapped[str] = mapped_column(String(160), nullable=False)
    name_en: Mapped[str] = mapped_column(String(160), nullable=False)
    is_paid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: Whether a request of this type must carry an attachment reference. The
    #: service refuses one that does not; the file itself is stored elsewhere.
    requires_attachment: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    counts_against_annual: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Deactivating removes a type from the catalogue of what may be *filed*. It
    #: does not touch a request already filed under it, and it does not delete the
    #: balances: a year somebody was granted is history.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<LeaveType {self.code} annual={self.counts_against_annual}>"


class LeaveBalance(Base):
    """One employee, one year, one type: what was granted and what is left of it.

    Keyed by the three, so "Ana's 2026 annual leave" is a row rather than a query
    over a ledger. The four figures are the fast answer and the ledger below is the
    history; the check constraint is what stops the two from ever disagreeing about
    what is spendable.
    """

    __tablename__ = "leave_balances"
    __table_args__ = (
        UniqueConstraint(
            "employee_id",
            "year",
            "leave_type_id",
            name="uq_leave_balances_employee_year_type",
        ),
        CheckConstraint("year BETWEEN 2000 AND 2200", name="ck_leave_balances_year"),
        CheckConstraint(
            f"entitled_days BETWEEN 0 AND {MAX_BALANCE_DAYS}",
            name="ck_leave_balances_entitled",
        ),
        CheckConstraint(
            f"carried_over_days BETWEEN 0 AND {MAX_BALANCE_DAYS}",
            name="ck_leave_balances_carried",
        ),
        CheckConstraint("used_days >= 0", name="ck_leave_balances_used"),
        CheckConstraint("pending_days >= 0", name="ck_leave_balances_pending"),
        # The allowance rule, at the only layer that cannot be raced: a request may
        # never leave the year holding more spent and reserved days than it granted.
        CheckConstraint(
            "used_days + pending_days <= entitled_days + carried_over_days",
            name="ck_leave_balances_within_allowance",
        ),
        Index("ix_leave_balances_employee_year", "employee_id", "year"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    employee_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="RESTRICT"),
        nullable=False,
    )
    year: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    leave_type_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("leave_types.id", ondelete="RESTRICT"),
        nullable=False,
    )
    #: Materialised from `settings.annual_leave_days` when the row is created, and
    #: adjustable per person by HR afterwards.
    entitled_days: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Days brought from the previous year. Never derived: carrying days over is a
    #: decision, and a system that invented one would grant an allowance nobody did.
    carried_over_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Days of requests the engine has approved.
    used_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Days of requests that are in the approval queue.
    pending_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    @property
    def remaining_days(self) -> int:
        """What may still be requested. The one figure a refusal has to state."""
        return (
            self.entitled_days + self.carried_over_days - self.used_days - self.pending_days
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<LeaveBalance {self.employee_id} {self.year} type={self.leave_type_id} "
            f"remaining={self.remaining_days}>"
        )


class LeaveBalanceEntry(Base):
    """One movement of a balance, with the totals that followed it.

    Append-only in the database, like the expected-hours snapshot: the history of
    how a figure was reached is evidence, and evidence that can be edited is not.
    `days` is the size of the movement and the four columns are the balance *after*
    it, so the row reads on its own — "reserved 3, leaving 27 entitled and 3
    pending" — and a reconciliation is a comparison rather than a re-derivation.
    """

    __tablename__ = "leave_balance_entries"
    __table_args__ = (
        CheckConstraint(
            f"entry_type IN {BALANCE_ENTRY_TYPES_SQL}", name="ck_leave_balance_entries_type"
        ),
        CheckConstraint("days <> 0", name="ck_leave_balance_entries_days"),
        CheckConstraint("entitled_days >= 0", name="ck_leave_balance_entries_entitled"),
        CheckConstraint("carried_over_days >= 0", name="ck_leave_balance_entries_carried"),
        CheckConstraint("used_days >= 0", name="ck_leave_balance_entries_used"),
        CheckConstraint("pending_days >= 0", name="ck_leave_balance_entries_pending"),
        # The order the movements happened in, which `created_at` cannot express: it is
        # the transaction's start time, so every entry one request writes shares it and
        # a reader ordering by it would get the ledger back shuffled. The same reason
        # the audit trail numbers its rows.
        UniqueConstraint("seq", name="uq_leave_balance_entries_seq"),
        Index("ix_leave_balance_entries_balance", "balance_id", "seq"),
        Index("ix_leave_balance_entries_request", "leave_request_id"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    #: Monotonic, assigned by the database. The ledger's reading order.
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), nullable=False)
    balance_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("leave_balances.id", ondelete="RESTRICT"),
        nullable=False,
    )
    entry_type: Mapped[str] = mapped_column(String(16), nullable=False)
    #: The size of the movement, always positive: the type says which way it went,
    #: and a signed number would be a second way to say the same thing.
    days: Mapped[int] = mapped_column(Integer, nullable=False)
    entitled_days: Mapped[int] = mapped_column(Integer, nullable=False)
    carried_over_days: Mapped[int] = mapped_column(Integer, nullable=False)
    used_days: Mapped[int] = mapped_column(Integer, nullable=False)
    pending_days: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The request that caused it, for the three movements a request produces. Null
    #: for a grant or a carry-over, which nobody's request caused.
    leave_request_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    #: Why an HR member changed a figure. The one place prose belongs in this
    #: module: it is about the *allowance*, not about anybody's health.
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_employee_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<LeaveBalanceEntry {self.entry_type} {self.days}d balance={self.balance_id}>"


class LeaveRequest(Base):
    """A request for leave: which type, which dates, and how many working days.

    **No `reason` column**, and that is the ticket's §8 requirement rather than an
    omission — see the module docstring. What the row records is the type, the two
    dates, the working days they are worth, and (when the type asks for one) a
    reference to a separately stored file that only HR may read.

    **No `status` column either.** The state a reader sees is derived from four
    facts that are all on this row or in the engine — whether it was filed, what the
    engine said, whether the balance was settled, and whether the requester withdrew
    it — and a copied status would be a second answer that goes stale.
    """

    __tablename__ = "leave_requests"
    __table_args__ = (
        CheckConstraint("end_date >= start_date", name="ck_leave_requests_window"),
        CheckConstraint("business_days_count > 0", name="ck_leave_requests_days"),
        # Exactly one of "no attachment" and "an opaque key" is expressible, and the
        # reference is the only text this table holds.
        CheckConstraint(
            f"attachment_reference IS NULL OR attachment_reference ~ '{ATTACHMENT_REFERENCE_SQL}'",
            name="ck_leave_requests_attachment_reference",
        ),
        Index("ix_leave_requests_employee_start", "employee_id", "start_date"),
        # The calendar read and the anomaly scan both ask "who is on leave on this
        # date", which is this index's question.
        Index("ix_leave_requests_covering", "start_date", "end_date"),
        # What the settle sweep looks for: filed, and the balance not yet resolved.
        Index(
            "ix_leave_requests_unsettled",
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
    leave_type_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("leave_types.id", ondelete="RESTRICT"),
        nullable=False,
    )
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    #: The working days the range is worth, counted from the schedule and the
    #: holiday calendar when the request was drafted. Stored rather than recomputed:
    #: editing a schedule next year must not change what March's leave was worth.
    business_days_count: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The engine's request. Null until the document is filed.
    approval_request_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    #: A key into a separately stored file — never the file, never its content.
    #: Ticket 31's document store holds the bytes; until it exists this is opaque
    #: to every reader but HR, and nothing in this module dereferences it.
    attachment_reference: Mapped[str | None] = mapped_column(String(200), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Stamped when the engine's approval was settled against the balance. It is
    #: also what the attendance scan reads: an approved, not-withdrawn request
    #: covering a date is a day of leave.
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: The requester's own act, before the leave starts. Set for both the engine's
    #: withdrawal of a pending request and this module's cancellation of an approved
    #: one, because to the person doing it they are one act.
    withdrawn_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: The reservation is resolved: the days were consumed or released. Its own
    #: fact, because the engine's status does not say whether *this module* has
    #: finished with the balance, and a settle that ran twice would move days twice.
    settled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def covers(self, on_date: date) -> bool:
        """Whether a leave covering this date includes it. Inclusive at both ends.

        The 28th to the 5th includes both, which is how somebody asks for it and
        how the deduction counts it.
        """
        return self.start_date <= on_date <= self.end_date

    @property
    def is_approved(self) -> bool:
        """In force: the engine approved it and nobody has withdrawn it."""
        return self.approved_at is not None and self.withdrawn_at is None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<LeaveRequest {self.employee_id} {self.start_date}..{self.end_date}>"


__all__ = [
    "ATTACHMENT_REFERENCE_SQL",
    "BALANCE_ENTRY_TYPES_SQL",
    "MAX_BALANCE_DAYS",
    "LeaveBalance",
    "LeaveBalanceEntry",
    "LeaveRequest",
    "LeaveType",
]
