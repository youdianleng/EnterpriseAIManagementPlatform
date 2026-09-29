"""The salary archive as the rest of the system sees it.

Three things in this file carry the ticket's weight, and each is a shape rather than a
rule somebody has to remember:

1. **`SalaryReading` has no public constructor.** Salary data reaches a route only
   inside one of these, and one can only be obtained from `SalaryService.read(...)` —
   the one place that serves this table and the one place that writes the audit entry.
   A list an employee fetches, a record HR pulls up, a row a manager is refused and a
   `404`-shaped question about somebody with no records are all reads, all go through
   that method, and none of them can be answered without an entry appearing in
   `audit_log`. The type is what makes "a future caller forgets to call `record()`"
   unrepresentable rather than merely unlikely: a route that never called `read()` has
   no rows to serve, because rows only travel inside a reading.

   It is `filter_for`'s argument applied to a different question. There, the private
   constructor stops a caller inventing a filter that allows everything; here, it stops
   a caller serving a figure that was never recorded as read.

2. **`SalaryRecord.amount` is a `Decimal`, and `stored_amount` is a string.** Both are
   the no-cent-lost decision: the column is `numeric(14, 2)`, Python's `Decimal` is
   exact decimal arithmetic, and the wire form is a string because JSON's one number
   type is a float. `tests/test_salary_records.py` round-trips `123456789.01` through
   HTTP and asserts the digits, which is a test a float could not pass.

3. **`SalaryRecordView` holds no money and no arithmetic.** The view a route returns is
   the row, the subject, who entered it and when — and *not* a fold of the base and the
   allowances into a total, which would be the first step of the payroll calculation
   D9 excludes. The one field that looks derived, `components_total`, deliberately does
   not exist. `as_of` is the archive's own question ("which record was in force") and is
   answered by a range predicate, never by comparing amounts.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any
from uuid import UUID

from app.domain.errors import DomainError
from app.domain.payroll.errors import PayrollErrorCode

#: The entity type the audit trail files these reads under. One string, written once,
#: so `audit_log` can be filtered to the archive's accesses with an equality rather than
#: a list of spellings.
SALARY_ENTITY = "salary_record"

#: The catalogue key the archive's disclaimer is served under (ticket 43's last
#: checklist line, 界面上明确提示该模块为档案记录，实际发放以财务出具的工资单为准). The API
#: returns the *key*; the catalogue carries the three languages; a client renders
#: whichever its locale wants. **The sentence is not stored on the row and not composed
#: by the API** — a disclaimer that lives in a column is one a client can be handed
#: stale, and one the API spelled out would be a sentence in one language served to
#: every reader.
#:
#: Not a second copy of ticket 36's `answer.source_notice.personal_document` pattern,
#: which stores the notice with the answer: an answer is a permanent record of what a
#: reader was told, while this is a fact about a *surface*. It is the refusal's shape
#: instead — a key in the catalogue, resolved per request — with the three texts
#: travelling beside it so a client that holds no dictionary still renders the sentence
#: rather than a key.
ARCHIVE_NOTICE_KEY = "salary.archive_notice"

#: The disclaimer in the languages the interface ships (§10.4). **The Chinese is the
#: checklist's own sentence, kept verbatim** — 界面上明确提示该模块为档案记录，实际发放以财务
#: 出具的工资单为准 — and the Spanish and English are the same statement, because a reader of
#: a Spanish screen still has to be told that this screen is an archive. The API serves
#: this object beside every record for the reason D21's notice is served with an answer:
#: a disclaimer a client composes is a disclaimer that drifts.
ARCHIVE_NOTICE_TEXT: dict[str, str] = {
    "zh": "本模块为薪酬档案记录，实际发放以财务出具的工资单为准。",
    "es": (
        "Este módulo es el archivo de retribuciones; el importe realmente abonado es el "
        "que figura en la nómina emitida por Finanzas."
    ),
    "en": (
        "This module is the salary archive; the amount actually paid is the one on the "
        "payslip issued by Finance."
    ),
}

#: The two decimals a euro has. `quantize` refuses anything finer rather than rounding
#: it, because rounding a salary on the way in is how a cent goes missing.
MONEY_QUANTUM = Decimal("0.01")

#: The digits `base_salary` holds: `numeric(14, 2)`, so at most twelve before the point —
#: `999999999999.99`. The bound is here as well as in the column, and for the reason the
#: scale is: a figure the column cannot hold would be refused by PostgreSQL as a
#: `numeric field overflow`, which reaches a client as a 500 rather than as the 422 the
#: caller's mistake deserves. A currency with three decimal places, or an amount larger
#: than this, is a widening of the column — a migration somebody makes deliberately, not
#: a value this parser quietly truncates.
MAX_MONEY = Decimal("999999999999.99")

#: How long a change reason may be. Long enough for "ajuste por convenio 2026", short
#: enough that the column cannot become the free-text field this design avoids.
MAX_REASON_CHARS = 500

#: How many allowance lines one record may carry. A bound rather than a policy: a record
#: with a hundred lines is a file that was pasted into the wrong field.
MAX_COMPONENTS = 50

#: How long an allowance's label may be.
MAX_LABEL_CHARS = 120

#: How long an allowance's code may be.
MAX_CODE_CHARS = 40


class PayPeriod(StrEnum):
    """How the salary is paid out.

    Three values. **Nothing multiplies one into another** — a monthly figure is not
    divided into a weekly one here, and the field exists so a payroll reader knows which
    period the figure covers, not so this system can prorate it (§8.3).
    """

    MONTHLY = "monthly"
    BIWEEKLY = "biweekly"
    WEEKLY = "weekly"


class ChangeReasonType(StrEnum):
    """What kind of event put this figure in force."""

    INITIAL = "initial"
    ADJUSTMENT = "adjustment"
    CORRECTION = "correction"


@dataclass(frozen=True, slots=True)
class AllowanceLine:
    """One line of the structured allowance breakdown: §3.5's 「津贴明细」.

    `code` is what a payroll reader matches on, `label` is what a person reads, and
    `amount` is a **string** in the record's currency. A string rather than a number
    because this value travels through JSON, whose only number type is a float — and a
    float is how a cent goes missing. The same decision as `base_salary`, one level
    down, and for the same reason.
    """

    code: str
    label: str
    amount: str


@dataclass(frozen=True, slots=True)
class SalaryRecord:
    """One stored record: a figure in force over a window, and who entered it."""

    id: UUID
    employee_id: UUID
    effective_from: date
    effective_to: date | None
    base_salary: Decimal
    currency: str
    pay_period: str
    components: tuple[AllowanceLine, ...]
    change_reason_type: str
    change_reason: str
    created_by_user_id: UUID | None
    created_at: datetime | None = None

    @property
    def is_open(self) -> bool:
        """True while this record has no stated end — the one in force."""
        return self.effective_to is None

    @property
    def stored_amount(self) -> str:
        """The base figure as the wire carries it: exact, two places, no float.

        `quantize` is applied on construct as well, so this cannot disagree with the
        column's scale; the property exists so that "how an amount is serialised" is
        stated in one place rather than in every schema that carries one.
        """
        return f"{self.base_salary:.2f}"


@dataclass(frozen=True, slots=True)
class RecordQuery:
    """Which slice of one person's chain a caller asked for.

    `as_of` is the ticket's 「任一时点可查出当时有效的值」: when it is set, the repository
    answers with the *one* record whose window covers that day, so the fold happens in
    the `WHERE` clause and not in a loop over rows. `None` means the whole chain,
    oldest first.
    """

    employee_id: UUID
    as_of: date | None = None
    limit: int = 100
    offset: int = 0


@dataclass(frozen=True, slots=True)
class RecordInput:
    """A record to store. Everything a caller may state, and nothing they may not.

    `created_by_user_id` is the actor, not a field: the archive's answer to "who entered
    it" is the login the request ran under, and a body that could name somebody else
    would be a claim rather than a fact.
    """

    employee_id: UUID
    effective_from: date
    effective_to: date | None
    base_salary: Decimal
    currency: str
    pay_period: str
    components: tuple[AllowanceLine, ...]
    change_reason_type: str
    change_reason: str
    created_by_user_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class RecordView:
    """A record as a response carries it. The row, plus the subject's name.

    **No money is folded here.** There is no total, no annual figure and no conversion;
    `base_salary` and the allowance lines travel exactly as they are stored. The one
    derived-looking field a reader might expect — the sum of the allowances — is absent
    on purpose, and `tests/test_salary_records.py` asserts the response's numeric fields
    are exactly the stored ones.
    """

    record: SalaryRecord
    employee_name: str


@dataclass(frozen=True, slots=True)
class Reading:
    """What one person's chain read as, **and the fact that it was read**.

    This is the ticket's unusual requirement made structural. A caller cannot obtain the
    rows any other way: `SalaryReading.__init__` refuses every construction that did not
    come from the module that also writes the audit entry, so a route that served salary
    figures without recording the read has nothing to serve.

    It is a plain value — the fields are public and it is frozen — so the seal is on
    *construction*, not on use. A caller may read what it was given; it may not make one.
    """

    rows: tuple[RecordView, ...]
    subject: UUID
    as_of: date | None
    #: How many rows the caller's reach holds for this subject, which is not always
    #: `len(rows)`: a page is a slice of the chain, and a client paginating needs the
    #: size of the whole. It is the same filter the rows were read with, asked as a
    #: count, so the two cannot disagree about what is reachable.
    total: int = 0
    notice_key: str = ARCHIVE_NOTICE_KEY


#: Identity token proving a reading came from this module. Module-private, and compared
#: by identity, so a copy of the object cannot stand in for it.
_READING_TOKEN = object()


@dataclass(frozen=True, slots=True, init=False)
class SalaryReading(Reading):
    """A `Reading` that only this package can produce.

    The leading underscore does the work: `SalaryReading(...)` raises `TypeError`, so the
    only way to hold one is `SalaryReading._issued(...)`, which `SalaryService.read(...)`
    calls *after* it has written the audit entry. `FilterSpec` uses the same mechanism
    for the same reason — "forgot to filter" and "forgot to record" are both failures
    that a type can remove rather than a review can catch.
    """

    def __init__(self, *, _token: object = None, reading: Reading | None = None) -> None:
        if _token is not _READING_TOKEN or reading is None:
            raise TypeError(
                "SalaryReading is produced by SalaryService.read(); it has no public "
                "constructor"
            )
        object.__setattr__(self, "rows", reading.rows)
        object.__setattr__(self, "subject", reading.subject)
        object.__setattr__(self, "as_of", reading.as_of)
        object.__setattr__(self, "total", reading.total)
        object.__setattr__(self, "notice_key", reading.notice_key)

    @classmethod
    def _issued(cls, reading: Reading) -> "SalaryReading":
        """Private by convention *and* by token: the only in-module construction path."""
        return cls(_token=_READING_TOKEN, reading=reading)


def notice_payload() -> dict[str, Any]:
    """The disclaimer as a response carries it: the key, and the sentence per language.

    One function so the two places it travels — the chain's page and the record an entry
    returns — cannot serve different wording, and so that "what the archive tells a
    reader about itself" is a single object rather than a string composed at each route.
    """
    return {
        "message_key": ARCHIVE_NOTICE_KEY,
        "text": dict(ARCHIVE_NOTICE_TEXT),
    }


def parse_amount(value: Any, field_name: str = "base_salary") -> Decimal:
    """A money value as the archive stores it, or a refusal naming the field.

    Accepts a `Decimal`, an `int`, a `float` or the string form of any of them, and
    returns a `Decimal` quantised to two places. Three rules, all of them about
    precision rather than taste:

    * the value goes through `str()` before `Decimal()`, so a JSON number arrives as the
      digits that were written rather than as the nearest binary double;
    * anything finer than a cent is **refused**, not rounded — rounding a salary on the
      way in is how a cent goes missing, and there is no second place to notice it;
    * a non-finite value (`NaN`, `Infinity`) is refused, because `numeric(14, 2)` has no
      representation for it and the failure would otherwise surface as a 500.
    """
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise _invalid(f"{field_name!r} must be a number") from None
    if not amount.is_finite():
        raise _invalid(f"{field_name!r} must be a finite amount")
    if amount <= 0:
        raise _invalid(f"{field_name!r} must be a positive amount")
    if MAX_MONEY < amount:
        # The column's own maximum, refused here rather than by PostgreSQL: a figure
        # `numeric(14, 2)` cannot hold comes back from the driver as a `numeric field
        # overflow`, which reaches the caller as a 500 instead of the 422 this is.
        raise _invalid(
            f"{field_name!r} is larger than the archive holds ({MAX_MONEY}); a wider "
            "column is a migration, not a value this module rounds"
        )
    if amount != amount.quantize(MONEY_QUANTUM):
        raise _invalid(
            f"{field_name!r} must not be finer than a cent; {amount} has more than "
            "two decimal places"
        )
    return amount.quantize(MONEY_QUANTUM)


def parse_components(entries: Any) -> tuple[AllowanceLine, ...]:
    """The allowance breakdown, or a refusal.

    A list of objects with `code`, `label` and `amount`. The amounts are normalised to
    the two-place string form, which is what makes "a float in a JSONB column" a
    refused input rather than a stored rounding: `0.1 + 0.2` cannot be written here
    because `"0.30000000000000004"` is refused as finer than a cent.

    An empty or absent breakdown is `()` — most records carry a base and nothing else —
    so this is a normaliser, not a validator that can only say yes.
    """
    if entries is None:
        return ()
    if not isinstance(entries, list | tuple):
        raise _invalid("'components' must be a list of allowance lines")
    if len(entries) > MAX_COMPONENTS:
        raise _invalid(f"'components' may hold at most {MAX_COMPONENTS} lines")

    lines: list[AllowanceLine] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise _invalid(f"'components[{index}]' must be an object")
        code = _text(entry.get("code"), f"components[{index}].code", MAX_CODE_CHARS)
        label = _text(entry.get("label"), f"components[{index}].label", MAX_LABEL_CHARS)
        if code in seen:
            # One line per code: two lines with the same code are two answers to "what
            # is this allowance", and a reader has no way to pick.
            raise _invalid(f"'components[{index}].code' repeats {code!r}")
        seen.add(code)
        amount = parse_amount(entry.get("amount"), f"components[{index}].amount")
        lines.append(AllowanceLine(code=code, label=label, amount=f"{amount:.2f}"))
    return tuple(lines)


def parse_currency(value: Any) -> str:
    """ISO 4217, upper case. Never converted, never defaulted from another record."""
    if value is None:
        from app.models.payroll import EUROS

        return EUROS
    if not isinstance(value, str) or len(value) != 3 or not value.isalpha():
        raise _invalid("'currency' must be a three-letter ISO 4217 code")
    if value != value.upper():
        raise _invalid("'currency' must be upper case, as ISO 4217 writes it")
    return value


def parse_pay_period(value: Any) -> str:
    try:
        return PayPeriod(str(value)).value
    except ValueError:
        raise _invalid(
            "'pay_period' must be one of "
            + ", ".join(period.value for period in PayPeriod)
        ) from None


def parse_reason_type(value: Any) -> str:
    try:
        return ChangeReasonType(str(value)).value
    except ValueError:
        raise _invalid(
            "'change_reason_type' must be one of "
            + ", ".join(kind.value for kind in ChangeReasonType)
        ) from None


def parse_reason(value: Any) -> str:
    return _text(value, "change_reason", MAX_REASON_CHARS)


def parse_window(effective_from: Any, effective_to: Any) -> tuple[date, date | None]:
    """The record's window, or a refusal. Inclusive at both ends.

    An end before the start is refused here *and* by
    `ck_salary_records_effective_range`: the message a client reads comes from here, and
    the database is what makes it true even for a writer that skips this function.
    """
    start = _date(effective_from, "effective_from")
    end = None if effective_to is None else _date(effective_to, "effective_to")
    if end is not None and end < start:
        raise _invalid(f"'effective_to' ({end}) is before 'effective_from' ({start})")
    return start, end


def _date(value: Any, field_name: str) -> date:
    if isinstance(value, datetime):
        raise _invalid(f"{field_name!r} must be a date, not a timestamp")
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise _invalid(f"{field_name!r} must be an ISO date (YYYY-MM-DD)") from None


def _text(value: Any, field_name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"{field_name!r} must be a non-empty string")
    if len(value) > limit:
        raise _invalid(f"{field_name!r} is longer than {limit} characters")
    return value.strip()


def _invalid(detail: str) -> DomainError:
    return DomainError(PayrollErrorCode.RECORD_INVALID, detail=detail)


__all__ = [
    "ARCHIVE_NOTICE_KEY",
    "ARCHIVE_NOTICE_TEXT",
    "MAX_COMPONENTS",
    "MAX_MONEY",
    "MONEY_QUANTUM",
    "SALARY_ENTITY",
    "AllowanceLine",
    "ChangeReasonType",
    "PayPeriod",
    "Reading",
    "RecordInput",
    "RecordQuery",
    "RecordView",
    "SalaryReading",
    "SalaryRecord",
    "parse_amount",
    "parse_components",
    "parse_currency",
    "parse_pay_period",
    "parse_reason",
    "parse_reason_type",
    "parse_window",
    "notice_payload",
]
