"""The authorization kernel.

The kernel is a pure function of a principal, an action and a resource, so these
tests need no database and no fixtures — which is exactly why every rule can be
enumerated rather than sampled.

Two properties get special attention:

* `FilterSpec` has no public constructor, so "forgot to filter" is impossible
  rather than a review concern.
* There is no superuser bypass. The matrix below asserts that an administrator is
  refused where the catalogue says so, because a hidden bypass is the failure
  mode the whole module exists to remove.
"""

from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest

from app.domain.access.kernel import (
    CLEARANCE_RANK,
    Decision,
    FilterSpec,
    Reason,
    Resource,
    ResourceKind,
    can,
    department_scope_ids,
    filter_for,
)
from app.domain.access.permissions import RULES, Action, roles_may
from app.domain.access.principal import PRIVILEGED_ROLES, Principal

OTHER_DEPARTMENT = uuid4()
MY_DEPARTMENT = uuid4()
MY_EMPLOYEE = uuid4()
OTHER_EMPLOYEE = uuid4()


def principal(
    *,
    roles=("employee",),
    clearance="low",
    departments=(MY_DEPARTMENT,),
    reports=(),
    employee_id=None,
) -> Principal:
    return Principal(
        user_id=uuid4(),
        employee_id=employee_id or MY_EMPLOYEE,
        username="ana",
        roles=frozenset(roles),
        clearance_level=clearance,
        department_ids=frozenset(departments),
        primary_department_id=MY_DEPARTMENT if departments else None,
        is_manager="manager" in roles,
        reports_employee_ids=frozenset(reports),
    )


# --- the catalogue ---------------------------------------------------------


def test_every_action_has_a_rule() -> None:
    """A missing rule would make `can` refuse an action nobody can perform,
    which is a silent feature outage rather than a visible error."""
    missing = [action for action in Action if action not in RULES]
    assert missing == []


def test_no_rule_names_a_role_outside_the_system_set() -> None:
    from app.domain.access.principal import SYSTEM_ROLES

    for action, rule in RULES.items():
        unknown = rule.roles - SYSTEM_ROLES
        assert unknown == set(), f"{action} names unknown roles: {unknown}"


def test_no_action_is_both_public_and_role_restricted() -> None:
    for action, rule in RULES.items():
        if rule.public:
            assert rule.roles == frozenset(), f"{action} is public and restricted"


def test_the_clearance_order_matches_the_kernel() -> None:
    """The snapshot module restates the ordering to avoid a circular import; the
    two must not drift, in either direction.

    Both directions are checked because a rank can be read backwards without
    looking wrong: the snapshot turns a rank back into a name, and an off-by-one
    there hands "low" to the people who should see the most.
    """
    from app.domain.access.snapshot import _CLEARANCE_BY_RANK

    highest_first = tuple(
        sorted(CLEARANCE_RANK, key=lambda name: CLEARANCE_RANK[name], reverse=True)
    )
    assert _CLEARANCE_BY_RANK == highest_first


# --- the decision ----------------------------------------------------------


def test_an_unknown_action_is_refused() -> None:
    """A default of "allow" for something nobody catalogued would mean the
    catalogue silently stopped being the answer."""
    decision = can(principal(), "not.an.action")  # type: ignore[arg-type]

    assert decision.denied
    assert decision.primary_reason is Reason.UNKNOWN_ACTION


def test_a_role_that_permits_the_action_is_allowed() -> None:
    decision = can(principal(roles=("hr",)), Action.EMPLOYEE_MANAGE)

    assert decision.allowed
    assert decision.primary_reason is Reason.ROLE_PERMITS


def test_a_role_that_does_not_permit_the_action_is_refused() -> None:
    decision = can(principal(roles=("employee",)), Action.EMPLOYEE_MANAGE)

    assert decision.denied
    assert decision.primary_reason is Reason.ROLE_LACKS_PERMISSION


def test_a_decision_carries_a_readable_detail_for_the_log() -> None:
    """The audit trail needs the rule that fired, not just the outcome."""
    decision = can(principal(roles=("employee",)), Action.ACCOUNT_MANAGE)

    assert decision.detail
    assert "ACCOUNT_MANAGE" in decision.detail or "account.manage" in decision.detail


def test_as_audit_fields_is_serialisable() -> None:
    decision = can(principal(roles=("hr",)), Action.EMPLOYEE_MANAGE)

    fields = decision.as_audit_fields()
    assert fields["allowed"] is True
    assert fields["reasons"] == ["role_permits"]


