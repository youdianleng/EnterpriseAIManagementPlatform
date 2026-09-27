"""Who is left without an approver once somebody leaves.

A termination takes a person out of every list of people — the directory, the
department headcount, the pool of possible approvers — and one of those lists
matters more than the others: an approval route that resolves to a leaver is a
route nobody can walk. The document is filed, sits at its first level and is never
decided, because the person who was to decide it cannot sign in.

**Refused, not reassigned.** When a route resolves to a terminated approver, this
module refuses the document and names the people HR has to fix. The alternative —
walking up to the approver's own approver — was rejected for a company of a hundred
people with two approval levels, for three reasons:

* **Substitution is not the engine's to make.** The engine already declines to
  reassign: when a route resolves to the requester, the first level is recorded as
  `skipped` with the reason rather than handed to somebody else, "because the route
  named the requester's manager and nobody else is entitled to that step". A
  leaver's manager is no more entitled to the step, and the person above the
  leaver may be in another department, or be the requester.
* **The chain is shallow and the second level is not a person.** With two levels,
  "the approver's own approver" lands on a department manager or on HR. HR cannot
  substitute for the first level — that is a role, not a route — so in the common
  case the fallback either changes nothing or resolves back to the leaver's own
  manager.
* **A refusal reaches somebody; a silent fallback does not.** A fallback hides a
  configuration nobody repaired, and HR only discovers it when a decision is late.
  A refusal at submission is a problem while the requester can still act on it,
  which is the same reason an unresolvable route and an unavailable HR approver are
  refused when a document is filed rather than when the first level approves.

The second half is not optional: a refusal that names nobody is a dead end for HR.
`ApproverGap` is therefore the *report* as well as the refusal's content — the
applier job prints it on every pass, and the same rows are what the refusal names.
It is defined in `domain/employee/approver_gap.py` rather than here, and that is a
structural decision rather than tidiness: the employee repository answers "who is
now pointing at a leaver" and this module decides what to do about it, so the shape
they exchange must be importable by both. Defined here, it would make the employee
package import the personnel package, which imports the employee package back — a
cycle Python resolves by handing one of them a half-built module.
"""

from typing import Protocol

from app.domain.employee.approver_gap import ApproverGap


class ApproverCoverageRepository(Protocol):
    """The one read this module needs: the routes that no longer resolve.

    Satisfied by `domain/employee/repository.EmployeeRepository`, which is what the
    personnel service already holds as its directory — a route is a fact about
    people and their positions, not about documents, so it is asked there rather
    than through a second repository invented for one query. Named here as well so
    the shape this rule depends on is readable next to the rule.
    """

    async def terminated_approvers(self) -> list[ApproverGap]: ...


#: How many gaps a refusal spells out before it stops naming them. The refusal is
#: a sentence somebody reads to start working; the full list is the job report's,
#: which is where a hundred-row answer belongs.
NAMES_IN_REFUSAL = 10


def describe(gaps: list[ApproverGap]) -> str:
    """The refusal's detail: who to reassign, not merely that something is wrong.

    Employee, approver and where the approver was configured, because those are
    the three facts the fix needs and looking each of them up separately is how a
    refusal turns into a support ticket.
    """
    shown = gaps[:NAMES_IN_REFUSAL]
    parts = [
        f"{gap.employee_name} -> {gap.approver_name} "
        f"({'position' if gap.named_on_position else gap.department_code})"
        for gap in shown
    ]
    remainder = len(gaps) - len(shown)
    if remainder > 0:
        parts.append(f"and {remainder} more")
    return "; ".join(parts)


__all__ = [
    "NAMES_IN_REFUSAL",
    "ApproverCoverageRepository",
    "ApproverGap",
    "describe",
]