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
    # Ticket 24, and the four actions it said it would add. Reading somebody else's
    # working-time record is two different reaches — a manager's, which stops at
    # their own reports, and HR's, which is the whole company — so it is two
    # actions: one action with a role list would make the second a consequence of
    # the first, and widening a manager's reach would widen HR's silently. Filing a
    # correction is two acts for the same reason: your own (self-only, like the
    # clock) and HR's after-the-fact correction of somebody else's record, which is
    # the same chain and the same engine.
    ATTENDANCE_READ_REPORT = "attendance.read_report"
    ATTENDANCE_READ_ALL = "attendance.read_all"
    ATTENDANCE_CORRECTION_OWN = "attendance.correction_own"
    ATTENDANCE_CORRECTION_ANY = "attendance.correction_any"

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

    # Leave (ticket 25). Six actions, and the split is the same one ticket 24 drew
    # for attendance, for the same reason: filing your own leave and reading it are
    # self-service and self-only, a manager reaches their reports, HR reaches the
    # company, and maintaining the catalogue is administration's. Nothing here is a
    # wider version of anything else — a manager reading their report's leave does not
    # inherit the employee's own action, and HR reading the company's does not inherit
    # the manager's.
    #:
    #: The type catalogue is published, like the holiday calendar: it is a fact about
    #: what the company offers rather than personnel data, and a client needs it to
    #: render the form.
    LEAVE_TYPE_READ = "leave.type_read"
    LEAVE_TYPE_MANAGE = "leave.type_manage"
    #: Your own balances and your own requests. Self-only through
    #: `SELF_ONLY_ACTIONS`: nobody reads somebody else's balance through this action.
    LEAVE_READ_OWN = "leave.read_own"
    #: Filing a request about yourself. Its own action rather than part of the read,
    #: because asking for time off is the act that spends an allowance and an
    #: installation may well want to hand it out separately.
    LEAVE_REQUEST_OWN = "leave.request_own"
    #: A manager's reach: their reports' leave, which is what approving it needs. The
    #: resource rule is the reporting relationship — see `LEAVE_CROSS_ACTIONS` — and
    #: deliberately not the department, because a manager and a colleague share one.
    LEAVE_READ_REPORT = "leave.read_report"
    #: HR's reach: the company's leave. HR owns the personnel file and the working-time
    #: record, and leave is part of both.
    LEAVE_READ_ALL = "leave.read_all"
    #: Reading the *file* a request refers to — a sick note, in the ordinary case —
    #: rather than the request. HR alone, and its own action because "may decide this
    #: leave" and "may read the medical proof attached to it" are different
    #: authorities: a manager approves the absence and is refused the note.
    LEAVE_ATTACHMENT_READ = "leave.attachment_read"
    #: Setting somebody's yearly entitlement or the days carried over from last year.
    #: A figure, not a document: it belongs with the catalogue rather than with the
    #: approval chain.
    LEAVE_BALANCE_MANAGE = "leave.balance_manage"

    # Overtime (ticket 26). Seven actions, and the seventh is the one the design
    # settles by name: §4.1 gives `finance` "加班月度导出" — the monthly overtime export
    # — because overtime pay is a payroll calculation and this system only accumulates
    # and exports the hours. So the company-wide *read* is two roles rather than one
    # (HR keeps the working-time record; finance reads it to pay from), which is why
    # overtime gets its own cross-action set and its own company-role set beside the
    # attendance and leave ones instead of borrowing theirs.
    #:
    #: Filing is self-service and self-only: nobody files somebody else's overtime, and
    #: the hours are applied for *before* they are worked, so there is nothing for HR to
    #: enter after the fact.
    OVERTIME_REQUEST_OWN = "overtime.request_own"
    OVERTIME_READ_OWN = "overtime.read_own"
    #: A manager's reach: their reports' overtime, which is what approving it needs.
    #: The resource rule is the reporting relationship — see `OVERTIME_CROSS_ACTIONS` —
    #: and deliberately not the department, because a manager and a colleague share one.
    OVERTIME_READ_REPORT = "overtime.read_report"
    #: The company's overtime: HR, which keeps the working-time record, and finance,
    #: which this design gives the monthly export. Nobody else — not administration,
    #: which configures the system rather than reading personnel files, and not
    #: compliance, whose overtime-adjacent read is the audit trail itself.
    OVERTIME_READ_ALL = "overtime.read_all"
    #: Confirming or adjusting the hours of a settled record. HR alone, and its own
    #: action because it is the one act here that overrules a computed figure: a
    #: manager approves the overtime, and is refused the adjustment.
    OVERTIME_CONFIRM = "overtime.confirm"
    #: Running the comparison of approved against actually worked for a period. HR's,
    #: because the outcome of it is a queue of differences that only HR can answer.
    OVERTIME_SETTLE = "overtime.settle"
    #: Producing the monthly file. Its own action rather than a use of
    #: `overtime.read_all`, because the file carries the staff number — a withheld
    #: field — and an installation may well want the reading of a screen and the
    #: handing over of a payroll file to be separable decisions. HR and finance, which
    #: is exactly the set `EMPLOYEE_READ_WITHHELD` names for that column, minus
    #: compliance: the reader of the audit trail reads *who exported what* rather than
    #: the payroll file itself.
    OVERTIME_EXPORT = "overtime.export"

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
    # Ticket 30's two reaches over *somebody else's* hours, and they are the pair
    # tickets 24, 25 and 26 drew for their own records: a manager reads the time of
    # the people who report to them, HR reads the company's. Neither is a wider
    # version of the three above — the kernel's report branch is the resource rule,
    # and `TIMESHEET_CROSS_ACTIONS` below is what names it — which is what stops
    # "HR may see every timesheet" from arriving as a quiet widening of "I may see
    # my own".
    #:
    #: **The manager's is two reaches, not one.** §4.1 gives a manager 直属下属; the
    #: ticket adds 项目经理, and a project manager named on a project reads the time
    #: booked against it whoever recorded it. Both are the same *kind* of caller and
    #: the same role list, so they are one action with a resource rule that is a
    #: union — the shape the project branch already uses, and the reason the reach
    #: lives in the kernel rather than in a query.
    TIMESHEET_READ_REPORT = "timesheet.read_report"
    #: HR's reach: the company's hours. §4.1 gives hr "考勤/请假/工时全量" and gives
    #: nobody else the timesheet — finance's company-wide read is the overtime
    #: export, which is a payroll calculation and not this.
    TIMESHEET_READ_ALL = "timesheet.read_all"

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
    # Ticket 24. Three readings of §4.1, recorded rather than left to the reader:
    # a manager reaches *their reports'* attendance, which is the half of §4.1's
    # manager row that is about time; HR reaches the company's, because the
    # working-time record is a personnel record and HR keeps those; and nobody
    # else — not administration, which configures the system rather than reading
    # personnel files, and not finance, which reads the payroll record and reads
    # these files through an export when an accountant needs one.
    Action.ATTENDANCE_READ_REPORT: ActionRule(
        roles=frozenset({"manager"}),
        description=(
            "Reading the attendance of the people who report to you, and of nobody "
            "else. The role list says *which kind of caller*; `ATTENDANCE_CROSS_"
            "ACTIONS` beside the self-only set says the resource rule, which is the "
            "reporting relationship itself — a manager reads their reports and is "
            "refused a colleague in their own department."
        ),
    ),
    Action.ATTENDANCE_READ_ALL: ActionRule(
        roles=frozenset({"hr"}),
        description=(
            "Reading anybody's attendance. HR owns the working-time record — it is "
            "what the four-year obligation is kept for — and the export an accountant "
            "or a labour inspector reads is produced through this action."
        ),
    ),
    Action.ATTENDANCE_CORRECTION_OWN: ActionRule(
        roles=frozenset({"employee"}),
        description=(
            "Filing a correction request about your own punch. Self-only through "
            "`SELF_ONLY_ACTIONS`: nobody asks for a correction of somebody else's day "
            "through this action, and a manager's approval of one is the engine's "
            "act, not this one."
        ),
    ),
    Action.ATTENDANCE_CORRECTION_ANY: ActionRule(
        roles=frozenset({"hr"}),
        description=(
            "Filing a correction request about somebody else's punch — 事后修正. HR "
            "corrects a record after the fact, and it goes down the same chain: the "
            "same document, the same two levels, the same append, and no in-place "
            "overwrite anywhere."
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
    # Ticket 30. §4.1 read as the two reaches the ticket names: a manager reads
    # their direct reports' hours, HR reads everybody's, and nobody else reads
    # anybody's. What the role list cannot say is *whose* hours — the manager's own
    # case and the project manager's are the same role and two different reasons, so
    # the kernel's report branch decides the row and `filter_for` describes it.
    Action.TIMESHEET_READ_REPORT: ActionRule(
        roles=frozenset({"manager"}),
        description=(
            "Reading the hours of the people who report to you, and the hours booked "
            "against the projects you manage. Two reaches and one role: a project "
            "manager manages *their* project, so the resource rule — not this list — "
            "is what narrows it, and a manager is refused a colleague in their own "
            "department, which the department clause would have allowed."
        ),
    ),
    Action.TIMESHEET_READ_ALL: ActionRule(
        roles=frozenset({"hr"}),
        description=(
            "Reading anybody's hours, and producing the report file. HR keeps the "
            "working-time record and §4.1 gives it the timesheet in full — the "
            "report and its export state minutes, and no rate, so what is handed "
            "over is the same record in another shape rather than a payroll figure."
        ),
    ),
    # Leave (ticket 25). Six actions, and the readings are recorded rather than left
    # to the reader, exactly as ticket 24 recorded its four.
    Action.LEAVE_TYPE_READ: ActionRule(
        roles=frozenset({"admin", "hr", "finance", "it", "compliance", "manager", "employee"}),
        description=(
            "The leave type catalogue. Published like the holiday calendar: it is what "
            "the company offers rather than personnel data, and the form that files a "
            "request is rendered from it."
        ),
    ),
    Action.LEAVE_TYPE_MANAGE: ActionRule(
        roles=frozenset({"admin", "hr"}),
        description=(
            "Adding a kind of leave, and changing whether it is paid, needs proof or "
            "spends the annual allowance. The same pair that maintains the schedules: "
            "these flags decide what a leave costs somebody."
        ),
    ),
    Action.LEAVE_READ_OWN: ActionRule(
        roles=frozenset({"employee"}),
        description=(
            "Your own balances, their history and your own requests. Self-only through "
            "`SELF_ONLY_ACTIONS`: the Spanish working-time obligation gives the employee "
            "their own record, and nobody else's is reachable through this action."
        ),
    ),
    Action.LEAVE_REQUEST_OWN: ActionRule(
        roles=frozenset({"employee"}),
        description=(
            "Asking for your own time off. Its own action rather than part of the read, "
            "because it is the act that spends an allowance — an installation may want "
            "to hand the two out separately, and filing is what the balance check "
            "guards."
        ),
    ),
    Action.LEAVE_READ_REPORT: ActionRule(
        roles=frozenset({"manager"}),
        description=(
            "Reading the leave of the people who report to you, and of nobody else. The "
            "role list says which *kind* of caller; `LEAVE_CROSS_ACTIONS` beside the "
            "self-only set says the resource rule, which is the reporting relationship "
            "itself — a manager reads their reports and is refused a colleague in their "
            "own department, which is the escalation the department clause would allow."
        ),
    ),
    Action.LEAVE_READ_ALL: ActionRule(
        roles=frozenset({"hr"}),
        description=(
            "Reading anybody's leave. HR keeps the personnel file and the working-time "
            "record, and a leave is in both: it is what the absence is explained by and "
            "what the payroll month is adjusted for."
        ),
    ),
    Action.LEAVE_ATTACHMENT_READ: ActionRule(
        roles=frozenset({"hr"}),
        description=(
            "Reading the file a request refers to — a sick note, in the ordinary case. "
            "HR alone, which is the design's §8 rule and the reason it is an action of "
            "its own: a manager approves the absence and is refused the medical proof, "
            "and the request's payload reports this list rather than hardcoding it."
        ),
    ),
    Action.LEAVE_BALANCE_MANAGE: ActionRule(
        roles=frozenset({"admin", "hr"}),
        description=(
            "Setting somebody's yearly entitlement, or the days carried over from last "
            "year. A figure rather than a document, which is why it does not go down "
            "the approval chain: the allowance is granted, and what is filed against it "
            "is what gets approved."
        ),
    ),
    # Overtime (ticket 26). The readings are recorded rather than left to the reader,
    # as tickets 24 and 25 recorded theirs.
    Action.OVERTIME_REQUEST_OWN: ActionRule(
        roles=frozenset({"employee"}),
        description=(
            "Asking to work overtime on a date, in advance. Self-only through "
            "`SELF_ONLY_ACTIONS`: nobody files somebody else's overtime, and there is "
            "no retroactive path at all — the request has to be for today or later, so "
            "HR has nothing to enter after the fact either."
        ),
    ),
    Action.OVERTIME_READ_OWN: ActionRule(
        roles=frozenset({"employee"}),
        description=(
            "Your own overtime records, your own totals and their history. Self-only "
            "through `SELF_ONLY_ACTIONS`, like your own attendance and your own leave."
        ),
    ),
    Action.OVERTIME_READ_REPORT: ActionRule(
        roles=frozenset({"manager"}),
        description=(
            "Reading the overtime of the people who report to you, and of nobody else. "
            "The role list says which *kind* of caller; `OVERTIME_CROSS_ACTIONS` beside "
            "the self-only set says the resource rule, which is the reporting "
            "relationship itself — a manager reads their reports and is refused a "
            "colleague in their own department."
        ),
    ),
    Action.OVERTIME_READ_ALL: ActionRule(
        roles=frozenset({"hr", "finance"}),
        description=(
            "Reading anybody's overtime. HR keeps the working-time record, and §4.1 "
            "gives finance the monthly overtime export because overtime pay is a "
            "payroll calculation — this is the only action in the catalogue where "
            "finance's company-wide reach is written down, and it is narrow on purpose: "
            "the hours, and nothing about the person beyond the name the file states."
        ),
    ),
    Action.OVERTIME_CONFIRM: ActionRule(
        roles=frozenset({"hr"}),
        description=(
            "Confirming or adjusting the hours of a settled record, with the reason. HR "
            "alone: the computed figure stays readable, and the confirmed one is stored "
            "beside it, so this is the one act in the module that overrules arithmetic "
            "and it is deliberately not a manager's."
        ),
    ),
    Action.OVERTIME_SETTLE: ActionRule(
        roles=frozenset({"hr"}),
        description=(
            "Running the comparison of the approved minutes against the day's actually "
            "worked minutes for a period. HR's, because its outcome is a queue of "
            "differences that only HR can answer."
        ),
    ),
    Action.OVERTIME_EXPORT: ActionRule(
        roles=frozenset({"hr", "finance"}),
        description=(
            "Producing the month's CSV. Its own action because the file carries the "
            "staff number — a withheld field — so an installation may separate the "
            "reading of a screen from the handing over of a payroll file. HR and "
            "finance: the two roles §4.1 gives the personnel side and the payroll side, "
            "and the two `EMPLOYEE_READ_WITHHELD` already names for that column."
        ),
    ),
    Action.DOCUMENT_READ: ActionRule(
        roles=frozenset({"admin", "hr", "finance", "it", "compliance", "employee"}),
        description="Reading a document; clearance and department decide which ones.",
    ),    Action.DOCUMENT_LIST: ActionRule(
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
        Action.ATTENDANCE_CORRECTION_OWN,
        Action.SCHEDULE_READ_OWN,
        Action.TIMESHEET_READ_OWN,
        Action.TIMESHEET_WRITE_OWN,
        Action.TIMESHEET_SUBMIT_OWN,
        # Ticket 25's two: your own leave, and asking for more of it. HR and a
        # manager are refused somebody else's through them and reach it through their
        # own actions instead.
        Action.LEAVE_READ_OWN,
        Action.LEAVE_REQUEST_OWN,
        # Ticket 26's two: your own overtime, and asking for it in advance. The same
        # rule, and it carries the module's central constraint: nobody files somebody
        # else's overtime, and there is no retroactive entry for anybody to file.
        Action.OVERTIME_REQUEST_OWN,
        Action.OVERTIME_READ_OWN,
    }
)

#: The actions that reach *somebody else's* attendance record, and the reason they
#: are listed here rather than left to the generic path (ticket 24).
#:
#: The kernel's ordinary employee path would answer these with the department
#: clause: a manager and a colleague share a department, so "in my department" would
#: be read as "mine to read", which is exactly the escalation the ticket refuses by
#: name. So these actions are decided by their own branch — the reporting
#: relationship, or a company-wide remit, and nothing else — and this set is what
#: names them, so a fifth action added to the attendance surface has to be put in
#: one of the two lists deliberately.
ATTENDANCE_CROSS_ACTIONS: frozenset[Action] = frozenset(
    {
        Action.ATTENDANCE_READ_REPORT,
        Action.ATTENDANCE_READ_ALL,
        Action.ATTENDANCE_CORRECTION_ANY,
    }
)

#: The same two reaches over somebody else's *leave* (ticket 25), and they are the
#: same shape for the same reason: a manager reads their reports' leave because
#: approving it is theirs, HR reads the company's because the personnel file is, and
#: neither is decided by the department — a manager and a colleague share one.
#:
#: Stated as its own set rather than folded into the attendance one, so that adding a
#: sixth attendance action or a third leave action is a decision somebody makes in one
#: of the two lists rather than something that happens to both.
LEAVE_CROSS_ACTIONS: frozenset[Action] = frozenset(
    {
        Action.LEAVE_READ_REPORT,
        Action.LEAVE_READ_ALL,
    }
)

#: The same two reaches over somebody else's *overtime* (ticket 26), and the reason it
#: is a third set rather than a wider second one: the company-wide role list is not the
#: same. §4.1 gives finance the monthly overtime export, so `OVERTIME_COMPANY_ROLES`
#: below has two members where the attendance and leave sets have one, and folding
#: overtime into `LEAVE_CROSS_ACTIONS` would have handed finance the whole company's
#: leave as well — a reach nobody granted it.
OVERTIME_CROSS_ACTIONS: frozenset[Action] = frozenset(
    {
        Action.OVERTIME_READ_REPORT,
        Action.OVERTIME_READ_ALL,
    }
)

#: The same two reaches over somebody else's *hours* (ticket 30), stated as its own
#: set for the reason the three above are: adding a fourth timesheet action is a
#: decision somebody makes in this list rather than something that happens to the
#: attendance surface as well.
#:
#: **The export is deliberately not here.** Ticket 26 gave its file an action of its
#: own because the payroll CSV carries the staff number — a withheld field — so
#: reading a screen and handing over a payroll file are separable decisions. This
#: report states minutes and names, never the staff number (`timesheet/export.py`
#: says why), so the file is the same rows as the screen and the same two actions
#: govern it; a third action would be a second thing to grant for no new authority.
TIMESHEET_CROSS_ACTIONS: frozenset[Action] = frozenset(
    {
        Action.TIMESHEET_READ_REPORT,
        Action.TIMESHEET_READ_ALL,
    }
)

#: The roles whose reach over somebody else's personnel record — their hours, their
#: leave — is the whole company rather than their own reports.
#:
#: HR, and deliberately not "is privileged": finance and compliance are privileged for
#: *personnel files* (§4.1) and reading somebody's hours or their leave is not what
#: either of them was given, so the two sets are stated separately and cannot be
#: confused.
COMPANY_RECORD_ROLES: frozenset[str] = frozenset({"hr"})

#: Ticket 24's name for the same set, kept because the attendance surface reads it and
#: so does the permission matrix. One value today, and the alias is what says so —
#: two literals would be two things to change.
ATTENDANCE_COMPANY_ROLES: frozenset[str] = COMPANY_RECORD_ROLES

#: The roles whose reach over somebody else's *hours* is the whole company (ticket 30).
#:
#: The same one member as the personnel-record set, and an alias rather than a second
#: literal for the reason the attendance one is: §4.1 gives HR the timesheet in full
#: and gives nobody else a company-wide reading of it, and a copy of `{"hr"}` would be
#: a second place to change the day an installation wants finance to have it — which
#: would then silently give finance the company's leave as well if the copy were made
#: in the wrong place.
TIMESHEET_COMPANY_ROLES: frozenset[str] = COMPANY_RECORD_ROLES

#: The roles whose reach over somebody else's *overtime* is the whole company (ticket
#: 26).
#:
#: Two, where the personnel-record set above has one, and the second is the whole
#: reason this constant exists separately: `docs/DESIGN.md` §4.1 gives finance
#: "加班月度导出" — the monthly overtime export — and gives it nothing else in the
#: personnel record. Overtime is what a payroll month is computed from, so the reading
#: is finance's; a leave, an absence or a punch is not, so it is not. Stated as its own
#: set rather than widened into `COMPANY_RECORD_ROLES`, which would have granted
#: finance the company's attendance and leave in the same edit.
OVERTIME_COMPANY_ROLES: frozenset[str] = frozenset({"hr", "finance"})

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
