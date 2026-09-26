"""The document access rule, exhaustively.

`docs/DESIGN.md` §4.2 states the rule as four clauses. This module restates those
clauses as an executable expectation and compares them against the kernel for
*every* combination of role, user clearance, document clearance, department
relation, document kind, ownership and explicit sharing — generated, not
enumerated by hand, because a hand-written matrix is a list of the cases somebody
thought of.

The expectation is written from the design text rather than from the kernel. If
both were written the same way it would only prove the kernel agrees with itself.
"""

from itertools import product
from uuid import uuid4

import pytest

from app.domain.access.kernel import Reason, Resource, ResourceKind, can
from app.domain.access.permissions import Action
from app.domain.access.principal import PRIVILEGED_ROLES, SYSTEM_ROLES, Principal

CLEARANCE_RANK = {"low": 0, "medium": 1, "high": 2}
CLEARANCES = ("low", "medium", "high")

MY_DEPARTMENT = uuid4()
OTHER_DEPARTMENT = uuid4()
MY_EMPLOYEE = uuid4()
OTHER_EMPLOYEE = uuid4()

#: The roles `docs/DESIGN.md` §4.2 names in its last clause. Written out here so
#: the test fails if the kernel's exception set is widened by accident.
EXCEPTION_ROLES = frozenset({"hr", "compliance"})

#: Every department relation a document can have to the caller.
DEPARTMENT_RELATIONS = ("mine", "other", "none")


def principal(roles: tuple[str, ...], clearance: str) -> Principal:
    return Principal(
        user_id=uuid4(),
        employee_id=MY_EMPLOYEE,
        username="ana",
        roles=frozenset(roles) | {"employee"},
        clearance_level=clearance,
        department_ids=frozenset({MY_DEPARTMENT}),
        primary_department_id=MY_DEPARTMENT,
        is_manager="manager" in roles,
        reports_employee_ids=frozenset(),
    )


def resource(
    *, department: str, document_clearance: str, is_company_kb: bool, owner: str, granted: bool
) -> Resource:
    return Resource(
        kind=ResourceKind.DOCUMENT,
        department_id={
            "mine": MY_DEPARTMENT,
            "other": OTHER_DEPARTMENT,
            "none": None,
        }[department],
        clearance=document_clearance,
        owner_employee_id=MY_EMPLOYEE if owner == "mine" else OTHER_EMPLOYEE,
        is_company_kb=is_company_kb,
        explicit_grant=granted,
    )


def design_says(
    *,
    roles: frozenset[str],
    user_clearance: str,
    document_clearance: str,
    department: str,
    is_company_kb: bool,
    is_owner: bool,
    granted: bool,
) -> bool:
    """`docs/DESIGN.md` §4.2, clause by clause.

    allow(user, doc) =
        doc.owner_employee_id == user.employee_id
     OR (doc.is_company_kb AND clearance_ok AND dept_ok)
     OR explicit_grant(user, doc)
     OR user.has_role('hr') OR user.has_role('compliance')

    with `clearance_ok` applied to the share clause as well (the note under the
    formula: "an explicit share cannot break the ceiling") and to the exception
    clause, which ticket 12 states as a *cross-department* exception. The
    exception's ceiling is the one interpretation in this file that the design's
    formula leaves open; it is recorded in the ticket.
    """
    if is_owner:
        return True

    clearance_ok = CLEARANCE_RANK[document_clearance] <= CLEARANCE_RANK[user_clearance]
    dept_ok = department == "mine"

    if is_company_kb and clearance_ok and dept_ok:
        return True
    if granted and clearance_ok:
        return True
    if is_company_kb and (roles & EXCEPTION_ROLES) and clearance_ok:
        return True
    return False


