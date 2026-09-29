"""Personnel change value objects.

One document for 入转调离 and five change types, all of them the same shape: a
change is a list of `(field, before, after)` items. A paragraph cannot be
applied, so there is no paragraph — the applier reads field names.

`parse_changes` is the whole payload contract: which fields a type carries,
which are required, and what kind of value each one holds. It is pure, so the
same rules can hold a form in the UI to what the applier will do, and it is what
makes "a change with no structured detail is refused" true at the one door a
change comes in through.

Three choices worth stating, because each of them is a decision rather than a
consequence:

* **`before` in the payload is what the caller asserts; `before` in the audit is
  what was read when the change was applied.** A second queued salary change
  cannot state a `before` that is true both when it is drafted and when it takes
  effect, so the payload's copy is the intent and the audit's is the fact.
* **A change that was not applied changed nothing.** `ChangeStatus` is what this
  module wrote down about itself; whether the engine approved the request is read
  from the engine (`state_of_change`) and never mirrored onto the row, because
  two copies of "was this approved" are two versions of the truth.
* **The type decides which fields exist.** A promotion may not carry a
  `department_id`: that is a transfer, and letting one type carry another's field
  is how a "promotion" quietly moves somebody between departments.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any
from uuid import UUID

from app.domain.approval.models import ApprovalState as ApprovalSnapshot
from app.domain.approval.models import ApprovalStatus
from app.domain.errors import DomainError
from app.domain.personnel.errors import PersonnelErrorCode


class ChangeType(StrEnum):
    """The five kinds of personnel change one document covers (DESIGN §7.6)."""

    JOIN = "join"
    TRANSFER = "transfer"
    PROMOTION = "promotion"
    SALARY = "salary"
    TERMINATION = "termination"


class ChangeStatus(StrEnum):
    """DESIGN §3.1's value domain for the change's own column.

    Four of these are written here: `draft` when the document is created,
    `pending` once it has been filed, `applied` or `cancelled` when one of those
    happened. `approved` is the *engine's* word for its request and is not written
    by this module — what the API publishes is derived from this column plus the
    engine's answer (`ChangeState`), which keeps one copy of "was this approved".
    The value stays in the domain so a future writer that does mirror the engine
    needs no migration.
    """

    DRAFT = "draft"
    PENDING = "pending"
    APPROVED = "approved"
    APPLIED = "applied"
    CANCELLED = "cancelled"


class ChangeState(StrEnum):
    """The states the UI has to tell apart (ticket 17).

    Four of them are the ticket's: 草稿, 审批中, 已批准待生效, 已生效. `cancelled`
    and `rejected` are the two ways a document leaves that path, and leaving them
    out would make a cancelled change look like a draft.
    """

    DRAFT = "draft"
    IN_APPROVAL = "in_approval"
    APPROVED_PENDING = "approved_pending"
    APPLIED = "applied"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


#: The kinds of value a field may hold. Named rather than typed, because the
#: catalogue is a table a reader scans, not a type hierarchy.
TEXT = "text"
DATE = "date"
IDENTIFIER = "identifier"
MONEY = "money"
FLAG = "flag"


@dataclass(frozen=True, slots=True)
class FieldSpec:
    kind: str
    required: bool = False


#: Which fields each change type carries. The whole of "structured, not free
#: text" is this table plus `parse_changes` below.
FIELD_SPECS: dict[ChangeType, dict[str, FieldSpec]] = {
    # A join creates the employee *and* their first assignment, so the assignment
    # is part of the same document: one approval for one arrival.
    ChangeType.JOIN: {
        "first_name": FieldSpec(TEXT, required=True),
        "last_name": FieldSpec(TEXT, required=True),
        "email": FieldSpec(TEXT, required=True),
        "hire_date": FieldSpec(DATE, required=True),
        "department_id": FieldSpec(IDENTIFIER, required=True),
        "job_position_id": FieldSpec(IDENTIFIER, required=True),
        "preferred_name": FieldSpec(TEXT),
        "employee_no": FieldSpec(TEXT),
        "is_part_time": FieldSpec(FLAG),
        "manager_employee_id": FieldSpec(IDENTIFIER),
    },
    ChangeType.TRANSFER: {
        "department_id": FieldSpec(IDENTIFIER, required=True),
        "job_position_id": FieldSpec(IDENTIFIER, required=True),
        "is_part_time": FieldSpec(FLAG),
        "manager_employee_id": FieldSpec(IDENTIFIER),
    },
    # No `department_id`: staying in the department is what makes it a promotion.
    ChangeType.PROMOTION: {
        "job_position_id": FieldSpec(IDENTIFIER, required=True),
        "manager_employee_id": FieldSpec(IDENTIFIER),
    },
    # The agreed figure, not a computed one. Ticket 43 added `salary_records`, and an
    # applied change now writes an archive row from this payload
    # (`service._apply_salary`), so these two fields are the *change*: which figure was
    # agreed and when it takes effect.
    #
    # **The other half of the archive is deliberately not here.** A record also carries a
    # pay period, a structured allowance breakdown and a reason, and those belong to the
    # archive's own endpoint (`POST /api/v1/salary/records`) rather than to this
    # document's field catalogue: this table is a *generic* payment of fields whose kinds
    # are text, date, identifier, money and flag, and a JSON array of allowance lines is
    # none of the five. Teaching it a sixth kind so a raise could restate a breakdown
    # would widen every change type to serve one, and the archive is where a breakdown is
    # edited. What the applier does instead is stated and tested: it writes the record
    # with the archive's own defaults for those three and says so in the ticket file.
    ChangeType.SALARY: {
        "base_salary": FieldSpec(MONEY, required=True),
        "currency": FieldSpec(TEXT),
    },
    ChangeType.TERMINATION: {
        "termination_date": FieldSpec(DATE, required=True),
        # Text, not a fixed vocabulary: `before` is whatever status the record had
        # ("active"), and only the new value is constrained — to `terminated`, by
        # `_require_consistent` below.
        "status": FieldSpec(TEXT, required=True),
    },
}

#: The only employment status a termination may write. The set is fixed, like
#: every other vocabulary here, and `terminated` is the only one this document
#: type is allowed to state.
TERMINATED = "terminated"

#: How many characters a text field may hold. Long enough for a name or an email,
#: short enough that a payload cannot become the free-text field this design bans.
MAX_TEXT = 200


@dataclass(frozen=True, slots=True)
class FieldChange:
    """One field, its previous value and its new one.

    `before` is `None` when the field had no value; for a join that is every
    field, because there was nobody there.
    """

    field: str
    before: Any | None
    after: Any


def parse_changes(
    change_type: ChangeType, entries: object, *, effective_date: date
) -> tuple[FieldChange, ...]:
    """Read a payload into the fields its type allows, or refuse it.

    Every refusal is the same catalogued code: what a client does with "the
    payload is wrong" does not depend on which field was wrong, and the detail
    says which. The rules that need the database — does that department exist,
    is that position in the employee's own department — are the service's,
    because this function is also what a form can be held to before a request.
    """
    specs = FIELD_SPECS[change_type]
    if isinstance(entries, str | bytes) or not isinstance(entries, Sequence) or not entries:
        raise _invalid("changes must be a non-empty list")

    parsed: list[FieldChange] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise _invalid("each change must be an object with field, before and after")
        name = entry.get("field")
        if not isinstance(name, str) or not name:
            raise _invalid("each change must name its field")
        spec = specs.get(name)
        if spec is None:
            raise _invalid(f"{change_type} does not carry {name!r}")
        if name in seen:
            raise _invalid(f"{name!r} appears twice")
        if "before" not in entry:
            raise _invalid(f"{name!r} states no previous value (use null when there was none)")
        if "after" not in entry:
            raise _invalid(f"{name!r} states no new value")
        seen.add(name)

        before = None if entry["before"] is None else _coerce(spec.kind, entry["before"], name)
        after = None if entry["after"] is None else _coerce(spec.kind, entry["after"], name)
        if after is None:
            raise _invalid(f"{name!r} states no new value")
        if change_type is ChangeType.JOIN and before is not None:
            raise _invalid(f"a join has nothing before it, and {name!r} states one")
        parsed.append(FieldChange(field=name, before=before, after=after))

    missing = sorted(name for name, spec in specs.items() if spec.required and name not in seen)
    if missing:
        raise _invalid(f"missing required field(s): {', '.join(missing)}")

    return _require_consistent(change_type, tuple(parsed), effective_date)


def _require_consistent(
    change_type: ChangeType, changes: tuple[FieldChange, ...], effective_date: date
) -> tuple[FieldChange, ...]:
    """Rules that hold between fields, and between a field and the effective date.

    Checked here rather than at application time because a change that cannot be
    applied is refused while somebody can still correct it, not on the morning it
    was supposed to take effect.
    """
    values = {change.field: change.after for change in changes}
    if change_type is ChangeType.JOIN and values["hire_date"] != effective_date:
        raise _invalid(
            f"a join takes effect on the hire date: {values['hire_date']} is not {effective_date}"
        )
    if change_type is ChangeType.TERMINATION:
        if values["termination_date"] != effective_date:
            raise _invalid(
                f"a termination takes effect on its termination date: "
                f"{values['termination_date']} is not {effective_date}"
            )
        if values["status"] != TERMINATED:
            raise _invalid(f"a termination states status {TERMINATED!r}")
    return changes


def payload_of(change_type: ChangeType, changes: Sequence[FieldChange]) -> dict[str, Any]:
    """The payload as stored: JSONB, so every value is a JSON value.

    Dates become ISO strings, identifiers become strings and money becomes a
    two-decimal string. Money as a string is deliberate: `0.1 + 0.2` has no place
    in a salary, and a JSON number would be a float by the time anything read it.
    """
    return {
        "changes": [
            {
                "field": change.field,
                "before": _stored(change_type, change.field, change.before),
                "after": _stored(change_type, change.field, change.after),
            }
            for change in changes
        ]
    }


def state_of_change(change: "PersonnelChange", approval: ApprovalStatus | None) -> ChangeState:
    """Which of the six states a change is in.

    Two local facts decide the terminal states — an applied change cannot be
    un-applied by a later approval, and a cancelled one is not resurrected by a
    decision that arrived after somebody stopped it. Everything else is the
    engine's answer, mapped to the vocabulary the UI reads.
    """
    if change.applied_at is not None:
        return ChangeState.APPLIED
    if change.cancelled_at is not None:
        return ChangeState.CANCELLED
    if approval is None:
        return ChangeState.DRAFT
    if approval in (ApprovalStatus.PENDING_FIRST, ApprovalStatus.PENDING_SECOND):
        return ChangeState.IN_APPROVAL
    if approval is ApprovalStatus.APPROVED:
        return ChangeState.APPROVED_PENDING
    if approval is ApprovalStatus.REJECTED:
        return ChangeState.REJECTED
    # `draft` (returned for correction) and `withdrawn` are both this document
    # being the requester's own again: correct it and file it once more.
    return ChangeState.DRAFT


@dataclass(frozen=True, slots=True)
class PersonnelChange:
    """One change document, as stored.

    `employee_id` is empty until a join is applied: creating the employee record
    when the draft is written would put a hire into the directory before the day
    it was agreed for, which is the leak this whole ticket exists to prevent.
    """

    id: UUID
    change_type: ChangeType
    effective_date: date
    changes: tuple[FieldChange, ...]
    status: ChangeStatus
    created_by_employee_id: UUID
    employee_id: UUID | None
    approval_request_id: UUID | None
    applied_values: dict[str, Any] | None
    applied_at: datetime | None
    cancelled_at: datetime | None
    cancelled_by_employee_id: UUID | None
    cancel_reason: str | None
    created_at: datetime
    updated_at: datetime

    @property
    def values(self) -> dict[str, Any]:
        """The new value of each field, by name — what the applier reads."""
        return {change.field: change.after for change in self.changes}

    @property
    def payload(self) -> dict[str, Any]:
        return payload_of(self.change_type, self.changes)

    @property
    def before_values(self) -> dict[str, Any]:
        """The asserted previous values, as the audit log can hold them."""
        return {
            change.field: _stored(self.change_type, change.field, change.before)
            for change in self.changes
        }

    @property
    def after_values(self) -> dict[str, Any]:
        return {
            change.field: _stored(self.change_type, change.field, change.after)
            for change in self.changes
        }


@dataclass(frozen=True, slots=True)
class ChangeInput:
    """A draft to write."""

    change_type: ChangeType
    effective_date: date
    created_by_employee_id: UUID
    changes: tuple[FieldChange, ...]
    employee_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ChangeQuery:
    """What the list endpoint is asked for."""

    employee_id: UUID | None = None
    #: The *derived* state, not the stored one: "waiting to take effect" is the
    #: question HR asks, and the stored column cannot answer it.
    state: ChangeState | None = None
    change_type: ChangeType | None = None
    limit: int = 50
    offset: int = 0


@dataclass(frozen=True, slots=True)
class PersonnelChangeView:
    """A change with the two things a reader needs to make sense of it."""

    change: PersonnelChange
    state: ChangeState
    approval: ApprovalSnapshot | None = None


@dataclass(frozen=True, slots=True)
class ApplyFailure:
    """One change the job could not apply, and why."""

    change_id: UUID
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class ApplyReport:
    """What one run of the job did.

    Reported rather than raised: a change that can never be applied must not stop
    the ones behind it, and it is not retried into a hole either — it stays
    unapplied and is reported again by the next run, which is where an operator
    will see it.
    """

    applied: tuple[UUID, ...] = ()
    failed: tuple[ApplyFailure, ...] = ()
    #: Rows locked and looked at, including the ones the engine has not approved.
    examined: int = 0


def _invalid(detail: str) -> DomainError:
    return DomainError(PersonnelErrorCode.PERSONNEL_CHANGE_INVALID_PAYLOAD, detail=detail)


def _coerce(kind: str, value: Any, field: str) -> Any:
    if kind is TEXT:
        if not isinstance(value, str) or not value.strip():
            raise _invalid(f"{field!r} must be a non-empty string")
        if len(value) > MAX_TEXT:
            raise _invalid(f"{field!r} is longer than {MAX_TEXT} characters")
        return value.strip()
    if kind is DATE:
        if isinstance(value, datetime):
            raise _invalid(f"{field!r} must be a date, not a timestamp")
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value))
        except ValueError:
            raise _invalid(f"{field!r} must be an ISO date (YYYY-MM-DD)") from None
    if kind is IDENTIFIER:
        if isinstance(value, UUID):
            return value
        try:
            return UUID(str(value))
        except ValueError:
            raise _invalid(f"{field!r} must be a UUID") from None
    if kind is MONEY:
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise _invalid(f"{field!r} must be a number") from None
        if not amount.is_finite() or amount <= 0:
            raise _invalid(f"{field!r} must be a positive amount")
        return amount.quantize(Decimal("0.01"))
    if kind is FLAG:
        if not isinstance(value, bool):
            raise _invalid(f"{field!r} must be true or false")
        return value
    raise AssertionError(f"unknown field kind {kind!r}")  # pragma: no cover - catalogue typo


def _stored(change_type: ChangeType, field: str, value: Any) -> Any:
    """One value as JSONB holds it. `None` stays `None`."""
    if value is None:
        return None
    return _render(FIELD_SPECS[change_type][field].kind, value)


def _render(kind: str, value: Any) -> Any:
    if kind is DATE:
        return value.isoformat()
    if kind is IDENTIFIER:
        return str(value)
    if kind is MONEY:
        return f"{value:.2f}"
    return value


__all__ = [
    "ApplyFailure",
    "ApplyReport",
    "ChangeInput",
    "ChangeQuery",
    "ChangeState",
    "ChangeStatus",
    "ChangeType",
    "FieldChange",
    "FieldSpec",
    "PersonnelChange",
    "PersonnelChangeView",
    "TERMINATED",
    "parse_changes",
    "payload_of",
    "state_of_change",
]
