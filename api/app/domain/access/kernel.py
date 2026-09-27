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

from app.domain.access.permissions import (
    DOCUMENT_CROSS_DEPARTMENT_ROLES,
    SELF_ONLY_ACTIONS,
    Action,
    roles_may,
    rule_for,
)
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
    #: Whether `document_permissions` names this caller (directly, or through a
    #: department or role grant the document module resolved). It is a fact the
    #: document module looks up, never a request parameter: it grants reading, but
    #: it does not lift the clearance ceiling.
    explicit_grant: bool = False


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
    CLEARANCE_OK = "clearance_ok"
    DEPARTMENT_NOT_REACHABLE = "department_not_reachable"
    NOT_OWNER = "not_owner"
    NOT_SHARED = "not_shared"
    EXPLICIT_GRANT = "explicit_grant"
    DOCUMENT_EXCEPTION_ROLE = "document_exception_role"
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
        "explicit_grant_employee_id",
        "company_kb_cross_department",
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
        explicit_grant_employee_id: UUID | None = None,
        company_kb_cross_department: bool = False,
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
        #: The employee whose `document_permissions` rows count as an explicit
        #: grant, so a store can express the share clause in the same query.
        self.explicit_grant_employee_id = explicit_grant_employee_id
        #: True when this caller's roles reach company documents in every
        #: department. Company documents only: a personal upload stays bounded by
        #: ownership and by the share clause whatever the role.
        self.company_kb_cross_department = company_kb_cross_department

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        if self.allow_all:
            return f"<FilterSpec {self.kind} allow_all>"
        return (
            f"<FilterSpec {self.kind} departments={len(self.department_ids)} "
            f"clearance<={sorted(self.clearance_levels)} own={self.own_employee_id is not None} "
            f"cross_dept={self.company_kb_cross_department}>"
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
    # Documents have their own rule, stated once in `docs/DESIGN.md` §4.2 as four
    # clauses. It is not expressed with the generic department/clearance path
    # below, because that path *allows* a resource with no department and no
    # clearance — which for a document is somebody else's private upload.
    if resource.kind is ResourceKind.DOCUMENT:
        return _can_read_document(principal, resource)

    # Self-only actions, decided before anything else can widen them. Ownership is
    # the whole rule here rather than a clause of it, so this branch sits above the
    # privileged-role and manager-of-subject allowances: an HR member and a
    # manager are refused somebody else's attendance through these actions, and
    # reach it through their own actions instead (ticket 24). A resource that does
    # not name an owner is refused rather than allowed — "we cannot tell that it is
    # yours" is a refusal, not a permission, and a decision function whose default
    # on missing information is "yes" is how a filter-free query gets written.
    if action in SELF_ONLY_ACTIONS:
        if (
            resource.owner_employee_id is not None
            and resource.owner_employee_id == principal.employee_id
        ):
            return Decision(True, (Reason.IS_OWNER,), "the principal owns the resource")
        return Decision(
            False,
            (Reason.NOT_OWNER,),
            f"{action} is about the principal's own material; "
            f"owner={resource.owner_employee_id or 'unset'}",
        )

    reasons: list[Reason] = [Reason.ROLE_PERMITS]

    # Ownership always grants read access to one's own material, whatever the
    # department or clearance rules say. Documents are not in this set because
    # they were decided above, where the design states ownership as its own clause.
    if (
        action in {Action.EMPLOYEE_READ, Action.EMPLOYEE_READ_OWN}
        and resource.owner_employee_id is not None
        and resource.owner_employee_id == principal.employee_id
    ):
        return Decision(True, (Reason.IS_OWNER,), "the principal owns the resource")

    # Clearance is an upper bound. The privileged roles are above it for *employee*
    # material — that is what makes withheld fields readable — and the exemption
    # stops there: a document never reaches this branch, and its own rule has no
    # exception to the ceiling.
    if not principal.is_privileged and not _clearance_ok(principal, resource):
        return Decision(
            False,
            (Reason.CLEARANCE_TOO_LOW,),
            f"{resource.clearance} is above {principal.clearance_level}",
        )
    if resource.clearance is not None:
        reasons.append(Reason.CLEARANCE_OK)

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


def _clearance_ok(principal: Principal, resource: Resource) -> bool:
    """The ceiling: `rank(document) <= rank(principal)`.

    An unclassified resource is not treated as classified. Every write path sets
    a level, and the column is `NOT NULL`, so `None` here means "this caller did
    not tell the kernel", not "top secret".
    """
    if resource.clearance is None:
        return True
    return resource.clearance in _clearances_up_to(principal.clearance_level)


def _department_ok(principal: Principal, resource: Resource) -> bool:
    """`dept(document) ∈ departments(principal)`, descendants included.

    A document with no department is reachable by nobody through this clause; it
    is reached through ownership or an explicit grant instead.
    """
    if resource.department_id is None:
        return False
    return principal.covers_department(resource.department_id)


def _can_read_document(principal: Principal, resource: Resource) -> Decision:
    """The four clauses of `docs/DESIGN.md` §4.2, in the order the design lists
    them.

    Every clause is evaluated before answering, because they are alternatives: a
    low-clearance HR member is refused by the share clause and allowed by the
    exception clause, and short-circuiting on the first refusal would get that
    backwards. A refusal names every condition that failed, so an incident review
    does not have to re-derive them.
    """
    # 1. Your own document, whatever its classification. Hiding someone's own
    #    upload from them is not a security property, and the design states this
    #    clause without conditions.
    if (
        resource.owner_employee_id is not None
        and resource.owner_employee_id == principal.employee_id
    ):
        return Decision(True, (Reason.IS_OWNER,), "the principal owns the document")

    clearance_ok = _clearance_ok(principal, resource)
    department_ok = _department_ok(principal, resource)
    cross_department = bool(principal.roles & DOCUMENT_CROSS_DEPARTMENT_ROLES)

    # 2. The company knowledge base, with both conditions satisfied.
    if resource.is_company_kb and clearance_ok and department_ok:
        return Decision(
            True,
            (Reason.CLEARANCE_OK, Reason.SHARES_DEPARTMENT),
            "company document, clearance and department both satisfied",
        )

    # 3. An explicit grant — a personal document shared with this person, or a
    #    company document shared outside their department. The ceiling still
    #    applies: being named is not a reason to read above your clearance.
    if resource.explicit_grant and clearance_ok:
        return Decision(True, (Reason.EXPLICIT_GRANT,), "the document is shared with them")

    # 4. The exception roles, for company documents only, and only within their
    #    own clearance. This is the one clause that ignores departments, and it is
    #    deliberately not "is privileged": finance is privileged for payroll and
    #    has no business here. The ceiling is re-stated rather than inherited from
    #    clause 2, because a clause that forgot it would be the whole rule's undoing.
    if resource.is_company_kb and cross_department and clearance_ok:
        return Decision(
            True,
            (Reason.CLEARANCE_OK, Reason.DOCUMENT_EXCEPTION_ROLE),
            f"{sorted(principal.roles & DOCUMENT_CROSS_DEPARTMENT_ROLES)} reaches every department",
        )

    reasons: list[Reason] = []
    if not clearance_ok:
        reasons.append(Reason.CLEARANCE_TOO_LOW)
    if resource.is_company_kb:
        if not department_ok:
            reasons.append(Reason.DEPARTMENT_NOT_REACHABLE)
    elif not resource.explicit_grant:
        reasons.extend((Reason.NOT_OWNER, Reason.NOT_SHARED))
    # A personal document that *is* shared with them and is still refused leaves
    # only the ceiling, which is already recorded above.
    if not reasons:  # pragma: no cover - unreachable while the clauses above hold
        reasons.append(Reason.NOT_SHARED)

    return Decision(False, tuple(reasons), _document_refusal_detail(principal, resource))


def _document_refusal_detail(principal: Principal, resource: Resource) -> str:
    return (
        f"document company_kb={resource.is_company_kb} "
        f"clearance={resource.clearance or 'unset'} "
        f"department={resource.department_id or 'none'} "
        f"against clearance={principal.clearance_level} "
        f"departments={len(principal.department_ids)} shared={resource.explicit_grant}"
    )


def filter_for(principal: Principal, kind: ResourceKind) -> FilterSpec:
    """Describe what this principal may reach for one kind of resource.

    Privileged roles get `allow_all`, because their access does not depend on the
    row. Everyone else gets the concrete bounds, including their own id so that
    ownership can be expressed in the same query.
    """
    privileged = principal.is_privileged

    if kind is ResourceKind.DOCUMENT:
        # `allow_all` is never true for documents. It would read as "every row",
        # and every row includes other people's personal uploads, which the
        # decision rule never allows. The exception roles are recorded as their
        # own flag instead, because their reach is limited to company documents.
        return FilterSpec(
            _token=_FILTER_TOKEN,
            kind=kind,
            allow_all=False,
            department_ids=principal.department_ids,
            clearance_levels=_clearances_up_to(principal.clearance_level),
            own_employee_id=principal.employee_id,
            explicit_grant_employee_id=principal.employee_id,
            company_kb_cross_department=bool(
                principal.roles & DOCUMENT_CROSS_DEPARTMENT_ROLES
            ),
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


async def apply_rls_context(session, principal: Principal) -> None:  # noqa: ANN001 - AsyncSession
    """Publish the principal to the database for this transaction.

    Postgres policies read these settings, which is the second line of defence:
    if a query in the application forgets its filter, the database still refuses
    the rows. It must run inside the transaction and before any query — that
    ordering is part of the interface, not an implementation detail.

    `set_config(..., is_local => true)` is scoped to the transaction, so the
    settings cannot leak to another request that later borrows the same pooled
    connection.

    **Async, and awaited.** An earlier version called `session.execute` without
    awaiting it: with an `AsyncSession` that returns a coroutine which never runs,
    so the settings were never published. Nothing called it either, which is why
    the mistake was invisible — a guarantee that is never exercised is not a
    guarantee. Ticket 13 installs the policies these settings feed and calls this
    from the request path.

    The department and clearance *sets* are published as well as the identifiers:
    a policy cannot call `Principal.covers_department`, and re-deriving the set
    inside SQL would be a second implementation of the rule the kernel owns.
    """
    from sqlalchemy import text

    session.info["principal"] = principal
    await session.execute(
        text(
            """
            SELECT set_config('app.current_user_id', :user_id, true),
                   set_config('app.current_employee_id', :employee_id, true),
                   set_config('app.current_clearance', :clearance, true),
                   set_config('app.is_privileged', :privileged, true),
                   set_config('app.current_roles', :roles, true),
                   set_config('app.department_ids', :departments, true),
                   set_config('app.clearance_levels', :clearances, true)
            """
        ),
        {
            "user_id": str(principal.user_id),
            "employee_id": str(principal.employee_id),
            "clearance": principal.clearance_level,
            "privileged": "true" if principal.is_privileged else "false",
            # Postgres array literals. The values are ids and role names from the
            # snapshot, never text from a request.
            "roles": _array_literal(sorted(principal.roles)),
            "departments": _array_literal(
                sorted(str(value) for value in principal.department_ids)
            ),
            "clearances": _array_literal(sorted(_clearances_up_to(principal.clearance_level))),
        },
    )


def _array_literal(values: list[str]) -> str:
    """A Postgres array literal, for `set_config` and for comparison in policies.

    Quoted rather than interpolated, so a value containing a comma or a brace
    cannot turn one element into two. An empty list is `{}`, which is an empty
    array — the same as "this person reaches nothing", never "no restriction".
    """
    if not values:
        return "{}"
    quoted = (
        '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"' for value in values
    )
    return "{" + ",".join(quoted) + "}"


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