def test_a_refusal_records_every_reason_it_considered() -> None:
    decision = can(
        principal(roles=("employee",), clearance="low"),
        Action.DOCUMENT_READ,
        Resource(ResourceKind.DOCUMENT, clearance="high"),
    )

    assert decision.denied
    assert Reason.CLEARANCE_TOO_LOW in decision.reasons


# --- ownership -------------------------------------------------------------


def test_the_owner_reads_their_own_employee_record() -> None:
    decision = can(
        principal(),
        Action.EMPLOYEE_READ,
        Resource(ResourceKind.EMPLOYEE, owner_employee_id=MY_EMPLOYEE),
    )

    assert decision.allowed
    assert Reason.IS_OWNER in decision.reasons


def test_ownership_beats_a_department_the_caller_cannot_reach() -> None:
    """Your own record is yours whatever department it sits in."""
    decision = can(
        principal(departments=set()),
        Action.EMPLOYEE_READ,
        Resource(ResourceKind.EMPLOYEE, owner_employee_id=MY_EMPLOYEE),
    )

    assert decision.allowed


def test_someone_elses_record_in_another_department_is_refused() -> None:
    decision = can(
        principal(),
        Action.EMPLOYEE_READ,
        Resource(
            ResourceKind.EMPLOYEE,
            department_id=OTHER_DEPARTMENT,
            owner_employee_id=OTHER_EMPLOYEE,
        ),
    )

    assert decision.denied
    assert decision.primary_reason is Reason.DEPARTMENT_NOT_REACHABLE


# --- managers --------------------------------------------------------------


def test_a_manager_reaches_their_reports_across_departments() -> None:
    """Approving someone in another team has to be possible."""
    decision = can(
        principal(roles=("employee", "manager"), reports=(OTHER_EMPLOYEE,)),
        Action.EMPLOYEE_READ,
        Resource(
            ResourceKind.EMPLOYEE,
            department_id=OTHER_DEPARTMENT,
            owner_employee_id=OTHER_EMPLOYEE,
        ),
    )

    assert decision.allowed
    assert Reason.MANAGER_OF_SUBJECT in decision.reasons


def test_a_manager_does_not_reach_someone_who_is_not_their_report() -> None:
    decision = can(
        principal(roles=("employee", "manager"), reports=(uuid4(),)),
        Action.EMPLOYEE_READ,
        Resource(
            ResourceKind.EMPLOYEE,
            department_id=OTHER_DEPARTMENT,
            owner_employee_id=OTHER_EMPLOYEE,
        ),
    )

    assert decision.denied


# --- clearance -------------------------------------------------------------


@pytest.mark.parametrize(
    ("clearance", "document_clearance", "allowed"),
    [
        ("low", "low", True),
        ("low", "medium", False),
        ("low", "high", False),
        ("medium", "low", True),
        ("medium", "medium", True),
        ("medium", "high", False),
        ("high", "high", True),
    ],
)
def test_clearance_is_an_upper_bound(
    clearance: str, document_clearance: str, allowed: bool
) -> None:
    decision = can(
        principal(clearance=clearance),
        Action.DOCUMENT_READ,
        Resource(
            ResourceKind.DOCUMENT,
            department_id=MY_DEPARTMENT,
            clearance=document_clearance,
        ),
    )

    assert decision.allowed is allowed


@pytest.mark.parametrize("role", sorted(PRIVILEGED_ROLES))
def test_a_privileged_role_reaches_every_clearance(role: str) -> None:
    decision = can(
        principal(roles=(role,), clearance="low"),
        Action.DOCUMENT_READ,
        Resource(ResourceKind.DOCUMENT, department_id=OTHER_DEPARTMENT, clearance="high"),
    )

    assert decision.allowed
    assert Reason.IS_PRIVILEGED in decision.reasons


def test_read_withheld_is_decided_by_role_alone() -> None:
    """There is no resource that makes an address acceptable for another role."""
    allowed = can(principal(roles=("hr",)), Action.EMPLOYEE_READ_WITHHELD)
    refused = can(
        principal(roles=("admin",)),
        Action.EMPLOYEE_READ_WITHHELD,
        Resource(ResourceKind.EMPLOYEE, owner_employee_id=OTHER_EMPLOYEE),
    )

    assert allowed.allowed
    assert refused.denied
    assert refused.primary_reason is Reason.PRIVILEGED_ROLE_REQUIRED


# --- no hidden bypass ------------------------------------------------------


