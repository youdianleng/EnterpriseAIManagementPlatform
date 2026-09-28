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
    ATTENDANCE_CROSS_ACTIONS,
    COMPANY_RECORD_ROLES,
    DOCUMENT_CROSS_DEPARTMENT_ROLES,
    LEAVE_CROSS_ACTIONS,
    OVERTIME_COMPANY_ROLES,
    OVERTIME_CROSS_ACTIONS,
    PROJECT_ADMIN_ROLES,
    SELF_ONLY_ACTIONS,
    TIMESHEET_COMPANY_ROLES,
    TIMESHEET_CROSS_ACTIONS,
    Action,
    roles_may,
    rule_for,
)
from app.domain.access.principal import Principal

CLEARANCE_RANK = {"low": 0, "medium": 1, "high": 2}

#: The statuses a project has while it may receive new time (ticket 27). Written
#: here rather than imported from the project module, for the reason the snapshot
#: builder spells out the clearance order: the kernel is the outside of the project
#: module's boundary, and a decision function that imported the thing it decides
#: about would be one refactor away from deciding with it.
PROJECT_ACTIVE_STATUS = "active"


class ResourceKind(StrEnum):
    """What kind of thing a filter describes."""

    DOCUMENT = "document"
    EMPLOYEE = "employee"
    DEPARTMENT = "department"
    POSITION = "position"
    ACCOUNT = "account"
    AUDIT_LOG = "audit_log"
    #: A project, as a thing that is managed and a thing that time is recorded
    #: against. Tasks are decided as this kind too, carrying their project's
    #: department and manager: a task has no reach of its own, and giving it a kind
    #: would mean a second copy of a rule whose answer is always "whatever its
    #: project says".
    PROJECT = "project"
    #: A weekly pattern, and a day nobody works (ticket 22). Two kinds rather than
    #: one because they are two tables and two authorities — they simply happen to
    #: share a rule ("HR and administration, and reading is published"), which is
    #: why neither is given a branch of its own below: the department and clearance
    #: path is the whole answer for both, and a branch would be a place for the two
    #: to drift apart later.
    SCHEDULE = "schedule"
    HOLIDAY = "holiday"
    #: A week of hours (ticket 28). Its own kind rather than `EMPLOYEE`, because the
    #: resource a timesheet decision carries is the *week's owner* and the audit
    #: trail should say "timesheet" rather than guess at a person. It takes the
    #: self-only branch below and nothing else, which is exactly what the ticket
    #: asks for: reading or writing somebody else's week is refused for every role.
    TIMESHEET = "timesheet"
    #: A kind of leave (ticket 25): the catalogue row that says whether a leave is
    #: paid, needs proof or spends the annual allowance. Its own kind because it is
    #: not a person's record and not a schedule — it is the organisation's list of
    #: what it offers, decided by catalogue and by role alone.
    LEAVE_TYPE = "leave_type"
    #: One line of a timesheet report (ticket 30): somebody's hours on somebody's
    #: project. Its own kind rather than `TIMESHEET`, and the reason is the rule:
    #: a *week* is reachable only by the person it belongs to, while a report line is
    #: reachable by three different people for three different reasons, and a kind
    #: that carried both rules would be one branch away from granting a manager the
    #: right to edit what they may only read.
    TIMESHEET_REPORT = "timesheet_report"


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
    #: The employee a project names as its manager (ticket 27), a fact the project
    #: module looks up from the row and the caller passes in.
    #:
    #: **Not `owner_employee_id`.** Managing a project is not owning it, and the two
    #: are not interchangeable anywhere the kernel already uses ownership: a manager
    #: is not a reason to return `IS_OWNER`, which is what the self-only actions
    #: grant, and a project manager who arrived at `can()` as an owner would inherit
    #: every future rule that reads ownership. One field, one meaning.
    manager_employee_id: UUID | None = None


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
    MANAGES_OWN_PROJECT = "manages_own_project"
    NOT_PROJECT_MANAGER = "not_project_manager"
    #: Somebody else's attendance, and the caller is neither their manager nor HR.
    #: Its own reason rather than `NOT_OWNER`, which is what the self-only branch
    #: says and would describe the wrong rule: the file was never the caller's to
    #: read by ownership in the first place.
    NOT_MANAGER_OF_SUBJECT = "not_manager_of_subject"
    #: A line of somebody's time, and the caller is neither their manager, nor the
    #: manager of the project it was booked to (ticket 30). Its own reason because
    #: the rule is a union of two reaches and a refusal has to say which one was
    #: missing: `NOT_MANAGER_OF_SUBJECT` alone would describe half of it.
    NOT_YOUR_TIMESHEET_SCOPE = "not_your_timesheet_scope"


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
        "personal_documents_via_department",
        "include_company_kb",
        "manager_employee_id",
        "reports_employee_ids",
        "statuses",
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
        personal_documents_via_department: bool = False,
        manager_employee_id: UUID | None = None,
        reports_employee_ids: frozenset[UUID] = frozenset(),
        statuses: frozenset[str] = frozenset(),
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
        #: True when a personal document its owner published to a department is in
        #: this caller's reach *through that department* (ticket 36). It is the second
        #: of the two things `visibility='department'` decides — the first is that the
        #: document is *stored* as shared — and the flag exists because the two
        #: consumers of a document spec want different widths of one rule:
        #:
        #: * the **document list** sets it, so a colleague sees the document they may
        #:   open on the shared screen;
        #: * the **retrieval path** clears it (`answer_filter_for`), because the
        #:   ticket's rule is that a personal document is recalled from the pool only
        #:   by the person who uploaded it: 「个人文档不进入公司知识库的检索池」.
        #:
        #: The permission is a property of the document's `visibility` column rather
        #: than of the caller, so a store reads that column too — which is why the
        #: column is a fact rather than a cache of this flag.
        self.personal_documents_via_department = personal_documents_via_department
        #: Projects are reached by having been *named their manager*, which is the
        #: one way to reach a row whose department is not yours. Carried as the
        #: caller's own employee id, so a store can write the clause in the same
        #: query; `None` means projects are not reached this way at all — which is
        #: true of every kind but this one.
        self.manager_employee_id = manager_employee_id
        #: The employees whose hours this caller reaches as their manager, carried as
        #: the *set* for the reason `department_ids` is: it is the same relationship
        #: the kernel's `MANAGER_OF_SUBJECT` clause tests, and a store that had to
        #: re-derive "who reports to this person" would be a second implementation of
        #: it. Empty means "nobody reports to this caller", never "no restriction".
        #:
        #: Not `own_employee_id`: owning your own week is a different reach with a
        #: different action (`timesheet.read_own`), and folding the two together is
        #: how a report quietly starts including rows the caller may not read.
        self.reports_employee_ids = reports_employee_ids
        #: The *only* statuses that count as reachable, as a membership test rather
        #: than a lower bound. Empty means the kind is not filtered by status.
        #:
        #: It is what makes `allow_all` safe to hand to administration for
        #: projects: `allow_all` there says "no department restriction", and an
        #: archived project has to stay out of a *recording* filter even for the
        #: person who may edit it. A bound reading "active or later" would let every
        #: status added to the project module in future leak in silently; a closed
        #: set leaks nothing until it is named here.
        self.statuses = statuses

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        if self.allow_all:
            return f"<FilterSpec {self.kind} allow_all statuses={sorted(self.statuses)}>"
        return (
            f"<FilterSpec {self.kind} departments={len(self.department_ids)} "
            f"clearance<={sorted(self.clearance_levels)} own={self.own_employee_id is not None} "
            f"manages={self.manager_employee_id is not None} "
            f"reports={len(self.reports_employee_ids)} "
            f"cross_dept={self.company_kb_cross_department} "
            f"shared_personal={self.personal_documents_via_department} "
            f"statuses={sorted(self.statuses)}>"
        )

    def only_my_personal_documents(self) -> "FilterSpec":
        """The same reach, narrowed to the caller's **own** personal documents (ticket 36).

        A *question* answers from a narrower pool than a list shows: 「个人文档不进入公司知识库
        的检索池」, 「只有提问者本人的个人文档可被召回」. So the retrieval path asks for this
        and the document list does not, and the difference is one named field rather than a
        second reading of §4.2.

        **A method here rather than a `dataclasses.replace` at the call site, and the
        reason is a defect this ticket found**: `FilterSpec` is a slots class with a
        private constructor and no dataclass decorator, so `replace()` raises
        `TypeError: replace() should be called on dataclass instances` — inside a request,
        as a 500. Building the narrowed spec where the token lives makes that mistake
        unrepresentable, and it keeps the "which fields survived" question answerable: the
        copy goes through `_cleared` only, so everything else is provably the same object's
        value.

        The result is a *strict subset* of the reach this spec describes when the field it
        clears was set, and is `self`'s identical twin when it was not. It can never widen:
        there is no argument and no branch that sets a field.
        """
        return self._cleared("personal_documents_via_department")

    def _cleared(self, *fields: str) -> "FilterSpec":
        """A copy with these boolean fields set to `False`, and nothing else touched.

        Private, because the only narrowing anyone may express is a named one above: a
        general "clear any field" would be a way to build a spec with a reach nobody
        intended, which is what the private constructor exists to prevent.
        """
        values = {
            name: getattr(self, name)
            for name in FilterSpec.__slots__
            if name not in fields
        }
        values.update(dict.fromkeys(fields, False))
        return FilterSpec(_token=_FILTER_TOKEN, **values)  # type: ignore[arg-type]


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

    # Projects have a resource rule, and it is not the department path below.
    # "A project manager manages their own project; administration and HR manage
    # all of them" is a statement about *who was named on the row*, and the rule
    # below decides it the other way round: it would allow any project in a
    # department the caller works in, which is every colleague's project. The rule
    # lives here rather than in the router because a route that tested
    # `project.manager_employee_id == principal.employee_id` itself would be the
    # second place permission is decided, and the first one to be forgotten.
    if resource.kind is ResourceKind.PROJECT:
        return _can_on_project(principal, action, resource)

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

    # Somebody else's hours (ticket 30), decided before the generic path for the
    # reason the three branches below are: a manager and a colleague share a
    # department, so "in my department" would be read as "mine to sum". It is its
    # own call rather than a fourth clause of `_can_on_record` because its rule is
    # not the same one: a project manager reaches a line of time on *their project*
    # without being anybody's manager.
    if action in TIMESHEET_CROSS_ACTIONS:
        return _can_on_timesheet_line(principal, action, resource)

    # Somebody else's personnel record (ticket 24, extended by ticket 25 to leave and
    # by ticket 26 to overtime), decided before the generic path can reach it. The path
    # below would answer a manager's read of a colleague with the department clause —
    # they share a department — and that is precisely the escalation the tickets refuse:
    # a managerial position reaches its own reports, not its team's records. The
    # company-wide remit, by contrast, does not depend on the row at all, which is why
    # it is a role check here and not a second resource clause.
    if (
        action in ATTENDANCE_CROSS_ACTIONS
        or action in LEAVE_CROSS_ACTIONS
        or action in OVERTIME_CROSS_ACTIONS
    ):
        return _can_on_record(principal, action, resource)

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


