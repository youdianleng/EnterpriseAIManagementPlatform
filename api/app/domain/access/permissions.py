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

    # Roles. Reading the catalogue is open to everyone signed in; changing who
    # holds what is administration, and nothing else.
    ROLE_READ = "role.read"
    ROLE_MANAGE = "role.manage"

    # Compliance. Reading the audit trail is its own permission rather than a
    # property of being senior: an administrator configures the system, and the
    # reader who checks what they did must not be the same person.
    AUDIT_READ = "audit.read"

    # Notifications. Your own centre, and only your own: there is no action for
    # reading somebody else's, because there is no endpoint for it. Its own action
    # rather than "employee.read_own", which says something about a personnel
    # record and nothing about a notification.
    NOTIFICATION_READ_OWN = "notification.read_own"

    # Attendance. Self-service, and only self-service: you punch your own clock and
    # you read your own record. Two actions rather than one because they are two
    # acts — recording your time, and reading it — and because a manager reading a
    # report's day, or HR reading the company's, must arrive as a *new* action with
    # its own resource rule (ticket 24) rather than as a quiet widening of these.
    ATTENDANCE_CLOCK_OWN = "attendance.clock_own"
    ATTENDANCE_READ_OWN = "attendance.read_own"

    # Projects and their tasks (ticket 27). Reading is what an employee needs to
    # fill in a timesheet; managing is what a project manager does to their own
    # project. The restriction that makes the second sentence true is a
    # *resource-level* rule — `PROJECT_ADMIN_ROLES` below and the project branch of
    # the kernel's `_can_on_resource` — not a narrower role list here, because the
    # same role may manage one project and be refused another.
    PROJECT_READ = "project.read"
    PROJECT_MANAGE = "project.manage"
    #: A task is managed through its project, so these two decide who may write a
    #: task and share the project branch: a task resource carries its project's
    #: department and its project's manager.
    PROJECT_TASK_READ = "project_task.read"
    PROJECT_TASK_MANAGE = "project_task.manage"

    # Work schedules, holidays and expected hours (ticket 22). Four acts rather
    # than one, because they have four different answers to "who": reading your own
    # week is self-service and self-only, the holiday calendar is published to
    # everybody the way the department tree is, and maintaining either the patterns
    # or the calendar is HR and administration.
    SCHEDULE_READ_OWN = "schedule.read_own"
    SCHEDULE_MANAGE = "schedule.manage"
    HOLIDAY_READ = "holiday.read"
    HOLIDAY_MANAGE = "holiday.manage"

    # Weekly timesheets (ticket 28). Three acts, all of them self-only, because the
    # ticket says so in as many words: filling in somebody else's hours is a 403.
    # Separating them is what will let ticket 29 give a manager "read my report's
    # week" without that arriving as a quiet widening of the employee's own read —
    # the same argument the attendance pair records above.
    TIMESHEET_READ_OWN = "timesheet.read_own"
    TIMESHEET_WRITE_OWN = "timesheet.write_own"
    #: Filing a week is its own act because it is the moment the week stops being
    #: the employee's to change: one permission for "type my hours" and "hand them
    #: to my manager for sign-off" would make the second impossible to withdraw.
    TIMESHEET_SUBMIT_OWN = "timesheet.submit_own"

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
    Action.ROLE_READ: ActionRule(
        roles=frozenset({"admin", "hr", "finance", "it", "compliance", "manager", "employee"}),
        description="What each role may do is published, not secret.",
    ),
    Action.ROLE_MANAGE: ActionRule(
        roles=frozenset({"admin"}),
        description=(
            "Granting and revoking roles is administration. It is deliberately a "
            "different action from managing an account: creating a login and "
            "deciding what that login may do are not the same authority."
        ),
    ),
    Action.AUDIT_READ: ActionRule(
        roles=frozenset({"compliance"}),
        description=(
            "The audit trail is read by compliance alone. Not administration, "
            "which is the subject of half of it, and not HR, which reads "
            "personnel files rather than the record of who read them."
        ),
    ),
    Action.NOTIFICATION_READ_OWN: ActionRule(
        roles=frozenset({"employee"}),
        description=(
            "Your own notification centre. Every account holds `employee` — the "
            "snapshot adds it — so this is the whole signed-in population, which "
            "is exactly who a notification is addressed to."
        ),
    ),
    Action.ATTENDANCE_CLOCK_OWN: ActionRule(
        roles=frozenset({"employee"}),
        description=(
            "Punching your own clock. Every account holds `employee`, so the role "
            "list is the whole signed-in population; what makes the action self-only "
            "is `SELF_ONLY_ACTIONS` beside this table, and no role bypasses it."
        ),
    ),
    Action.ATTENDANCE_READ_OWN: ActionRule(
        roles=frozenset({"employee"}),
        description=(
            "Reading your own day and your own range. The Spanish working-time "
            "obligation gives the employee access to their own record, which is this "
            "action and nobody else's."
        ),
    ),
    # §4.1 gives an employee "their own data" and, since a timesheet is filled in
    # against a project, the projects they may book against. Every role holds
    # `employee`, so the list is everyone; *which* projects is decided by the
    # resource and by `filter_for`, not here.
    Action.PROJECT_READ: ActionRule(
        roles=frozenset({"admin", "hr", "finance", "it", "compliance", "manager", "employee"}),
        description="Reading a project and its tasks; which ones is a resource question.",
    ),
    # `manager` is listed because a project manager is by definition a manager of
    # *their* project, and the role list cannot say "their own" — the kernel's
    # project branch does, and `PROJECT_ADMIN_ROLES` below names the two roles the
    # restriction does not apply to.
    Action.PROJECT_MANAGE: ActionRule(
        roles=frozenset({"admin", "hr", "manager"}),
        description=(
            "Creating a project, and changing one. Creation has no resource to "
            "restrict, so any manager may create one; changing an existing project "
            "is limited to its own manager unless the holder is administration or HR."
        ),
    ),
    Action.PROJECT_TASK_READ: ActionRule(
        roles=frozenset({"admin", "hr", "finance", "it", "compliance", "manager", "employee"}),
        description="The tasks of a project the caller may already read.",
    ),
    Action.PROJECT_TASK_MANAGE: ActionRule(
        roles=frozenset({"admin", "hr", "manager"}),
        description="Adding a task to a project, and maintaining the ones it has.",
    ),
    Action.SCHEDULE_READ_OWN: ActionRule(
        roles=frozenset({"employee"}),
        description=(
            "Reading your own week and your own expected hours. Self-only through "
            "`SELF_ONLY_ACTIONS`, like your own attendance: HR reading somebody's "
            "expected hours is a different act and will arrive as its own action "
            "(ticket 24), not as a wider version of this one."
        ),
    ),
    Action.SCHEDULE_MANAGE: ActionRule(
        roles=frozenset({"admin", "hr"}),
        description=(
            "Writing the weekly patterns, and the per-employee overrides with dates "
            "on them. This is the input to every expected-hours figure, which is why "
            "it is HR and administration and nobody else."
        ),
    ),
    Action.HOLIDAY_READ: ActionRule(
        roles=frozenset({"admin", "hr", "finance", "it", "compliance", "manager", "employee"}),
        description=(
            "The public holiday calendar. Published like the organisation tree: it is "
            "a fact about the country, not personnel data, and everyone plans around it."
        ),
    ),
    Action.HOLIDAY_MANAGE: ActionRule(
        roles=frozenset({"admin", "hr"}),
        description=(
            "Importing and editing the days nobody works. A holiday moves a month's "
            "expected hours, so it is the same authority as the schedule itself."
        ),
    ),
    # Weekly timesheets (ticket 28). Every account holds `employee`, so the role
    # list is the whole signed-in population; what makes these self-only is
    # `SELF_ONLY_ACTIONS` below, and no role bypasses it — not a manager for their
    # report, not HR for the company. Ticket 29's approver surface adds the action
    # that reads a report's week, as its own permission rather than as a wider
    # version of this one.
    Action.TIMESHEET_READ_OWN: ActionRule(
        roles=frozenset({"employee"}),
        description=(
            "Reading your own week, and the approval state of the ones you have "
            "filed. The Spanish working-time obligation gives the employee their own "
            "record; nobody else's is reachable through this action."
        ),
    ),
    Action.TIMESHEET_WRITE_OWN: ActionRule(
        roles=frozenset({"employee"}),
        description=(
            "Writing entries into your own draft week, copying the previous one into "
            "it, and removing what you typed. A filed week is refused by the module "
            "whatever this action says."
        ),
    ),
    Action.TIMESHEET_SUBMIT_OWN: ActionRule(
        roles=frozenset({"employee"}),
        description=(
            "Filing your own week with the approval engine. Its own action because "
            "it is the act that takes the week out of your hands: an installation "
            "that wanted a second pair of eyes before filing could take this one "
            "away without touching the writing."
        ),
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

#: Actions about the caller's own material, and nothing else.
#:
#: For these, ownership is not a convenience the kernel grants on the way to
#: deciding — it *is* the decision. A punch belongs to the person who made it, and
#: so does their week and the hours it expects of them; no role reaches somebody
#: else's through these actions: not a manager for their report, not HR for the
#: company, and not an administrator either. Ticket 24 adds the actions that read a
#: report's day and the company's, each with its own resource rule, which is what
#: stops "HR may see attendance" from arriving as a quiet widening of "I may see my
#: own". Ticket 28's three join them for the same reason and with the same
#: consequence: 代填 is a 403 the ticket asks for by name.
SELF_ONLY_ACTIONS: frozenset[Action] = frozenset(
    {
        Action.ATTENDANCE_CLOCK_OWN,
        Action.ATTENDANCE_READ_OWN,
        Action.SCHEDULE_READ_OWN,
        Action.TIMESHEET_READ_OWN,
        Action.TIMESHEET_WRITE_OWN,
        Action.TIMESHEET_SUBMIT_OWN,
    }
)

#: Roles that manage *every* project, not only the ones they manage themselves.
#:
#: Administration and HR, and no third role, because those are the two §4.1 gives
#: the organisation-wide remit: administration configures the system, HR keeps the
#: record of who worked on what. Finance reads projects (a timesheet is what it
#: exports) and does not manage them, so it is deliberately absent — the same
#: distinction `DOCUMENT_CROSS_DEPARTMENT_ROLES` above draws for finance.
#:
#: Stated as its own constant rather than as "is privileged" for the reason that
#: one is: the two sets are not the same, and a rule that read the wrong one would
#: hand project management to finance and to compliance.
PROJECT_ADMIN_ROLES = frozenset({"admin", "hr"})


def roles_may(action: Action, roles: frozenset[str]) -> bool:
    """Role-level check, before any resource nuance.

    Kept separate from `can()` so the resource-aware decision can be read as
    "role permits it, *and* this particular row is reachable".
    """
    rule = RULES[action]
    if rule.public:
        return True
    return bool(roles & rule.roles)
