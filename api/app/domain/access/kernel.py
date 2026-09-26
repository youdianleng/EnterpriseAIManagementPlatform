"""The authorization kernel.

The only place in the system that answers "may this person do this to this
thing". Three entry points, and nothing else:

    can(principal, action, resource) -> Decision
    filter_for(principal, kind)      -> FilterSpec
    apply_rls_context(session, principal)

**Why `filter_for` returns data, not a query.** The same decision is consumed by
a document list, a vector search and a test. A query builder would have to be
reimplemented per store and could not be asserted without a database. A plain
description of what is reachable can be asserted directly, and each caller
translates it into its own query language.

**Why `FilterSpec` has no public constructor.** A caller cannot invent a filter
that allows everything, because the only way to obtain one is to ask this module
for it. "Forgot to filter" stops being possible rather than being a review
concern.

**`Decision` carries a reason.** The audit log has to say *why* something was
allowed or refused, and an incident review needs the rule that fired, not just
the outcome.
"""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from app.domain.access.permissions import Action, roles_may, rule_for
from app.domain.access.principal import Principal

CLEARANCE_RANK = {"low": 0, "medium": 1, "high": 2}


class ResourceKind(StrEnum):
    """What kind of thing a filter describes."""

    DOCUMENT = "document"
    EMPLOYEE = "employee"
    DEPARTMENT = "department"
    POSITION = "position"
    ACCOUNT = "account"
    AUDIT_LOG = "audit_log"


@dataclass(slots=True, frozen=True)
class Resource:
    """The facts a decision needs about the thing being acted on.

    Deliberately small: `clearance` exists for documents, `department_id` for
    anything that lives in the tree, `owner_employee_id` for anything a person
    owns. A resource that carried its own ORM row would couple this module to
    storage.

    Ownership is expressed as an *employee* id, matching the employee module.
    Using a user id here would create a second notion of "who owns this", and the
    two would eventually disagree.
    """

    kind: ResourceKind
    department_id: UUID | None = None
    clearance: str | None = None
    owner_employee_id: UUID | None = None
    is_company_kb: bool = False


class Reason(StrEnum):
    """Why a decision came out the way it did.

    Enumerated so the audit trail is queryable ("show me everything refused for
    clearance") rather than a pile of prose.
    """

    ROLE_PERMITS = "role_permits"
    PUBLIC_ACTION = "public_action"
    IS_OWNER = "is_owner"
    SHARES_DEPARTMENT = "shares_department"
    IS_PRIVILEGED = "is_privileged"
    MANAGER_OF_SUBJECT = "manager_of_subject"
    UNKNOWN_ACTION = "unknown_action"
    ROLE_LACKS_PERMISSION = "role_lacks_permission"
    CLEARANCE_TOO_LOW = "clearance_too_low"
    DEPARTMENT_NOT_REACHABLE = "department_not_reachable"
    NOT_OWNER = "not_owner"
    PRIVILEGED_ROLE_REQUIRED = "privileged_role_required"


@dataclass(slots=True, frozen=True)
class Decision:
    allowed: bool
    reasons: tuple[Reason, ...] = ()
    #: Human-readable, for the operator reading a log. Never shown to a client.
    detail: str = ""

    @property
    def denied(self) -> bool:
        return not self.allowed

    @property
    def primary_reason(self) -> Reason | None:
        return self.reasons[0] if self.reasons else None

    def as_audit_fields(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "reasons": [reason.value for reason in self.reasons],
        }