def _can_on_project(principal: Principal, action: Action, resource: Resource) -> Decision:
    """A project: who may read it, and who may change it.

    **Reading is the catalogue's answer.** A project catalogue is published — what is
    running, for which client, under whose management — so a role the action admits
    reads every project, and there is no resource clause to apply. The fields that
    would need one are a timesheet's, and a timesheet is not this table (ticket 28).

    **Changing is a fact about the row.** Either the caller was named the project's
    manager, or the caller holds a role with an organisation-wide remit
    (`PROJECT_ADMIN_ROLES`). Nothing else reaches it, which is why this does not fall
    through to the department path below: a colleague's project is in your
    department, and "in my department" must not be read as "mine to edit".

    A resource naming no manager is refused rather than allowed, for the reason the
    self-only branch gives: "we cannot tell that it is yours" is a refusal, and a
    decision function whose default on missing information is "yes" is how a
    filter-free query gets written. Administration and HR are above it, because
    their remit genuinely does not depend on the row.
    """
    if action in {Action.PROJECT_READ, Action.PROJECT_TASK_READ}:
        return Decision(True, (Reason.ROLE_PERMITS,), "the project catalogue is published")

    if bool(principal.roles & PROJECT_ADMIN_ROLES):
        return Decision(
            True, (Reason.IS_PRIVILEGED,), "administration and HR manage every project"
        )

    if (
        resource.manager_employee_id is not None
        and resource.manager_employee_id == principal.employee_id
    ):
        return Decision(True, (Reason.MANAGES_OWN_PROJECT,), "the principal manages this project")

    return Decision(
        False,
        (Reason.NOT_PROJECT_MANAGER,),
        f"{action} is for the project's own manager; manager="
        f"{resource.manager_employee_id or 'unset'}, caller={principal.employee_id}",
    )