#: Every role against every action. Expected to match the catalogue exactly, so a
#: rule change that accidentally widens access fails here rather than in review.
@pytest.mark.parametrize("action", sorted(RULES, key=str))
def test_the_matrix_matches_the_catalogue(action: Action) -> None:
    for role in ("admin", "hr", "finance", "it", "compliance", "manager", "employee"):
        held = frozenset({role})
        role_permits = roles_may(action, held)

        if action is Action.EMPLOYEE_READ_WITHHELD:
            # Reading withheld fields is governed by privilege alone: the
            # catalogue's role list does not apply.
            expected = bool(held & PRIVILEGED_ROLES)
        else:
            expected = role_permits

        decision = can(principal(roles=(role,)), action)
        assert decision.allowed is expected, f"{role} / {action}"


def test_an_administrator_is_not_a_superuser() -> None:
    """The ticket requires that an administrator is bound by the same function.

    Specifically: administration does not read withheld employee fields, because
    the catalogue says HR, finance and compliance do.
    """
    decision = can(principal(roles=("admin",)), Action.EMPLOYEE_READ_WITHHELD)

    assert decision.denied


def test_an_empty_role_set_is_refused_everything_but_public_actions() -> None:
    for action, rule in RULES.items():
        decision = can(principal(roles=()), action)
        assert decision.allowed is rule.public, action


# --- the filter ------------------------------------------------------------


def test_a_filter_spec_cannot_be_constructed_by_a_caller() -> None:
    """The guarantee that makes "forgot to filter" impossible."""
    with pytest.raises(TypeError) as excinfo:
        FilterSpec(
            kind=ResourceKind.DOCUMENT,
            allow_all=True,
            department_ids=frozenset(),
            clearance_levels=frozenset(CLEARANCE_RANK),
            own_employee_id=None,
            include_company_kb=True,
        )

    assert "no public constructor" in str(excinfo.value)


def test_a_caller_cannot_forge_the_internal_token() -> None:
    """Passing something that looks like the token still fails."""
    with pytest.raises(TypeError):
        FilterSpec(
            _token=object(),
            kind=ResourceKind.DOCUMENT,
            allow_all=True,
            department_ids=frozenset(),
            clearance_levels=frozenset(),
            own_employee_id=None,
            include_company_kb=True,
        )


def test_an_ordinary_principal_gets_their_departments_and_clearance() -> None:
    spec = filter_for(principal(clearance="medium"), ResourceKind.DOCUMENT)

    assert spec.allow_all is False
    assert spec.department_ids == frozenset({MY_DEPARTMENT})
    assert spec.clearance_levels == frozenset({"low", "medium"})
    assert spec.own_employee_id == MY_EMPLOYEE


def test_a_privileged_principal_gets_an_unrestricted_filter() -> None:
    spec = filter_for(principal(roles=("hr",), clearance="low"), ResourceKind.DOCUMENT)

    assert spec.allow_all is True
    assert spec.clearance_levels == frozenset(CLEARANCE_RANK)


def test_the_document_filter_keeps_personal_uploads_out_of_the_company_pool() -> None:
    spec = filter_for(principal(), ResourceKind.DOCUMENT)

    assert spec.include_company_kb is True
    # The owner id is carried separately so an owner's own uploads can still be
    # matched without widening the shared pool.
    assert spec.own_employee_id is not None


def test_department_scope_distinguishes_open_from_empty() -> None:
    """"No predicate" and "match nothing" are different answers, and a bare empty
    set would be read the wrong way round."""
    assert department_scope_ids(filter_for(principal(roles=("hr",)), ResourceKind.DOCUMENT)) is None
    assert department_scope_ids(
        filter_for(principal(departments=set()), ResourceKind.DOCUMENT)
    ) == frozenset()


def test_filter_spec_repr_does_not_leak_department_ids() -> None:
    """A spec may end up in a log line; the ids are not useful there."""
    spec = filter_for(principal(), ResourceKind.DOCUMENT)

    assert str(MY_DEPARTMENT) not in repr(spec)


def test_decisions_are_immutable() -> None:
    """A handler must not be able to widen its own permissions mid-request."""
    decision = Decision(True, (Reason.ROLE_PERMITS,))

    with pytest.raises(FrozenInstanceError):
        decision.allowed = False  # type: ignore[misc]


def test_principals_are_immutable() -> None:
    """Same reason: a cached snapshot is shared, so nobody may edit it in place."""
    snapshot = principal()

    with pytest.raises(FrozenInstanceError):
        snapshot.roles = frozenset({"admin"})  # type: ignore[misc]
