"""One employee's approval route, and whether it still ends at somebody.

A value object in a module of its own, and that is a structural decision rather
than tidiness: the employee repository answers "who is now pointing at a leaver"
and the personnel module decides what to do about it, so the shape they exchange
has to be importable by both. It lives here instead of in either of them because a
dataclass in `personnel` makes the employee package import the personnel package,
which imports the employee package back — a cycle Python resolves by handing one of
them a half-built module.
"""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ApproverGap:
    """One employee whose approval route now ends at somebody who has left.

    Carries the names as well as the ids: the row exists to be read by a person,
    and "employee 8f6d… reports to employee 2b41…" is not something HR can act on
    without a second lookup.
    """

    employee_id: UUID
    employee_name: str
    approver_employee_id: UUID
    approver_name: str
    department_code: str
    department_name: str
    #: True when the position itself named the approver, false when the department
    #: did. Which of the two to change is the first thing HR has to know.
    named_on_position: bool


__all__ = ["ApproverGap"]
