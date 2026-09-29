"""The salary archive: one table, effective-dated, append-only, and computing nothing.

`docs/DESIGN.md` §3.5 lists `salary_records` — 「薪酬**档案**，不做计算」 — and D9 is the
decision behind it: this system stores salary records and serves payslips, and it does
**not** compute payroll. No tax, no social security, no net pay, no proration, no
currency conversion. Not in a column, not in a helper, not in a response. Four
consequences are visible in this file, and each is a decision rather than an omission:

* **There is no derived money anywhere.** No `total`, no `annual`, no `gross`, no
  `net`. Every numeric column here holds a figure somebody entered; `len(SALARY_COLUMNS)`
  is asserted against the table's own columns in `tests/test_salary_records.py`, and the
  names are the ones a derived field would have to arrive under. A system that grows a
  `net_pay` column has started being a payroll system, and this one is not one.

* **`base_salary` is `NUMERIC(14, 2)`, and that is a correctness decision.** A cent may
  not go missing between the entry and the read-back, and `double precision` cannot
  promise that: `0.1 + 0.2` is already wrong in binary floating point, and a value with
  more than 15 significant digits is rounded on the way *in*. PostgreSQL's `numeric` is
  exact decimal arithmetic, `Mapped[Decimal]` is Python's exact decimal type, and the
  API serialises it as a **string** — so `123456789.01` is the same eleven digits in the
  request, in the row, in the response and in the assertion. The allowance amounts inside
  `components` are strings for the same reason: JSON has one number type and it is a
  float, so a JSON number is the one representation this archive must not accept.

* **The breakdown is `components`, a JSONB array of objects**, which is §3.5's
  「津贴明细」 as a structure rather than a sentence. `code`, `label` and `amount` per
  line; the array shape and the object-ness of every element are enforced by the
  database (`ck_salary_records_components_are_lines`, through the
  `salary_components_are_lines` function), so a prose blob cannot be stored there by
  anybody, this module included.

* **The rows are append-only, and the database says so.** Migration 0028 revokes
  `UPDATE` and `DELETE` from the runtime role, and the module writes no such statement
  at all: a new record never overwrites an old one, the old row keeps its dates, and the
  chain is ordered by `effective_from` so "what was in force on 2026-03-01" is one
  query. Two records for one employee may not cover the same day — `EXCLUDE USING gist
  (employee_id WITH =, daterange(effective_from, effective_to, '[]') WITH &&)` makes an
  overlap *unrepresentable*, which is a stronger statement than "the service refuses it".

**What this table deliberately does not have.** No `employee_private` column and no
personnel-change payload: ticket 17 recorded that a salary change lived in the change's
own `payload`/`applied_values` "until ticket 43 exists", and this is that ticket — the
archive is written from those agreed values when the change takes effect
(`domain/personnel/service.py::_apply_salary`), and the change keeps its own record of
what was agreed. The two are not copies of one another: the change is the *approval*,
this is the *archive*. No `file_path` either: that is `payslips`, which is ticket 44.
"""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db_metadata import Base

#: How a salary is paid out. Three values, and the shape of the payslip period follows
#: from it — but *only* the shape: nothing here multiplies a monthly figure into a
#: weekly one, because that would be proration and proration is a payroll calculation.
#: Written out in migration 0028's CHECK too, so an unknown period is refused by the
#: database rather than interpreted here.
PAY_PERIODS_SQL = "('monthly', 'biweekly', 'weekly')"

#: Why a record exists. `initial` is the first record for a person; `adjustment` is a
#: change to what is paid (a raise, a cut, a new allowance); `correction` is a fix to
#: the archive itself — a figure entered wrongly on a date it was always meant to hold.
#: Three, rather than a free-text "type": the archive is read by asking what *kind* of
#: event put a figure in force, and a vocabulary that grows by convention cannot be
#: queried.
CHANGE_REASON_TYPES_SQL = "('initial', 'adjustment', 'correction')"

#: `EUR`, `USD` — ISO 4217. The company is Spanish and euros are the ordinary case; a
#: record that means something else says so. **Never converted**, and there is no rate
#: table anywhere in this module: a converted figure is a computed one (D9).
CURRENCY_SQL = r"^[A-Z]{3}$"

#: The numeric columns this table is *allowed* to have. `tests/test_salary_records.py`
#: asserts the table's numeric and money-shaped columns are exactly these, which is how
#: "no derived money" stops being a promise about today's code and becomes a claim about
#: the schema. A `net_pay`, a `total`, an `annual_base` or an `amount_eur` arriving in a
#: later ticket fails that test by name.
SALARY_COLUMNS = ("base_salary", "components")

