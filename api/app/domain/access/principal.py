"""The principal: a permission snapshot for one request.

Built from the account, its employee, and that employee's active assignments.
Everything a permission decision needs is here, so `can()` never has to query —
which is what makes it a pure function and therefore exhaustively testable.

The snapshot is cached in Redis and invalidated by the writes that change it, not
by a TTL. A permission that lags behind a role change is a security hole with a
timer on it.
"""

from dataclasses import dataclass, field
from uuid import UUID

#: Roles the system recognises. Fixed by ticket 08: a role outside this set is a
#: data error, not an extension point.
SYSTEM_ROLES = frozenset({"admin", "hr", "finance", "it", "compliance", "manager", "employee"})

#: Roles that may read withheld employee fields. Mirrors the employee module's
#: rule; both are stated here so the two cannot drift.
PRIVILEGED_ROLES = frozenset({"hr", "finance", "compliance"})


@dataclass(slots=True, frozen=True)
class Principal:
    """Who is asking, and what they may reach.

    Frozen so a handler cannot widen its own permissions mid-request, and so a
    cached snapshot cannot be mutated by whoever reads it first.
    """

    user_id: UUID
    employee_id: UUID
    username: str
    roles: frozenset[str] = frozenset()
    #: Highest clearance held across the person's positions.
    clearance_level: str = "low"
    #: Every department reachable through an active assignment, descendants
    #: included. This is what "a colleague in my department" is tested against.
    department_ids: frozenset[UUID] = frozenset()
    #: The department of the primary position, used for ownership defaults.
    primary_department_id: UUID | None = None
    #: True when any held position is marked managerial.
    is_manager: bool = False
    #: Employees this person approves for, from the assignments that name them.
    reports_employee_ids: frozenset[UUID] = frozenset()
    #: Bumped whenever the snapshot's inputs change, so a cache entry can be
    #: recognised as stale without re-reading everything.
    version: int = 0
    snapshot_reason: tuple[str, ...] = field(default=())

    @property
    def is_privileged(self) -> bool:
        return bool(self.roles & PRIVILEGED_ROLES)

    def has_role(self, *roles: str) -> bool:
        return bool(self.roles & set(roles))

    def covers_department(self, department_id: UUID | None) -> bool:
        return department_id is not None and department_id in self.department_ids