def _can_on_timesheet_line(
    principal: Principal, action: Action, resource: Resource
) -> Decision:
    """A line of somebody's time: two reaches for a manager, one remit for HR.

    The ticket's sentence — 经理只能看自己下属的工时；项目经理能看自己项目的工时；人力资源可看全员
    — is a rule about the *row*, so it is decided here rather than by a query. The
    resource carries the two facts it needs: the employee whose hours they are
    (`owner_employee_id`) and the project's own manager (`manager_employee_id`).

    **A union, not a conjunction.** A manager reaches a line if the person reports to
    them *or* the project is theirs; a project's manager reads time recorded against
    it by somebody who does not report to them at all, which is what the ticket asks
    for and what a conjunction would refuse. The project clause matches a fact about
    the row rather than a role, which is the same reason `_can_on_project` is a
    branch: the same role manages one project and is refused another.

    **The department is deliberately absent.** A manager and a colleague share a
    department, and §4.1's 直属下属 is what the ticket means by 下属; letting the
    generic path answer would hand every manager their whole team's hours. The
    department is not a *narrower* reading of the rule either — a project's time is
    reachable through the project's manager whether or not the caller works there.

    **A row that names nobody is refused.** "We cannot tell that it is yours to read"
    is a refusal, not a permission, for the reason the self-only branch gives: a
    decision whose default on missing information is "yes" is how a filter-free query
    gets written.
    """
    if bool(principal.roles & TIMESHEET_COMPANY_ROLES):
        return Decision(
            True,
            (Reason.IS_PRIVILEGED,),
            f"{sorted(principal.roles & TIMESHEET_COMPANY_ROLES)} reaches the whole "
            "company's hours",
        )

    if (
        resource.owner_employee_id is not None
        and resource.owner_employee_id in principal.reports_employee_ids
    ):
        return Decision(
            True, (Reason.MANAGER_OF_SUBJECT,), "the hours belong to somebody who reports here"
        )

    if (
        resource.manager_employee_id is not None
        and resource.manager_employee_id == principal.employee_id
    ):
        return Decision(
            True, (Reason.MANAGES_OWN_PROJECT,), "the time was booked against this project"
        )

    return Decision(
        False,
        (Reason.NOT_YOUR_TIMESHEET_SCOPE,),
        f"{action} reaches your reports and your projects; employee="
        f"{resource.owner_employee_id or 'unset'}, project manager="
        f"{resource.manager_employee_id or 'unset'}, caller={principal.employee_id}",
    )