class FilterSpec:
    """What a principal may reach, as data.

    No public constructor: instances come from `filter_for` alone. The leading
    underscore on `__init__` is the mechanism — a caller can read a spec but
    cannot fabricate one.

    `_token` defaults to `None` rather than being required, so omitting it
    produces this module's explicit refusal instead of a generic "missing keyword
    argument" — a guard whose message depends on how the caller got it wrong
    reads like an accident rather than a rule.
    """

    __slots__ = (
        "kind",
        "allow_all",
        "department_ids",
        "clearance_levels",
        "own_employee_id",
        "include_company_kb",
    )

    def __init__(
        self,
        *,
        _token: object = None,
        kind: ResourceKind,
        allow_all: bool,
        department_ids: frozenset[UUID],
        clearance_levels: frozenset[str],
        own_employee_id: UUID | None,
        include_company_kb: bool,
    ) -> None:
        if _token is not _FILTER_TOKEN:
            raise TypeError(
                "FilterSpec is produced by filter_for(); it has no public constructor"
            )
        self.kind = kind
        self.allow_all = allow_all
        self.department_ids = department_ids
        self.clearance_levels = clearance_levels
        self.own_employee_id = own_employee_id
        self.include_company_kb = include_company_kb

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        if self.allow_all:
            return f"<FilterSpec {self.kind} allow_all>"
        return (
            f"<FilterSpec {self.kind} departments={len(self.department_ids)} "
            f"clearance<={sorted(self.clearance_levels)} own={self.own_employee_id is not None}>"
        )


#: Identity token proving a spec came from this module.
_FILTER_TOKEN = object()


def _clearances_up_to(level: str) -> frozenset[str]:
    """Every clearance at or below `level`. Reading upward is never allowed."""
    limit = CLEARANCE_RANK.get(level, 0)
    return frozenset(name for name, rank in CLEARANCE_RANK.items() if rank <= limit)


# --- the three entry points ------------------------------------------------


def can(principal: Principal, action: Action, resource: Resource | None = None) -> Decision:
    """May this principal perform this action on this resource?

    An unknown action is refused. A default of "allow" for something nobody
    catalogued would mean the catalogue silently stops being the answer.
    """
    rule = rule_for(action) if action in Action else None
    if rule is None:
        return Decision(False, (Reason.UNKNOWN_ACTION,), f"{action} is not catalogued")

    if rule.public:
        return Decision(True, (Reason.PUBLIC_ACTION,), f"{action} is public")

    # Reading a withheld field is decided by role alone; there is no "just this
    # once" and no resource that makes it acceptable for another role.
    if action is Action.EMPLOYEE_READ_WITHHELD:
        if principal.is_privileged:
            return Decision(True, (Reason.IS_PRIVILEGED,), "privileged role")
        return Decision(
            False, (Reason.PRIVILEGED_ROLE_REQUIRED,), "withheld fields need hr/finance/compliance"
        )

    if not roles_may(action, principal.roles):
        # The action name is in the detail so a log line says what was attempted,
        # not only which roles were held.
        return Decision(
            False,
            (Reason.ROLE_LACKS_PERMISSION,),
            f"{action} needs one of {sorted(rule.roles)}; "
            f"the principal holds {sorted(principal.roles)}",
        )

    if resource is None:
        return Decision(True, (Reason.ROLE_PERMITS,), "role permits the action")

    return _can_on_resource(principal, action, resource)


def _can_on_resource(principal: Principal, action: Action, resource: Resource) -> Decision:
    """Resource-level nuance, applied only after the role has permitted."""
    reasons: list[Reason] = [Reason.ROLE_PERMITS]

    # Ownership always grants read access to one's own material, whatever the
    # department or clearance rules say.
    if (
        action in {Action.EMPLOYEE_READ, Action.EMPLOYEE_READ_OWN, Action.DOCUMENT_READ}
        and resource.owner_employee_id is not None
        and resource.owner_employee_id == principal.employee_id
    ):
        return Decision(True, (Reason.IS_OWNER,), "the principal owns the resource")

    # Clearance is an upper bound that no role lifts except the privileged ones.
    if resource.clearance is not None and not principal.is_privileged:
        allowed_levels = _clearances_up_to(principal.clearance_level)
        if resource.clearance not in allowed_levels:
            return Decision(
                False,
                (Reason.CLEARANCE_TOO_LOW,),
                f"{resource.clearance} is above {principal.clearance_level}",
            )
        reasons.append(Reason.ROLE_PERMITS)

    if principal.is_privileged:
        return Decision(True, (Reason.IS_PRIVILEGED,), "privileged role reaches every department")

    # A manager reaches their own reports regardless of department boundaries,
    # which is what makes approving someone in another team possible.
    if (
        resource.kind is ResourceKind.EMPLOYEE
        and resource.owner_employee_id is not None
        and resource.owner_employee_id in principal.reports_employee_ids
    ):
        return Decision(True, (Reason.MANAGER_OF_SUBJECT,), "principal approves for this person")

    if resource.department_id is not None:
        if principal.covers_department(resource.department_id):
            return Decision(True, (*reasons, Reason.SHARES_DEPARTMENT), "department is reachable")
        return Decision(
            False,
            (Reason.DEPARTMENT_NOT_REACHABLE,),
            f"department {resource.department_id} is outside the principal's scope",
        )

    # No department and no ownership: the role check above is the whole answer.
    return Decision(True, tuple(reasons), "role permits and nothing further restricts")


