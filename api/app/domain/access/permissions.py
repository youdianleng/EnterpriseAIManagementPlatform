"""The action catalogue.

`can()` is the only place a permission decision is made, and this module is the
only place the *rules* for it live. One table is easier to audit than a hundred
`if role == "hr"` branches, and it is what makes "which roles may do X" a
question with a readable answer.

Actions are named `<domain>.<verb>`. A missing action is a bug, not a default:
`can()` refuses an unknown action rather than falling through to allow.
"""

from dataclasses import dataclass
from enum import StrEnum


class Action(StrEnum):
    # Organisation structure.
    DEPARTMENT_READ = "department.read"
    DEPARTMENT_MANAGE = "department.manage"
    POSITION_READ = "position.read"
    POSITION_MANAGE = "position.manage"

    # People. Two list actions rather than one, because they answer different
    # questions and the design treats them differently: the contact list is
    # name-and-position for everyone, while the full directory carries contact
    # details and is administrative.
    EMPLOYEE_DIRECTORY = "employee.directory"
    EMPLOYEE_LIST = "employee.list"
    EMPLOYEE_READ = "employee.read"
    EMPLOYEE_READ_OWN = "employee.read_own"
    EMPLOYEE_MANAGE = "employee.manage"
    EMPLOYEE_READ_WITHHELD = "employee.read_withheld"

    # Accounts.
    ACCOUNT_LIST = "account.list"
    ACCOUNT_MANAGE = "account.manage"

    # Session administration.
    SESSION_READ_OWN = "session.read_own"

    # Documents and the knowledge base. Fleshed out in tickets 12 and 31; present
    # here so the document module has an action to ask about from the start.
    DOCUMENT_READ = "document.read"
    DOCUMENT_LIST = "document.list"
    DOCUMENT_UPLOAD = "document.upload"
    DOCUMENT_MANAGE = "document.manage"
    DOCUMENT_SET_CLEARANCE = "document.set_clearance"


@dataclass(frozen=True, slots=True)
class ActionRule:
    """Which roles may perform an action, and whether it is public.

    `public` exists so that opening an endpoint is a deliberate, reviewable act.
    An endpoint nobody thought about is closed, because the default is to require
    a role.
    """

    roles: frozenset[str] = frozenset()
    public: bool = False
    description: str = ""


#: The catalogue. Reading is broadly shared inside the organisation; changing
#: anything is narrow.
#:
#: Note what is absent: no role bypasses `can()`. An administrator is a role like
#: any other, listed explicitly — there is no "superuser" branch anywhere, because
#: a hidden bypass is the failure mode this whole module exists to remove.
RULES: dict[Action, ActionRule] = {
    Action.DEPARTMENT_READ: ActionRule(
        roles=frozenset({"admin", "hr", "finance", "it", "compliance", "employee"}),
        description="Everyone may see the organisation tree.",
    ),
    Action.DEPARTMENT_MANAGE: ActionRule(
        roles=frozenset({"admin", "hr"}),
        description="Structure changes are HR and administration.",
    ),
    Action.POSITION_READ: ActionRule(
        roles=frozenset({"admin", "hr", "finance", "it", "compliance", "employee"}),
        description="The position catalogue is visible to everyone.",
    ),
    Action.POSITION_MANAGE: ActionRule(
        roles=frozenset({"admin", "hr"}),
        description="Maintaining positions is HR and administration.",
    ),
    Action.EMPLOYEE_DIRECTORY: ActionRule(
        roles=frozenset({"admin", "hr", "finance", "it", "compliance", "manager", "employee"}),
        description=(
            "The contact list: name, position and — for colleagues — email. "
            "Everyone may see who works here; the projection decides what each "
            "row carries."
        ),
    ),
    Action.EMPLOYEE_LIST: ActionRule(
        roles=frozenset({"admin", "hr", "finance", "it", "compliance"}),
        description="The directory queried as data, rather than browsed as a list.",
    ),
    Action.EMPLOYEE_READ: ActionRule(
        roles=frozenset({"admin", "hr", "finance", "it", "compliance", "employee"}),
        description="Reading a profile; what is returned depends on the relationship.",
    ),
    Action.EMPLOYEE_READ_OWN: ActionRule(
        roles=frozenset({"employee"}),
        description="Always allowed for the person themselves.",
    ),
    Action.EMPLOYEE_MANAGE: ActionRule(
        roles=frozenset({"admin", "hr"}),
        description="Creating and correcting records.",
    ),
    Action.EMPLOYEE_READ_WITHHELD: ActionRule(
        roles=frozenset({"hr", "finance", "compliance"}),
        description="Address, staff number and emergency contact. Not administration.",
    ),
    Action.ACCOUNT_LIST: ActionRule(
        roles=frozenset({"admin"}),
        description="Accounts are an administrative concern, not a personnel one.",
    ),
    Action.ACCOUNT_MANAGE: ActionRule(
        roles=frozenset({"admin"}),
        description="Creating, disabling and resetting accounts.",
    ),
    Action.SESSION_READ_OWN: ActionRule(
        roles=frozenset({"admin", "hr", "finance", "it", "compliance", "employee"}),
        description="Anyone may inspect their own session.",
    ),
    Action.DOCUMENT_READ: ActionRule(
        roles=frozenset({"admin", "hr", "finance", "it", "compliance", "employee"}),
        description="Reading a document; clearance and department decide which ones.",
    ),
    Action.DOCUMENT_LIST: ActionRule(
        roles=frozenset({"admin", "hr", "finance", "it", "compliance", "employee"}),
    ),
    Action.DOCUMENT_UPLOAD: ActionRule(
        roles=frozenset({"admin", "hr", "finance", "compliance", "employee"}),
        description="Anyone may upload, including personal documents.",
    ),
    Action.DOCUMENT_MANAGE: ActionRule(
        roles=frozenset({"admin", "hr"}),
        description="Company knowledge base content.",
    ),
    Action.DOCUMENT_SET_CLEARANCE: ActionRule(
        roles=frozenset({"admin", "hr"}),
        description="Only these may classify a company document.",
    ),
}


def rule_for(action: Action) -> ActionRule:
    return RULES[action]


#: Roles that may read *company* documents outside their own departments
#: (`docs/DESIGN.md` §4.2, last clause).
#:
#: Listed explicitly rather than derived from "is privileged", because the two
#: sets are not the same: finance is privileged for payroll and is not part of
#: this exception. The exception is about departments only — it does not lift the
#: clearance ceiling, which is the one condition no role escapes for documents.
DOCUMENT_CROSS_DEPARTMENT_ROLES = frozenset({"hr", "compliance"})


def roles_may(action: Action, roles: frozenset[str]) -> bool:
    """Role-level check, before any resource nuance.

    Kept separate from `can()` so the resource-aware decision can be read as
    "role permits it, *and* this particular row is reachable".
    """
    rule = RULES[action]
    if rule.public:
        return True
    return bool(roles & rule.roles)