def _can_on_record(principal: Principal, action: Action, resource: Resource) -> Decision:
    """Somebody else's personnel record — their hours, their leave — two reaches, and
    nothing else.

    **The company's is a role.** The working-time record is a personnel record and §4.1
    gives HR the personnel file; the four-year obligation the Spanish rules impose is
    kept for exactly this reading, and a leave is part of the same file. Overtime is the
    one exception, and it is stated as its own set: §4.1 gives finance the monthly
    overtime export, because overtime pay is a payroll calculation, so
    `OVERTIME_COMPANY_ROLES` names two roles where `COMPANY_RECORD_ROLES` names one.
    Nothing about the row enters into either, which is why the check is a role and not a
    clause.

    **A manager's is their reports.** `principal.reports_employee_ids` is built from
    the assignments that name this person as the approver — the same relationship the
    approval route is resolved from — so "my report" means here what it means
    everywhere else, and it is what makes approving their leave possible at all. A
    resource that names nobody, or names somebody who does not report to the caller,
    is refused: "we cannot tell that they report to you" is a refusal, not a
    permission, for the reason the self-only branch gives.

    **The department is not part of this rule, deliberately.** A manager and a
    colleague share a department, and the generic path below would read that as
    permission; these actions exist because the tickets refuse that reading by name.
    """
    company_roles = (
        OVERTIME_COMPANY_ROLES if action in OVERTIME_CROSS_ACTIONS else COMPANY_RECORD_ROLES
    )
    if bool(principal.roles & company_roles):
        return Decision(
            True,
            (Reason.IS_PRIVILEGED,),
            f"{sorted(principal.roles & company_roles)} reaches the whole company's record",
        )

    if (
        resource.owner_employee_id is not None
        and resource.owner_employee_id in principal.reports_employee_ids
    ):
        return Decision(
            True, (Reason.MANAGER_OF_SUBJECT,), "the principal approves for this person"
        )

    return Decision(
        False,
        (Reason.NOT_MANAGER_OF_SUBJECT,),
        f"{action} reaches your own reports and nothing else; subject="
        f"{resource.owner_employee_id or 'unset'}, caller={principal.employee_id}",
    )


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
        #
        # `personal_documents_via_department` is set here and cleared on the
        # retrieval path (`domain/retrieval/filtering.py`). The *full* reach §4.2
        # describes — and what a list shows — includes a personal document its owner
        # published to a department. A question is narrower by the ticket's own rule:
        # 「只有提问者本人的个人文档可被召回」. Both are this one spec shape, so the
        # difference is a named field rather than a second implementation of §4.2.
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
            personal_documents_via_department=True,
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

    if kind is ResourceKind.PROJECT:
        # The projects time may be recorded against (ticket 27, consumed by the
        # timesheet in ticket 28). Three clauses, all of them conjunctive — this is
        # not a union of alternatives the way a document's four clauses are, so
        # every field of the spec must be applied:
        #
        # * `statuses` is `{active}`. A draft project is not running yet and an
        #   archived one is finished; neither accepts new time. It is carried even
        #   for administration, so the one field a store must not skip is the one
        #   that survives `allow_all`.
        # * `allow_all` is administration's and HR's, and it means "no department
        #   restriction" rather than "every row" — the status clause above is what
        #   keeps that honest.
        # * `department_ids` and `manager_employee_id` are the two ways a project is
        #   in reach, and for everybody else they are the whole answer:
        #   `department_id IN reachable OR manager_employee_id = me`. A store that
        #   wrote only the first would take a project away from the manager who
        #   runs it the moment it moved to another department.
        return FilterSpec(
            _token=_FILTER_TOKEN,
            kind=kind,
            allow_all=bool(principal.roles & PROJECT_ADMIN_ROLES),
            department_ids=principal.department_ids,
            clearance_levels=frozenset(CLEARANCE_RANK),
            own_employee_id=principal.employee_id,
            include_company_kb=False,
            manager_employee_id=principal.employee_id,
            statuses=frozenset({PROJECT_ACTIVE_STATUS}),
        )

    if kind is ResourceKind.TIMESHEET_REPORT:
        # The rows one report may state (ticket 30). A **union of two reaches**, which
        # is why it is its own branch instead of the project spec reused: a manager's
        # hours are reachable through the reporting relationship *or* through the
        # project they run, and a store that applied one of the two would either drop
        # a project manager's own projects or hand a manager the whole department.
        #
        # * `allow_all` is HR's, and it means exactly that: 工时全量 is a remit that
        #   does not depend on the row.
        # * `reports_employee_ids` is the relationship `MANAGER_OF_SUBJECT` tests, the
        #   same one the approval route is resolved from.
        # * `manager_employee_id` is "projects I manage", the same field the project
        #   spec carries, read here as a fact about the time's *project*.
        #
        # Everything else is empty on purpose. `department_ids` is not part of the
        # rule (see `_can_on_timesheet_line`), and leaving the field populated for a
        # store that applied it "just in case" is how the escalation gets written.
        return FilterSpec(
            _token=_FILTER_TOKEN,
            kind=kind,
            allow_all=bool(principal.roles & TIMESHEET_COMPANY_ROLES),
            department_ids=frozenset(),
            clearance_levels=frozenset(CLEARANCE_RANK),
            # The caller's *own* hours are deliberately not part of this reach: they are
            # `timesheet.read_own`, a different action with its own rule, and a report
            # that quietly included them would widen the day that rule changed. So this
            # field is left unset, which for a store means "ownership is not a clause
            # here" rather than "the caller has no id".
            own_employee_id=None,
            include_company_kb=False,
            manager_employee_id=principal.employee_id,
            reports_employee_ids=principal.reports_employee_ids,
        )

    # Structure and administration are organisation-wide for anyone whose role
    # passed the action check; the filter records that rather than pretending
    # otherwise. A week of hours (ticket 28) lands here too, and `allow_all` is
    # harmless for it for a reason worth stating: the three timesheet actions are
    # self-only, so the *resource* clause refuses every week but the caller's own
    # before any filter is consulted — there is no list endpoint that could leak
    # through a permissive spec. Ticket 30's report is the exception that proves the
    # rule is about the *kind*: it asks for `TIMESHEET_REPORT`, which is decided
    # above, because a report genuinely can list somebody else's hours.
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
    "PROJECT_ACTIVE_STATUS",
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