def test_the_document_matrix_is_exhaustive() -> None:
    """Role × department × clearance, and the flags that cross them.

    Generated from the same inputs the assertions below use, so a change that
    quietly stops covering a dimension fails here rather than passing quietly.
    """
    combinations = product(
        sorted(SYSTEM_ROLES),
        CLEARANCES,
        CLEARANCES,
        DEPARTMENT_RELATIONS,
        (False, True),
        ("mine", "other"),
        (False, True),
    )

    checked = 0
    mismatches: list[str] = []
    for roles, user_clearance, doc_clearance, department, kb, owner, granted in combinations:
        checked += 1
        expected = design_says(
            roles=frozenset({roles}),
            user_clearance=user_clearance,
            document_clearance=doc_clearance,
            department=department,
            is_company_kb=kb,
            is_owner=owner == "mine",
            granted=granted,
        )
        decision = can(
            principal((roles,), user_clearance),
            Action.DOCUMENT_READ,
            resource(
                department=department,
                document_clearance=doc_clearance,
                is_company_kb=kb,
                owner=owner,
                granted=granted,
            ),
        )
        if decision.allowed is not expected:
            mismatches.append(
                f"role={roles} user={user_clearance} doc={doc_clearance} "
                f"dept={department} kb={kb} owner={owner} granted={granted}: "
                f"expected {expected}, got {decision.allowed} ({decision.detail})"
            )

    assert checked == len(SYSTEM_ROLES) * 3 * 3 * 3 * 2 * 2 * 2
    assert mismatches == [], "\n".join(mismatches[:20])


# --- the requirements, one test each ---------------------------------------


def test_a_share_never_lifts_the_ceiling() -> None:
    """The ticket's explicit requirement: naming somebody is not a clearance.

    A personal document, shared with them by name, classified above what they
    may read. They still cannot read it.
    """
    decision = can(
        principal(("employee",), "low"),
        Action.DOCUMENT_READ,
        resource(
            department="none",
            document_clearance="high",
            is_company_kb=False,
            owner="other",
            granted=True,
        ),
    )

    assert decision.denied
    assert Reason.CLEARANCE_TOO_LOW in decision.reasons


def test_a_share_does_grant_a_personal_document_within_the_ceiling() -> None:
    decision = can(
        principal(("employee",), "medium"),
        Action.DOCUMENT_READ,
        resource(
            department="none",
            document_clearance="medium",
            is_company_kb=False,
            owner="other",
            granted=True,
        ),
    )

    assert decision.allowed
    assert Reason.EXPLICIT_GRANT in decision.reasons


def test_someone_elses_unshared_personal_document_is_refused() -> None:
    """The clause the generic department-and-clearance path would have allowed:
    no department, no clearance, so nothing to check — and nothing to allow."""
    decision = can(
        principal(("employee",), "high"),
        Action.DOCUMENT_READ,
        resource(
            department="none",
            document_clearance="low",
            is_company_kb=False,
            owner="other",
            granted=False,
        ),
    )

    assert decision.denied
    assert Reason.NOT_OWNER in decision.reasons
    assert Reason.NOT_SHARED in decision.reasons


def test_your_own_document_is_yours_whatever_it_is_classified() -> None:
    """§4.2's first clause carries no conditions. Lowering your own access to
    your own upload would need a rule, and there is none."""
    decision = can(
        principal(("employee",), "low"),
        Action.DOCUMENT_READ,
        resource(
            department="other",
            document_clearance="high",
            is_company_kb=True,
            owner="mine",
            granted=False,
        ),
    )

    assert decision.allowed
    assert Reason.IS_OWNER in decision.reasons


@pytest.mark.parametrize(
    ("roles", "expected"),
    [
        (("hr",), True),
        (("compliance",), True),
        (("finance",), False),
        (("admin",), False),
        (("it",), False),
        (("manager",), False),
        (("employee",), False),
    ],
)
def test_only_the_named_roles_cross_departments(roles: tuple[str, ...], expected: bool) -> None:
    """`admin` is not in the list, and neither is `finance`. An administrator
    configured the clearance; that is not a reason to read above it."""
    decision = can(
        principal(roles, "medium"),
        Action.DOCUMENT_READ,
        resource(
            department="other",
            document_clearance="low",
            is_company_kb=True,
            owner="other",
            granted=False,
        ),
    )

    assert decision.allowed is expected, decision.detail


def test_the_exception_is_for_company_documents_only() -> None:
    decision = can(
        principal(("hr",), "high"),
        Action.DOCUMENT_READ,
        resource(
            department="other",
            document_clearance="low",
            is_company_kb=False,
            owner="other",
            granted=False,
        ),
    )

    assert decision.denied


def test_no_privileged_role_lifts_the_ceiling() -> None:
    """Every privileged role, on a company document it cannot otherwise reach."""
    for role in sorted(PRIVILEGED_ROLES):
        decision = can(
            principal((role,), "low"),
            Action.DOCUMENT_READ,
            resource(
                department="other",
                document_clearance="high",
                is_company_kb=True,
                owner="other",
                granted=True,
            ),
        )
        assert decision.denied, role
        assert Reason.CLEARANCE_TOO_LOW in decision.reasons, role