#: The one currency a record may leave `currency` out of. Called `DEFAULT_CURRENCY` in
#: `domain/personnel` as well; kept as a literal here so the archive does not import the
#: approval document's module to learn what a euro is.
EUROS = "EUR"

#: How much the numeric column holds. 14 digits with 2 after the point is twelve
#: integral digits — up to 999,999,999,999.99 — which is a salary in a currency that has
#: not been invented, and small enough that the column's width is a stated bound rather
#: than "however large a numeric can be". A currency with three decimal places would be
#: a widening of this column and of every amount inside `components`, which is a
#: migration somebody makes deliberately.
MONEY_PRECISION = 14
MONEY_SCALE = 2


class SalaryRecord(Base):
    """One figure, in force over one half-open date range.

    The range is `[effective_from, effective_to]` — inclusive at both ends, which is
    what the exclusion constraint's `daterange(..., '[]')` says — and `effective_to`
    is NULL while the record has no stated end. Two records for one employee may not
    overlap, so a chain of them reads as "what was in force when" without any of them
    being rewritten when the next arrives: the *new* record names its own start, and
    the previous one keeps the end it was entered with.

    **Nothing on this row is computed.** `base_salary` is what somebody entered,
    `components` is the breakdown they entered, and the effective dates are the window
    they chose. There is no fold of the two into a total, because a total is the first
    step of a payroll calculation (§8.3 lists 西班牙工资单计算 as an explicit non-goal).
    """

    __tablename__ = "salary_records"
    __table_args__ = (
        CheckConstraint(
            f"pay_period IN {PAY_PERIODS_SQL}",
            name="ck_salary_records_pay_period",
        ),
        CheckConstraint(
            f"change_reason_type IN {CHANGE_REASON_TYPES_SQL}",
            name="ck_salary_records_reason_type",
        ),
        CheckConstraint(f"currency ~ '{CURRENCY_SQL}'", name="ck_salary_records_currency"),
        # A half-open range is not a range: an end before the start would cover no day
        # while still excluding that window from every other record.
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_salary_records_effective_range",
        ),
        CheckConstraint("base_salary > 0", name="ck_salary_records_base_positive"),
        # The breakdown's shape, enforced through the function migration 0028 creates:
        # a CHECK may not contain the subquery this rule needs.
        CheckConstraint(
            "salary_components_are_lines(components)",
            name="ck_salary_records_components_are_lines",
        ),
        CheckConstraint(
            "length(btrim(change_reason)) > 0", name="ck_salary_records_change_reason"
        ),
        Index("ix_salary_records_employee_from", "employee_id", "effective_from"),
        Index("ix_salary_records_effective", "effective_from", "effective_to"),
        # One opening record per person. A second `initial` is the same hire entered
        # twice, and the entry that loses the race is the one a reviewer never sees.
        Index(
            "uq_salary_records_initial",
            "employee_id",
            unique=True,
            postgresql_where=text("change_reason_type = 'initial'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    employee_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="RESTRICT"),
        nullable=False,
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    #: NULL while this is the record in force: "no end stated" is a state, not a gap.
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    #: Exact, and the reason is in the module docstring: a float would round it.
    base_salary: Mapped[Decimal] = mapped_column(
        Numeric(precision=MONEY_PRECISION, scale=MONEY_SCALE), nullable=False
    )
    currency: Mapped[str] = mapped_column(String(length=3), nullable=False, default=EUROS)
    pay_period: Mapped[str] = mapped_column(String(length=12), nullable=False)
    #: The structured allowance breakdown. `[]` is the ordinary case.
    components: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    change_reason_type: Mapped[str] = mapped_column(String(length=12), nullable=False)
    change_reason: Mapped[str] = mapped_column(Text, nullable=False)
    #: The *account* that entered it: the archive's question is which login wrote this,
    #: and an employee with no account cannot have written one. Nullable because the
    #: applier job writes without a user (its principal names none) — and that is a fact
    #: the row states rather than one the trail has to reconstruct.
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = [
    "CHANGE_REASON_TYPES_SQL",
    "CURRENCY_SQL",
    "EUROS",
    "MONEY_PRECISION",
    "MONEY_SCALE",
    "PAY_PERIODS_SQL",
    "SALARY_COLUMNS",
    "SalaryRecord",
]