def filter_for(principal: Principal, kind: ResourceKind) -> FilterSpec:
    """Describe what this principal may reach for one kind of resource.

    Privileged roles get `allow_all`, because their access does not depend on the
    row. Everyone else gets the concrete bounds, including their own id so that
    ownership can be expressed in the same query.
    """
    privileged = principal.is_privileged

    if kind is ResourceKind.DOCUMENT:
        return FilterSpec(
            _token=_FILTER_TOKEN,
            kind=kind,
            allow_all=privileged,
            department_ids=frozenset() if privileged else principal.department_ids,
            clearance_levels=(
                frozenset(CLEARANCE_RANK)
                if privileged
                else _clearances_up_to(principal.clearance_level)
            ),
            own_employee_id=principal.employee_id,
            # Personal uploads never enter the company pool; the document module
            # reads this flag to keep them out of shared retrieval.
            include_company_kb=True,
        )

    if kind is ResourceKind.EMPLOYEE:
        return FilterSpec(
            _token=_FILTER_TOKEN,
            kind=kind,
            allow_all=privileged,
            department_ids=frozenset() if privileged else principal.department_ids,
            clearance_levels=frozenset(CLEARANCE_RANK),
            own_employee_id=principal.employee_id,
            include_company_kb=False,
        )

    # Structure and administration are organisation-wide for anyone whose role
    # passed the action check; the filter records that rather than pretending
    # otherwise.
    return FilterSpec(
        _token=_FILTER_TOKEN,
        kind=kind,
        allow_all=True,
        department_ids=frozenset(),
        clearance_levels=frozenset(CLEARANCE_RANK),
        own_employee_id=principal.employee_id,
        include_company_kb=False,
    )


def apply_rls_context(session, principal: Principal) -> None:  # noqa: ANN001 - AsyncSession
    """Publish the principal to the database for this transaction.

    Postgres policies read these settings, which is the second line of defence:
    if a query in the application forgets its filter, the database still refuses
    the rows. It must run inside the transaction and before any query — that
    ordering is part of the interface, not an implementation detail.

    `set_config(..., is_local => true)` is scoped to the transaction, so the
    settings cannot leak to another request that later borrows the same pooled
    connection.

    Ticket 13 installs the policies these settings feed; this publishes them.
    """
    from sqlalchemy import text

    session.info["principal"] = principal
    session.execute(
        text(
            """
            SELECT set_config('app.current_user_id', :user_id, true),
                   set_config('app.current_employee_id', :employee_id, true),
                   set_config('app.current_clearance', :clearance, true),
                   set_config('app.is_privileged', :privileged, true)
            """
        ),
        {
            "user_id": str(principal.user_id),
            "employee_id": str(principal.employee_id),
            "clearance": principal.clearance_level,
            "privileged": "true" if principal.is_privileged else "false",
        },
    )


def department_scope_ids(spec: FilterSpec) -> frozenset[UUID] | None:
    """Helper for callers translating a spec into SQL.

    Returns None for `allow_all`, which reads as "no department predicate" rather
    than "match nothing" — the distinction matters, and a bare empty set would be
    read the wrong way round.
    """
    return None if spec.allow_all else spec.department_ids


__all__ = [
    "CLEARANCE_RANK",
    "Decision",
    "FilterSpec",
    "Reason",
    "Resource",
    "ResourceKind",
    "apply_rls_context",
    "can",
    "department_scope_ids",
    "filter_for",
]
