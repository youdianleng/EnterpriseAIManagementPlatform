"""The permission matrix, the database backstop, and the escalations worth naming.

Five layers, one file, because they are five views of one rule and a change to
that rule has to satisfy all of them:

1. **The kernel matrix**, generated. Every role against every catalogued action
   against a resource with and without a clearance and a department. The
   expectation is written out from `docs/DESIGN.md` §4.1 and §4.2 *below*, not
   read from the kernel: a test that imported `RULES` would only prove the kernel
   agrees with itself.
2. **Row-level security**, over connections made with the restricted role
   `eam_app`. Nothing is mocked; the claim is about what PostgreSQL does with a
   policy when the application forgets.
3. **The HTTP matrix**, which is the layer that catches an endpoint that never
   asked the kernel at all.
4. **Named escalation scenarios**, one test each, with the real-world mistake it
   prevents.
5. **Cache invalidation**, through the HTTP surface, with a real write.

**Documents do not exist yet.** Ticket 31 adds `documents` and
`document_chunks`, and attendance (21) and salary (43) are further out. §4.2 is
nonetheless already in the database, as `document_visibility_predicate`, written
by ticket 13 so the rule has one home before there is a table to attach it to.
Layer 2 therefore creates its own `scratch_documents` table inside a transaction
that is rolled back, attaches that predicate as its policy the way ticket 31's
migration will, and asserts §4.2 against a real policy over a real connection.
That is the only way to test the document rule against a real database before the
documents module exists, and it is why the table is built here rather than
awaited. Nothing below claims to be end to end that is not; each scenario says
which ticket supplies the half that is missing.

**Where the expectation comes from.** §4.1 is one line per role and the catalogue
is twenty-one actions; mapping one to the other needs readings, and the readings
are recorded in `DESIGN_GRANTS` rather than left implicit. The document rule is
written out twice on purpose — once for `can()` (four clauses) and once for the
policy (the two clauses a row predicate can express) — because the two layers
answer different questions.
"""

import itertools
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings
from app.core.errors import ErrorCode
from app.domain.access.kernel import (
    CLEARANCE_RANK,
    Reason,
    Resource,
    ResourceKind,
    can,
)
from app.domain.access.permissions import (
    ATTENDANCE_COMPANY_ROLES,
    ATTENDANCE_CROSS_ACTIONS,
    PROJECT_ADMIN_ROLES,
    SELF_ONLY_ACTIONS,
    Action,
)
from app.domain.access.principal import SYSTEM_ROLES, Principal
from tests.support.platform import Platform

#: The role requests connect as. Named rather than imported from the migration, so
#: a rename shows up as a failing test instead of as two places agreeing.
APP_ROLE = "eam_app"

MY_DEPARTMENT = uuid4()
OTHER_DEPARTMENT = uuid4()
FINANCE_DEPARTMENT = uuid4()
MY_EMPLOYEE = uuid4()
OTHER_EMPLOYEE = uuid4()
REPORT_EMPLOYEE = uuid4()

CLEARANCES = ("low", "medium", "high")

#: The clearance every generated principal holds: the middle rank, so a resource
#: can sit below it, level with it, or above it.
CALLER_CLEARANCE = "medium"

#: §4.1 gives hr "the full personnel file" and finance "the payroll record", and
#: names compliance as the reader of the record of who read what. These three are
#: the roles for which the clearance ceiling and the department boundary do not
#: apply to *employee* material.
PRIVILEGED_ROLES = frozenset({"hr", "finance", "compliance"})

#: §4.2's last clause, written out rather than imported: hr and compliance reach
#: company documents outside their own departments.
DOCUMENT_EXCEPTION_ROLES = frozenset({"hr", "compliance"})

EVERYONE = frozenset(SYSTEM_ROLES)

#: `docs/DESIGN.md` §4.1 read as a grant table, one line per catalogued action.
#:
#: Three readings had to be taken, and they are recorded rather than hidden,
#: because a table that quietly copied the kernel would be the kernel agreeing
#: with itself:
#:
#: * **`manager` is an overlay.** §4.1 says so, and the snapshot builder adds
#:   `employee` to every account, so a role is evaluated as `{role, employee}` —
#:   the principal the platform actually builds. A matrix of bare roles would
#:   assert about a principal nobody ever has, and `manager`'s own scope could not
#:   be stated at all: everything §4.1 gives a manager (their reports' attendance,
#:   leave and timesheets) belongs to actions tickets 21–25 add.
#: * **IT's "no business data" is read as personnel *content*.** §4.1 gives IT
#:   account recovery and forced sign-out, and neither has an action of its own:
#:   `account.manage` also creates and disables accounts, and forcing a sign-out is
#:   not an action at all. So the fail-closed reading is taken — IT holds nothing
#:   administrative — and the contact list, which is the one personnel surface the
#:   duty requires it to look at, stays readable. `employee.directory` publishes
#:   that list to everyone.
#: * **`admin`'s "system administration" includes system content.** §4.1 gives
#:   administration the structure and the clearances; the company knowledge base
#:   is the third thing it administers, and `document.manage` is the action that
#:   says so.
DESIGN_GRANTS: dict[Action, frozenset[str]] = {
    # Every role's scope is stated as a place in the tree, and reading it is how
    # you find out where you are. §4.1 restricts whoever it means to restrict, and
    # names nobody here.
    Action.DEPARTMENT_READ: EVERYONE,
    # admin: "department and clearance configuration". hr: onboarding, transfer
    # and offboarding, the moves that change the tree.
    Action.DEPARTMENT_MANAGE: frozenset({"admin", "hr"}),
    # The catalogue of what people are, which is structure like the tree.
    Action.POSITION_READ: EVERYONE,
    # admin changes primary positions; hr runs the moves that need a position.
    Action.POSITION_MANAGE: frozenset({"admin", "hr"}),
    # employee: "this department's contact list" — and a contact list only HR may
    # read is not a contact list.
    Action.EMPLOYEE_DIRECTORY: EVERYONE,
    # The same rows queried as data, which is personnel work: the personnel file
    # (hr), the payroll record (finance), the data-protection reader's view
    # (compliance), account administration's need to identify a person (it), and
    # the administrator who owns the system.
    Action.EMPLOYEE_LIST: frozenset({"admin", "hr", "finance", "compliance", "it"}),
    # One profile. Which *fields* travel is the visibility projection's decision,
    # not this action's.
    Action.EMPLOYEE_READ: EVERYONE,
    # "Their own data": everyone in the system is an employee.
    Action.EMPLOYEE_READ_OWN: EVERYONE,
    # Employee records: admin creates accounts and changes primary positions, hr
    # keeps the personnel file.
    Action.EMPLOYEE_MANAGE: frozenset({"admin", "hr"}),
    # hr reads the full personnel file; finance reads the payroll record, which is
    # where an address and a staff number are read from; compliance is the reader
    # the design names. Not admin — §4.1 separates the duties, and administration
    # that could read these fields could read a salary.
    Action.EMPLOYEE_READ_WITHHELD: frozenset({"hr", "finance", "compliance"}),
    # Accounts are administration's own object.
    Action.ACCOUNT_LIST: frozenset({"admin"}),
    Action.ACCOUNT_MANAGE: frozenset({"admin"}),
    # Anyone may inspect their own session. Forcing somebody else out is the
    # administrative act §4.1 gives IT, and it has no action yet.
    Action.SESSION_READ_OWN: EVERYONE,
    # §4.1 is itself a published table of what each role may do; a mapping nobody
    # is allowed to read is a mapping nobody can review.
    Action.ROLE_READ: EVERYONE,
    # Granting and revoking roles is administration's own authority, and a
    # different one from creating the login that holds them.
    Action.ROLE_MANAGE: frozenset({"admin"}),
    # compliance: "the read-only audit log", named in that row alone. Not admin:
    # an administrator is the subject of half the trail, which is the arrangement
    # audit exists to avoid.
    Action.AUDIT_READ: frozenset({"compliance"}),
    # employee: "clearance-bounded document Q&A", and every other role is an
    # employee as well. *Which* documents is §4.2's question, asked separately.
    Action.DOCUMENT_READ: EVERYONE,
    Action.DOCUMENT_LIST: EVERYONE,
    Action.DOCUMENT_UPLOAD: EVERYONE,
    # hr: "company-level document management"; admin: system content.
    Action.DOCUMENT_MANAGE: frozenset({"admin", "hr"}),
    # Classifying a company document: admin configures clearances, hr manages the
    # knowledge base.
    Action.DOCUMENT_SET_CLEARANCE: frozenset({"admin", "hr"}),
    # "Their own data" again, for the notification centre: what the system told
    # you is yours to read. Nobody reads anybody else's, which is the whole rule.
    Action.NOTIFICATION_READ_OWN: EVERYONE,
    # Attendance (ticket 21). §4.1 gives an employee their own record: the four
    # hours they worked, the punch they forgot, the day they were absent. Every
    # role holds `employee`, so the role list is everyone — and the *reading* that
    # makes these two self-only is recorded in `design_says` below, because "every
    # role may" and "nobody may do it for somebody else" are both true at once.
    Action.ATTENDANCE_CLOCK_OWN: EVERYONE,
    Action.ATTENDANCE_READ_OWN: EVERYONE,
    # Ticket 24's four. §4.1 gives a manager their reports' attendance and HR the
    # company's, and an employee their own: the two reads that reach somebody else
    # are separate actions with separate role lists, and the two filing actions are
    # split the same way — your own punch (self-only, like the clock) and HR's
    # after-the-fact correction of somebody else's. What the role list cannot say is
    # *whose* record, so `design_says` refuses the generated resource for a manager
    # — it names no owner, and this dimension contains no reports — and layer 4
    # asserts the manager's own case by name.
    Action.ATTENDANCE_READ_REPORT: frozenset({"manager"}),
    Action.ATTENDANCE_READ_ALL: frozenset({"hr"}),
    Action.ATTENDANCE_CORRECTION_OWN: EVERYONE,
    Action.ATTENDANCE_CORRECTION_ANY: frozenset({"hr"}),
    # Projects (ticket 27). Reading the catalogue is what an employee needs in order
    # to fill in a timesheet, and a catalogue only some roles may read is not a
    # catalogue. What is *not* in this table is the second half of the rule: who may
    # change which project is a fact about the row (its manager), and the named
    # escalations in layer 4 assert it.
    Action.PROJECT_READ: EVERYONE,
    Action.PROJECT_TASK_READ: EVERYONE,
    # Creating a project, and changing one. A project *manager* is a manager of
    # their own project, so the role list keeps `manager` and the resource clause
    # narrows it — the one shape in this catalogue where the role list alone is not
    # the answer. §4.1's "administration and HR manage everything" is the privilege
    # clause beside it.
    Action.PROJECT_MANAGE: frozenset({"admin", "hr", "manager"}),
    Action.PROJECT_TASK_MANAGE: frozenset({"admin", "hr", "manager"}),
    # Schedules and holidays (ticket 22). Reading your own week is "their own data"
    # a third time, and self-only through `SELF_ONLY_ACTIONS`; the holiday calendar
    # is published like the organisation tree, because it is a fact about the
    # country rather than personnel data. Maintaining either is HR and
    # administration — the same pair that owns the department structure, and for the
    # same reason: both are inputs to what the company owes somebody.
    Action.SCHEDULE_READ_OWN: EVERYONE,
    Action.SCHEDULE_MANAGE: frozenset({"admin", "hr"}),
    Action.HOLIDAY_READ: EVERYONE,
    Action.HOLIDAY_MANAGE: frozenset({"admin", "hr"}),
    # Weekly timesheets (ticket 28). "Their own data" a fourth time, and self-only
    # through `SELF_ONLY_ACTIONS`: §4.1 gives an employee their own record, and 代填 —
    # filling in somebody else's hours — is a refusal the ticket states by name. The
    # three are separate acts because filing a week is the moment it stops being the
    # employee's to change, and an installation that wanted a second pair of eyes
    # before filing could take `timesheet.submit_own` away without touching the
    # writing. Ticket 29's approver surface adds the action that reads a report's
    # week, as its own permission rather than a wider version of these.
    Action.TIMESHEET_READ_OWN: EVERYONE,
    Action.TIMESHEET_WRITE_OWN: EVERYONE,
    Action.TIMESHEET_SUBMIT_OWN: EVERYONE,
}

#: The resource each action acts on. A document is decided by §4.2 whatever the
#: verb, because the design states one rule for documents and does not qualify it
#: by what is being done to them.
KIND_FOR_ACTION: dict[Action, ResourceKind] = {
    Action.DEPARTMENT_READ: ResourceKind.DEPARTMENT,
    Action.DEPARTMENT_MANAGE: ResourceKind.DEPARTMENT,
    Action.POSITION_READ: ResourceKind.POSITION,
    Action.POSITION_MANAGE: ResourceKind.POSITION,
    Action.EMPLOYEE_DIRECTORY: ResourceKind.EMPLOYEE,
    Action.EMPLOYEE_LIST: ResourceKind.EMPLOYEE,
    Action.EMPLOYEE_READ: ResourceKind.EMPLOYEE,
    Action.EMPLOYEE_READ_OWN: ResourceKind.EMPLOYEE,
    Action.EMPLOYEE_MANAGE: ResourceKind.EMPLOYEE,
    Action.EMPLOYEE_READ_WITHHELD: ResourceKind.EMPLOYEE,
    Action.ACCOUNT_LIST: ResourceKind.ACCOUNT,
    Action.ACCOUNT_MANAGE: ResourceKind.ACCOUNT,
    Action.SESSION_READ_OWN: ResourceKind.ACCOUNT,
    # The catalogue of roles is published alongside the catalogue of accounts, and
    # is the closest existing kind: there is no role-shaped resource yet.
    Action.ROLE_READ: ResourceKind.ACCOUNT,
    Action.ROLE_MANAGE: ResourceKind.ACCOUNT,
    Action.AUDIT_READ: ResourceKind.AUDIT_LOG,
    Action.DOCUMENT_READ: ResourceKind.DOCUMENT,
    Action.DOCUMENT_LIST: ResourceKind.DOCUMENT,
    Action.DOCUMENT_UPLOAD: ResourceKind.DOCUMENT,
    Action.DOCUMENT_MANAGE: ResourceKind.DOCUMENT,
    Action.DOCUMENT_SET_CLEARANCE: ResourceKind.DOCUMENT,
    # Notifications hang off the person they were sent to, which is the employee
    # resource the router names when it asks the kernel.
    Action.NOTIFICATION_READ_OWN: ResourceKind.EMPLOYEE,
    # Attendance is about a person's own record, so the employee resource is the
    # one the router names — carrying the *subject's* employee id as its owner.
    Action.ATTENDANCE_CLOCK_OWN: ResourceKind.EMPLOYEE,
    Action.ATTENDANCE_READ_OWN: ResourceKind.EMPLOYEE,
    # The record is a person's, whichever action reaches it: the subject is the
    # resource's owner, and the kernel's attendance branch is what reads it.
    Action.ATTENDANCE_READ_REPORT: ResourceKind.EMPLOYEE,
    Action.ATTENDANCE_READ_ALL: ResourceKind.EMPLOYEE,
    Action.ATTENDANCE_CORRECTION_OWN: ResourceKind.EMPLOYEE,
    Action.ATTENDANCE_CORRECTION_ANY: ResourceKind.EMPLOYEE,
    # Projects and their tasks. A task is decided as its project: it has no reach of
    # its own, so the resource the router builds carries the project's department and
    # the project's manager.
    Action.PROJECT_READ: ResourceKind.PROJECT,
    Action.PROJECT_MANAGE: ResourceKind.PROJECT,
    Action.PROJECT_TASK_READ: ResourceKind.PROJECT,
    Action.PROJECT_TASK_MANAGE: ResourceKind.PROJECT,
    # Ticket 22 gives each of its two objects its own kind rather than sharing
    # `department`: the kernel treats both through the ordinary department path
    # (no branch of its own), and the kinds exist so the audit trail and the matrix
    # say "schedule" and "holiday" instead of guessing at a third thing.
    Action.SCHEDULE_READ_OWN: ResourceKind.EMPLOYEE,
    Action.SCHEDULE_MANAGE: ResourceKind.SCHEDULE,
    Action.HOLIDAY_READ: ResourceKind.HOLIDAY,
    Action.HOLIDAY_MANAGE: ResourceKind.HOLIDAY,
    # The week, which is its own kind rather than the employee it belongs to: the
    # resource carries the week's owner, and the audit trail should say "timesheet"
    # rather than guess at a person. It takes the self-only branch and nothing else.
    Action.TIMESHEET_READ_OWN: ResourceKind.TIMESHEET,
    Action.TIMESHEET_WRITE_OWN: ResourceKind.TIMESHEET,
    Action.TIMESHEET_SUBMIT_OWN: ResourceKind.TIMESHEET,
}

#: Actions the catalogue decides by role alone, with no resource clause to apply.
#:
#: Every read here is one: the organisation's own directory, its structure, its
#: published role catalogue, and — since ticket 27 — which projects are running and
#: who runs them. What is withheld in this system is *content* (a document above your
#: clearance, an employee's withheld fields, a salary), never the fact that a piece
#: of structure exists.
#:
#: The list is written out rather than derived from the verb, because deriving it is
#: how a read that *is* restricted would be swept in: `EMPLOYEE_READ` and
#: `DOCUMENT_READ` are reads, and both have a resource rule the matrix asserts.
RESOURCE_FREE_READS: frozenset[Action] = frozenset(
    {
        Action.DEPARTMENT_READ,
        Action.POSITION_READ,
        Action.EMPLOYEE_DIRECTORY,
        Action.EMPLOYEE_LIST,
        Action.EMPLOYEE_READ_OWN,
        Action.ACCOUNT_LIST,
        Action.SESSION_READ_OWN,
        Action.ROLE_READ,
        Action.AUDIT_READ,
        Action.NOTIFICATION_READ_OWN,
        Action.PROJECT_READ,
        Action.PROJECT_TASK_READ,
    }
)

ACTIONS = tuple(sorted(Action, key=str))

#: The resource dimension: with and without a clearance, with and without a
#: department. Both *values* of each are present — a clearance below and above the
#: caller's, a department the caller reaches and one they do not — because "with a
#: clearance" only means something once the two sides of the ceiling are told
#: apart. `None` is the action with no resource named at all, which is the shape
#: every `require()` dependency uses.
RESOURCE_CLEARANCES: tuple[str | None, ...] = (None, *CLEARANCES)
RESOURCE_DEPARTMENTS: tuple[str | None, ...] = (None, "mine", "other")
RESOURCE_SHAPES: tuple[tuple[str | None, str | None] | None, ...] = (
    None,
    *itertools.product(RESOURCE_CLEARANCES, RESOURCE_DEPARTMENTS),
)


def may(role: str, action: Action) -> bool:
    """§4.1's answer for one role, held alongside `employee`."""
    return bool(frozenset({role, "employee"}) & DESIGN_GRANTS[action])


def principal(
    role: str,
    *,
    clearance: str = CALLER_CLEARANCE,
    reports: tuple[UUID, ...] = (),
    departments: tuple[UUID, ...] = (MY_DEPARTMENT,),
) -> Principal:
    return Principal(
        user_id=uuid4(),
        employee_id=MY_EMPLOYEE,
        username="ana",
        roles=frozenset({role, "employee"}),
        clearance_level=clearance,
        department_ids=frozenset(departments),
        primary_department_id=MY_DEPARTMENT if departments else None,
        is_manager=role == "manager",
        reports_employee_ids=frozenset(reports),
    )


def shape_resource(kind: ResourceKind, shape: tuple[str | None, str | None]) -> Resource:
    clearance, department = shape
    return Resource(
        kind=kind,
        department_id={
            "mine": MY_DEPARTMENT,
            "other": OTHER_DEPARTMENT,
            None: None,
        }[department],
        clearance=clearance,
        # A company document: the ordinary case, and the one the policy is written
        # for. Personal uploads, explicit shares and ownership are their own
        # dimension and are generated in `test_document_access.py`.
        is_company_kb=kind is ResourceKind.DOCUMENT,
    )


def design_says_document(
    held: frozenset[str], clearance: str | None, department: str | None
) -> bool:
    """§4.2 for a company document nobody owns and nobody shared.

    Clause 1 (ownership) and clause 3 (an explicit share) are not in this
    dimension; clauses 2 and 4 are, and clause 4 does not lift the ceiling.
    """
    clearance_ok = (
        clearance is None or CLEARANCE_RANK[clearance] <= CLEARANCE_RANK[CALLER_CLEARANCE]
    )
    department_ok = department == "mine"
    if clearance_ok and department_ok:
        return True
    return clearance_ok and bool(held & DOCUMENT_EXCEPTION_ROLES)


def design_says(role: str, action: Action, shape: tuple[str | None, str | None] | None) -> bool:
    """§4.1 and §4.2, answered without asking the kernel.

    The general rule for a resource that is a row in the organisation tree is
    D11's pair of conditions read as the system's access rule — the caller's
    clearance is a ceiling and the department has to be one they reach — which is
    what §4.2 states for documents and what the rest of the tree inherits.
    Ownership and the manager-of-a-report clause are not dimensions of this
    matrix; layer 4 asserts them by name.
    """
    if not may(role, action):
        return False
    if shape is None:
        return True

    # The self-only actions. Every generated resource is somebody else's — it
    # names no owner, and the matrix's caller owns only what they own — so the
    # answer is "no" for every role, HR and management included. §4.1 gives an
    # employee their own attendance and gives nobody anybody else's through these
    # two actions; ticket 24 crosses that line with a new action, which is the
    # reason this expectation is written as a refusal rather than as a role check.
    if action in SELF_ONLY_ACTIONS:
        return False

    # Somebody else's attendance (ticket 24). The generated resource names no owner,
    # and this dimension has no reports in it, so a manager reaches nothing here —
    # while HR's remit does not depend on the row at all. The manager's own case is
    # asserted by name in layer 4, and the department is deliberately not part of the
    # expectation: "in my department" is the reading these actions refuse.
    if action in ATTENDANCE_CROSS_ACTIONS:
        return bool(frozenset({role, "employee"}) & ATTENDANCE_COMPANY_ROLES)

    held = frozenset({role, "employee"})
    clearance, department = shape

    # Reading a withheld field is decided by the role alone: there is no resource
    # that makes an address acceptable for another role.
    if action is Action.EMPLOYEE_READ_WITHHELD:
        return True

    # A project, and the generated resource is one the caller does not manage: it
    # names no manager, and the matrix's caller manages only what they manage — the
    # same shape as the self-only refusal above, and true for the same reason. What
    # the generated dimension *can* say is §4.1's second half: administration and HR
    # name nobody on a project and manage it anyway. The manager's own case is not
    # in this dimension and is asserted by name in layer 4.
    if KIND_FOR_ACTION[action] is ResourceKind.PROJECT:
        if action in RESOURCE_FREE_READS:
            return True
        return bool(held & PROJECT_ADMIN_ROLES)

    if KIND_FOR_ACTION[action] is ResourceKind.DOCUMENT:
        return design_says_document(held, clearance, department)

    privileged = bool(held & PRIVILEGED_ROLES)
    if not privileged:
        if clearance is not None and CLEARANCE_RANK[clearance] > CLEARANCE_RANK[CALLER_CLEARANCE]:
            return False
        if department is not None and department != "mine":
            return False
    return True


# --- layer 1: the kernel matrix ---------------------------------------------


def test_the_generated_matrix_covers_every_dimension() -> None:
    """Role × action × resource shape, counted, and shown to discriminate.

    The count is asserted so that a refactor which quietly drops a dimension —
    a resource shape, a role, an action — fails here instead of passing with less
    coverage than it claims. The two distribution assertions guard the other
    direction: a matrix whose expectation came out the same way for every case
    would pass whatever the kernel did.
    """
    cases = list(itertools.product(sorted(SYSTEM_ROLES), ACTIONS, RESOURCE_SHAPES))
    permitted = sum(1 for role, action, shape in cases if design_says(role, action, shape))

    assert len(RESOURCE_SHAPES) == 1 + len(RESOURCE_CLEARANCES) * len(RESOURCE_DEPARTMENTS)
    assert len(cases) == len(SYSTEM_ROLES) * len(ACTIONS) * len(RESOURCE_SHAPES)
    # Literal on purpose: `len(ACTIONS)` would agree with itself however many
    # actions the catalogue grew, and the point is that adding one is a decision
    # somebody makes here rather than something that happens. Ticket 21 wrote 24;
    # ticket 27 adds the four project actions; ticket 22 adds four more — reading
    # your own week, maintaining schedules, reading the calendar, maintaining it —
    # ticket 28 adds three for the weekly timesheet (read your own week, write it,
    # file it), ticket 24 adds four for attendance corrections (a report's record,
    # the company's record, your own correction, HR's), and the sum is asserted
    # literally so that a fourth arrives as a failing test rather than as extra
    # coverage.
    assert len(cases) == 7 * 39 * 13
    assert 0 < permitted < len(cases), "the expectation answers the same way everywhere"

    discriminating = [
        action
        for action in ACTIONS
        if any(may(role, action) for role in SYSTEM_ROLES)
        and not all(may(role, action) for role in SYSTEM_ROLES)
    ]
    assert len(discriminating) >= 8, (
        "too few actions tell one role from another to be a permission matrix: "
        f"{[str(action) for action in discriminating]}"
    )


def test_the_design_table_describes_every_action_and_its_resource() -> None:
    """A new action arriving without an expectation would otherwise be covered by
    nothing at all, silently."""
    assert sorted(DESIGN_GRANTS, key=str) == list(ACTIONS)
    assert sorted(KIND_FOR_ACTION, key=str) == list(ACTIONS)


@pytest.mark.parametrize("role", sorted(SYSTEM_ROLES))
def test_the_kernel_matrix_for(role: str) -> None:
    """One role against every action and every resource shape.

    Mismatches are collected and reported together: a rule that is wrong is
    usually wrong for several cases at once, and failing on the first one hides
    the shape of the mistake.
    """
    checked = 0
    mismatches: list[str] = []

    for action, shape in itertools.product(ACTIONS, RESOURCE_SHAPES):
        checked += 1
        expected = design_says(role, action, shape)
        resource = None if shape is None else shape_resource(KIND_FOR_ACTION[action], shape)
        decision = can(principal(role), action, resource)
        if decision.allowed is not expected:
            clearance, department = shape or ("-", "-")
            mismatches.append(
                f"role={role} action={action} clearance={clearance} department={department}: "
                f"expected {expected}, got {decision.allowed} ({decision.detail})"
            )

    assert checked == len(ACTIONS) * len(RESOURCE_SHAPES)
    assert mismatches == [], "\n".join(mismatches)


# --- layer 2: row-level security over the restricted role --------------------

SCRATCH_DOCUMENTS = "scratch_documents"


@pytest.fixture
async def app_connection(settings: Settings) -> AsyncIterator[async_sessionmaker]:
    """Sessions bound to the restricted role, on the test database."""
    engine = create_async_engine(settings.runtime_test_database_url)
    try:
        yield async_sessionmaker(bind=engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def publish(session, **values: str) -> None:  # noqa: ANN001 - AsyncSession
    """The context the application publishes, written out by hand.

    Deliberately not `kernel.apply_rls_context`: a test that called the kernel's
    own function would prove the two agree about a *name* and nothing about what
    the database does with the value.
    """
    for name, value in values.items():
        await session.execute(
            text("SELECT set_config(:name, :value, true)"),
            {"name": name, "value": value},
        )


def array_literal(values) -> str:  # noqa: ANN001 - iterable of str
    """A Postgres array literal, quoted so an id cannot become two elements."""
    return "{" + ",".join(f'"{value}"' for value in values) + "}"


async def seed_withheld_details(platform: Platform) -> tuple[str, str]:
    """Two employees with withheld details, written as the owner.

    The owner can write them without a personnel context; that asymmetry is the
    point. The fixtures set state up, and the restricted role is what has to read
    it back under the rule.
    """
    first = await platform.employee()
    second = await platform.employee()
    for employee_id in (first, second):
        await platform.sql(
            """
            INSERT INTO employee_private (employee_id, address_line, postal_code)
            VALUES (:id, 'Calle Mayor 1', '28001')
            ON CONFLICT (employee_id) DO UPDATE SET address_line = 'Calle Mayor 1'
            """,
            {"id": employee_id},
        )
    return first, second


async def visible_private_ids(session) -> set[str]:  # noqa: ANN001 - AsyncSession
    rows = (await session.execute(text("SELECT employee_id FROM employee_private"))).scalars()
    return {str(value) for value in rows}


async def test_the_suite_reads_through_the_restricted_role(settings: Settings) -> None:
    """The separation itself, asserted rather than assumed.

    If the runtime connection were the owner, every row-level claim in this module
    would still pass on a developer's machine and mean nothing: a table's owner is
    exempt from its own policies.
    """
    assert settings.enforces_database_security
    assert settings.runtime_test_database_url != settings.test_database_url


async def test_without_a_published_context_a_protected_table_returns_no_rows(
    platform: Platform, app_connection: async_sessionmaker
) -> None:
    """A forgotten context reads as "no rows", never as "every row".

    This is the whole design of the second line of defence: the failure mode of a
    missing filter is silence, not disclosure.
    """
    first, _second = await seed_withheld_details(platform)

    async with app_connection() as session:
        mine = await session.scalar(
            text("SELECT count(*) FROM employee_private WHERE employee_id = :id"),
            {"id": first},
        )
        everything = await session.scalar(text("SELECT count(*) FROM employee_private"))

    assert (mine, everything) == (0, 0), (
        f"no context published, yet employee_private returned {mine} of the seeded row "
        f"and {everything} rows in total"
    )


async def test_with_a_context_the_restricted_role_sees_exactly_what_the_rule_allows(
    platform: Platform, app_connection: async_sessionmaker
) -> None:
    """The person, and the personnel roles, and nobody in between."""
    first, second = await seed_withheld_details(platform)

    async with app_connection() as session:
        await publish(session, **{"app.current_employee_id": second})
        own = await visible_private_ids(session)

    async with app_connection() as session:
        await publish(session, **{"app.is_privileged": "true"})
        privileged = await visible_private_ids(session)

    assert own == {second}, (
        f"a context naming one employee returned {sorted(own)} for "
        f"employees {sorted({first, second})}"
    )
    assert privileged == {first, second}


async def test_the_database_rule_is_coarser_than_the_product_rule(
    platform: Platform, app_connection: async_sessionmaker
) -> None:
    """An administrative context reads the row here; the product still withholds
    the fields.

    This is the honest boundary of row-level security, and it is asserted rather
    than glossed over. PostgreSQL applies the select policy to the rows an UPDATE
    reads, so a role that may not SELECT a row cannot UPDATE it either; keeping
    administrators out of the read clause made every administrative correction
    report "0 rows updated". The field-level rule stays in the kernel and in the
    visibility projection, where layer 4 checks it.

    **Not a licence to widen the policy.** The coarse direction is the safe one
    only here, where the columns are corrections an administrator legitimately
    writes; for documents the policy is deliberately *narrower* than §4.2, which
    the next tests assert.
    """
    await seed_withheld_details(platform)

    async with app_connection() as session:
        await publish(
            session,
            **{
                "app.current_employee_id": str(uuid4()),
                "app.current_roles": "{admin,employee}",
            },
        )
        visible = await visible_private_ids(session)

    assert len(visible) == 2, f"an administrative context saw {sorted(visible)}"


#: The rows the database rule has to answer for: §4.2's last three inputs, with
#: ownership as its own dimension.
DOCUMENT_ROWS = tuple(itertools.product(CLEARANCES, ("mine", "other"), ("mine", "other")))


def document_label(clearance: str, department: str, owner: str) -> str:
    return f"{clearance}|{department}|{owner}"


def rls_says(caller_clearance: str, clearance: str, department: str, owner: str) -> bool:
    """§4.2's first two clauses — the two a row predicate can express.

    Clause 1 is ownership, whatever the classification. Clause 2 is the company
    document clause: within the ceiling *and* in a department the caller reaches.

    The exception roles and explicit shares are deliberately absent: a predicate
    cannot see `document_permissions`, and the roles a caller holds are the
    kernel's question. A row-level policy is the backstop *under* the application's
    rule, so it is allowed to be narrower and must never be wider.
    """
    if owner == "mine":
        return True
    return (
        CLEARANCE_RANK[clearance] <= CLEARANCE_RANK[caller_clearance]
        and department == "mine"
    )


@asynccontextmanager
async def scratch_documents(settings: Settings) -> AsyncIterator[AsyncConnection]:
    """A `documents` table that exists only inside a transaction that is rolled back.

    Ticket 31 creates the real table. The rule it will be governed by is already
    in the database, so a table with the same three columns and that predicate as
    its policy is the only way to assert §4.2 against a real policy before the
    documents module exists.

    The table is created on the connection the migrations use, so it is owned by
    `eam`, and every assertion runs after `SET LOCAL ROLE eam_app` — a table's
    owner is exempt from its own policies, and a test that read this one as the
    owner would exercise no policy at all. `SET LOCAL` is transaction-scoped and
    the transaction is rolled back, so neither the table nor the role outlives the
    test.
    """
    engine = create_async_engine(settings.test_database_url)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(
                    text(
                        f"""
                        CREATE TABLE {SCRATCH_DOCUMENTS} (
                            label text PRIMARY KEY,
                            owner_employee_id uuid,
                            department_id uuid,
                            clearance_level text NOT NULL
                        )
                        """
                    )
                )
                for clearance, department, owner in DOCUMENT_ROWS:
                    await connection.execute(
                        text(
                            f"""
                            INSERT INTO {SCRATCH_DOCUMENTS}
                                (label, owner_employee_id, department_id, clearance_level)
                            VALUES (:label, :owner, :department, :clearance)
                            """
                        ),
                        {
                            "label": document_label(clearance, department, owner),
                            "owner": MY_EMPLOYEE if owner == "mine" else OTHER_EMPLOYEE,
                            "department": (
                                MY_DEPARTMENT if department == "mine" else OTHER_DEPARTMENT
                            ),
                            "clearance": clearance,
                        },
                    )

                await connection.execute(
                    text(f"ALTER TABLE {SCRATCH_DOCUMENTS} ENABLE ROW LEVEL SECURITY")
                )
                # The policy ticket 31's migration will attach, against the same
                # three columns.
                await connection.execute(
                    text(
                        f"""
                        CREATE POLICY {SCRATCH_DOCUMENTS}_read ON {SCRATCH_DOCUMENTS}
                        FOR SELECT USING (
                            document_visibility_predicate(
                                owner_employee_id, department_id, clearance_level
                            )
                        )
                        """
                    )
                )

                # Migration 0007's ALTER DEFAULT PRIVILEGES is what makes a table a
                # later migration adds readable by the runtime role without another
                # grant statement — the claim its comment makes, asserted here.
                readable = await connection.scalar(
                    text("SELECT has_table_privilege(:role, :table, 'SELECT')"),
                    {"role": APP_ROLE, "table": SCRATCH_DOCUMENTS},
                )
                assert readable is True, (
                    f"{APP_ROLE} cannot read a table created now; a later migration's "
                    "table would be invisible to every request"
                )

                await connection.execute(text(f"SET LOCAL ROLE {APP_ROLE}"))
                who = await connection.scalar(text("SELECT current_user"))
                superuser = await connection.scalar(text("SELECT current_setting('is_superuser')"))
                assert (who, superuser) == (APP_ROLE, "off"), (
                    f"the policy assertions would run as {who} (superuser={superuser}), "
                    "which bypasses row-level security"
                )
                yield connection
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


async def publish_document_context(
    connection: AsyncConnection,
    *,
    employee_id: UUID,
    clearance_levels: frozenset[str],
    department_ids: frozenset[UUID],
) -> None:
    """The context a request publishes before it touches a document table."""
    for name, value in (
        ("app.current_employee_id", str(employee_id)),
        ("app.clearance_levels", array_literal(sorted(clearance_levels))),
        ("app.department_ids", array_literal(sorted(str(value) for value in department_ids))),
    ):
        await connection.execute(
            text("SELECT set_config(:name, :value, true)"), {"name": name, "value": value}
        )


async def predicate_allows(
    connection: AsyncConnection,
    *,
    owner: UUID,
    department: UUID | None,
    clearance: str,
) -> bool:
    """`document_visibility_predicate` asked directly, the way a policy asks it."""
    return bool(
        await connection.scalar(
            text("SELECT document_visibility_predicate(:owner, :department, :clearance)"),
            {"owner": owner, "department": department, "clearance": clearance},
        )
    )


def clearances_up_to(level: str) -> frozenset[str]:
    return frozenset(name for name in CLEARANCES if CLEARANCE_RANK[name] <= CLEARANCE_RANK[level])


async def test_the_document_policy_hides_every_row_without_a_context(
    settings: Settings,
) -> None:
    """The same silence as `employee_private`, on the table ticket 31 will add."""
    async with scratch_documents(settings) as connection:
        visible = await connection.scalar(text(f"SELECT count(*) FROM {SCRATCH_DOCUMENTS}"))
        present = await connection.scalar(
            text(f"SELECT count(*) FROM {SCRATCH_DOCUMENTS} WHERE label = 'low|mine|other'")
        )

    assert (visible, present) == (0, 0), (
        f"no context published, yet the documents policy returned {visible} rows "
        f"({present} of them a row that should need a department)"
    )


async def test_the_database_applies_the_document_rule_to_every_combination(
    settings: Settings,
) -> None:
    """§4.2 at the database layer: caller clearance × document clearance ×
    department × ownership, all 36 combinations, against a real policy.

    A table rather than a function call, because the claim is that PostgreSQL
    *enforces* the rule: the predicate could be right while the policy was never
    attached, and only a query proves otherwise.
    """
    checked = 0
    mismatches: list[str] = []

    async with scratch_documents(settings) as connection:
        for caller_clearance in CLEARANCES:
            await publish_document_context(
                connection,
                employee_id=MY_EMPLOYEE,
                clearance_levels=clearances_up_to(caller_clearance),
                department_ids=frozenset({MY_DEPARTMENT}),
            )
            visible = set(
                (await connection.execute(text(f"SELECT label FROM {SCRATCH_DOCUMENTS}")))
                .scalars()
                .all()
            )

            for clearance, department, owner in DOCUMENT_ROWS:
                checked += 1
                expected = rls_says(caller_clearance, clearance, department, owner)
                label = document_label(clearance, department, owner)
                seen = label in visible
                if seen is not expected:
                    mismatches.append(
                        f"caller={caller_clearance} document={clearance} department={department} "
                        f"owner={owner}: expected visible={expected}, the policy returned "
                        f"visible={seen}"
                    )

    assert checked == len(CLEARANCES) * len(CLEARANCES) * 2 * 2
    assert mismatches == [], "\n".join(mismatches)


# --- layer 3: the HTTP matrix ------------------------------------------------

#: Every endpoint that exists today, with the action it performs. `None` is an
#: endpoint that names no action because it answers about the caller alone.
HTTP_MATRIX: tuple[tuple[str, str, Action | None], ...] = (
    ("GET", "/api/v1/departments", Action.DEPARTMENT_READ),
    ("POST", "/api/v1/departments", Action.DEPARTMENT_MANAGE),
    ("GET", "/api/v1/positions", Action.POSITION_READ),
    ("POST", "/api/v1/positions", Action.POSITION_MANAGE),
    ("GET", "/api/v1/employees/directory", Action.EMPLOYEE_DIRECTORY),
    ("GET", "/api/v1/employees/me", None),
    ("GET", "/api/v1/employees/{subject}", Action.EMPLOYEE_READ),
    ("POST", "/api/v1/employees", Action.EMPLOYEE_MANAGE),
    ("GET", "/api/v1/accounts", Action.ACCOUNT_LIST),
    ("POST", "/api/v1/accounts", Action.ACCOUNT_MANAGE),
    ("GET", "/api/v1/audit-log", Action.AUDIT_READ),
    ("GET", "/api/v1/auth/session", None),
    # Attendance (ticket 21). Every role may punch its own clock and read its own
    # record; naming somebody else is a refusal the matrix's layer 4 covers by
    # name, because it is a resource-level decision rather than a role one.
    ("POST", "/api/v1/attendance/clock", Action.ATTENDANCE_CLOCK_OWN),
    ("GET", "/api/v1/attendance/day", Action.ATTENDANCE_READ_OWN),
    ("GET", "/api/v1/attendance/range", Action.ATTENDANCE_READ_OWN),
    # Ticket 24. The four routes above answer about the caller and are self-only;
    # these are the surfaces that can name somebody else, and the route-level guard
    # is "signed in" — which of the three attendance actions applies is a fact about
    # the caller and the subject, so the kernel is asked inside the handler and the
    # rows here record the action the request turns out to perform: the caller's own.
    # The refusal a manager gets for a colleague, and HR's reach, are asserted by
    # name in `test_attendance_corrections.py`.
    ("GET", "/api/v1/attendance/punches", Action.ATTENDANCE_READ_OWN),
    ("GET", "/api/v1/attendance/export", Action.ATTENDANCE_READ_OWN),
    ("POST", "/api/v1/attendance/corrections", Action.ATTENDANCE_CORRECTION_OWN),
    ("GET", "/api/v1/attendance/corrections", Action.ATTENDANCE_READ_OWN),
    (
        "GET",
        "/api/v1/attendance/corrections/{correction_id}",
        Action.ATTENDANCE_READ_OWN,
    ),
    (
        "PATCH",
        "/api/v1/attendance/corrections/{correction_id}",
        Action.ATTENDANCE_CORRECTION_OWN,
    ),
    (
        "POST",
        "/api/v1/attendance/corrections/{correction_id}/submit",
        Action.ATTENDANCE_CORRECTION_OWN,
    ),
    (
        "POST",
        "/api/v1/attendance/corrections/{correction_id}/decide",
        Action.SESSION_READ_OWN,
    ),
    # Projects (ticket 27). `manager` is in `project.manage`'s role list because a
    # project manager manages *their* project, and the row above is somebody else's
    # — which layer 4 asserts by name. Reading the catalogue is open to every role.
    ("GET", "/api/v1/projects", Action.PROJECT_READ),
    ("GET", "/api/v1/projects/selectable", Action.PROJECT_READ),
    ("POST", "/api/v1/projects", Action.PROJECT_MANAGE),
    ("GET", "/api/v1/projects/{project_id}", Action.PROJECT_READ),
    ("PATCH", "/api/v1/projects/{project_id}", Action.PROJECT_MANAGE),
    ("POST", "/api/v1/projects/{project_id}/tasks", Action.PROJECT_TASK_MANAGE),
    ("PATCH", "/api/v1/projects/{project_id}/tasks/{task_id}", Action.PROJECT_TASK_MANAGE),
    (
        "POST",
        "/api/v1/projects/{project_id}/tasks/{project_id}/deactivate",
        Action.PROJECT_TASK_MANAGE,
    ),
    ("POST", "/api/v1/projects/{project_id}/record-time", Action.PROJECT_TASK_READ),
    # Schedules and holidays (ticket 22). Reading your own week is self-service for
    # every role and self-only (layer 4 asserts the refusal by name, as it does for
    # attendance); the calendar is published; maintaining either is HR and
    # administration. The patch and delete endpoints are absent deliberately: their
    # paths name a row that does not exist in this fixture, so a 404 would be
    # indistinguishable from the 200 the matrix expects — `test_schedules.py`
    # asserts those three by role instead.
    ("GET", "/api/v1/schedules", Action.SCHEDULE_MANAGE),
    ("POST", "/api/v1/schedules", Action.SCHEDULE_MANAGE),
    ("GET", "/api/v1/schedules/mine", Action.SCHEDULE_READ_OWN),
    ("GET", "/api/v1/schedules/expected-hours", Action.SCHEDULE_READ_OWN),
    (
        "POST",
        "/api/v1/schedules/expected-hours/snapshots",
        Action.SCHEDULE_MANAGE,
    ),
    ("GET", "/api/v1/holidays", Action.HOLIDAY_READ),
    ("POST", "/api/v1/holidays", Action.HOLIDAY_MANAGE),
    # Weekly timesheets (ticket 28). Every role may read and write *its own* week, so
    # the role-level answer here is "yes" for all seven and the interesting refusal —
    # somebody else's week, which the ticket says is a 403 — is a resource-level
    # decision layer 4 asserts by name. The week is a query parameter (see the router:
    # this is the convention ticket 21 established for a surface that answers about
    # the caller), and `{task_id}` stands in for the entry id.
    ("GET", "/api/v1/timesheets/week", Action.TIMESHEET_READ_OWN),
    ("GET", "/api/v1/timesheets/week/status", Action.TIMESHEET_READ_OWN),
    ("GET", "/api/v1/timesheets/mine", Action.TIMESHEET_READ_OWN),
    ("POST", "/api/v1/timesheets/entries", Action.TIMESHEET_WRITE_OWN),
    ("PATCH", "/api/v1/timesheets/entries/{task_id}", Action.TIMESHEET_WRITE_OWN),
    ("DELETE", "/api/v1/timesheets/entries/{task_id}", Action.TIMESHEET_WRITE_OWN),
    ("POST", "/api/v1/timesheets/copy-previous", Action.TIMESHEET_WRITE_OWN),
    ("POST", "/api/v1/timesheets/submit", Action.TIMESHEET_SUBMIT_OWN),
)


def holiday_date(suffix: str) -> date:
    """A holiday date derived from a fresh uuid, so two calls cannot collide.

    The uniqueness rule is `(date, scope, region)`, so a fixed date would make the
    second role that may add one receive a 409 — which the matrix would report as a
    permission failure. Far-future years keep these rows away from the calendars the
    other tests build.
    """
    return date(
        2100 + int(suffix[0:2], 16) % 90,
        1 + int(suffix[2:4], 16) % 12,
        1 + int(suffix[4:6], 16) % 28,
    )


#: The week the timesheet rows are about. A Monday, so the module's week key is valid,
#: and fixed rather than derived from "today" so the payload and the path always agree
#: about which week they mean — the failure a computed date would produce is a 400
#: that reads like a permission problem.
MATRIX_WEEK_START = date(2027, 1, 4)


#: The routes whose body the matrix can make genuinely valid, so a permitted caller
#: is expected to reach the *handler's* answer (200/201) rather than the 404 a row the
#: matrix never created would produce.
#:
#: Everything else is checked for the refusal only. A body naming a row that does not
#: exist cannot distinguish a wired endpoint from an unwired one — a 404 proves the
#: guard let the caller through but says nothing about the handler — and
#: `test_projects.py` asserts what those routes do with rows that exist.
RESOURCE_FREE_ROUTES: frozenset[str] = frozenset(
    {
        "/api/v1/departments",
        "/api/v1/positions",
        "/api/v1/employees",
        "/api/v1/employees/{subject}",
        "/api/v1/accounts",
        "/api/v1/attendance/clock",
        "/api/v1/attendance/day",
        "/api/v1/attendance/range",
        # Ticket 24. The three reads answer about the caller with no parameters at
        # all, and the correction draft is a request about the caller's own punch —
        # so all four have an answer this layer can assert exactly. The routes that
        # name a correction id cannot: the row does not exist in this fixture, and a
        # 404 proves the handler is wired but says nothing about its body.
        "/api/v1/attendance/punches",
        "/api/v1/attendance/export",
        "/api/v1/attendance/corrections",
        "/api/v1/projects",
        "/api/v1/projects/selectable",
    }
)


def http_payload(path: str, *, department: str, employee: str) -> dict:
    """A body that would succeed if the permission allowed it.

    Deliberately valid — a real department, a real position, a real employee with
    no account yet — so a 403 can only mean the permission was refused. A payload
    that failed validation would make the two indistinguishable.

    The bodies that name a *task* are the exception, and the exception is the point:
    a task id cannot be invented, because it has to be a row in a project that
    exists. Those routes are in `RESOURCE_FREE_ROUTES` below, so the matrix claims
    nothing about their bodies — only that the guard lets a permitted caller past.
    """
    suffix = uuid4().hex[:8]
    return {
        "/api/v1/departments": {
            "code": f"mx{suffix}",
            "name_es": "Matriz",
            "name_en": "Matrix",
        },
        "/api/v1/positions": {
            "code": f"px{suffix}",
            "title_es": "Puesto",
            "title_en": "Position",
            "department_id": department,
        },
        "/api/v1/employees": {
            "first_name": "Matriz",
            "last_name": "Prueba",
            "email": f"mx{suffix}@empresa.es",
            "hire_date": "2024-01-15",
        },
        "/api/v1/accounts": {"employee_id": employee, "username": f"mx{suffix}"},
        "/api/v1/attendance/clock": {"kind": "clock_in"},
        # Ticket 24. A real request about the caller: a day that has happened, an
        # instant that has, and a reason. The caller has punched nothing, so the
        # flow takes it as a punch to make up — which is the ordinary case for a
        # forgotten clock_out and the one this body exercises.
        "/api/v1/attendance/corrections": {
            "business_date": (date.today() - timedelta(days=30)).isoformat(),
            "kind": "clock_out",
            "corrected_at": datetime.combine(
                date.today() - timedelta(days=30), time(9, 0), tzinfo=UTC
            ).isoformat(),
            "reason": "Matriz",
        },
        "/api/v1/attendance/corrections/{correction_id}": {"reason": "Matriz dos"},
        "/api/v1/attendance/corrections/{correction_id}/submit": {},
        "/api/v1/attendance/corrections/{correction_id}/decide": {"decision": "approve"},
        # Ticket 27. `suffix` is fresh per call, so repeated runs of the matrix in
        # one test cannot collide on the project code — which is unique for good.
        "/api/v1/projects": {
            "code": f"mx{suffix}",
            "name_es": "Matriz",
            "name_en": "Matrix",
            "department_id": department,
            "start_date": "2026-01-01",
        },
        "/api/v1/projects/{project_id}": {"name_es": "Matriz dos"},
        "/api/v1/projects/{project_id}/tasks": {
            "code": f"t{suffix}",
            "name_es": "Tarea",
            "name_en": "Task",
        },
        "/api/v1/projects/{project_id}/tasks/{task_id}": {"name_es": "Tarea dos"},
        "/api/v1/projects/{project_id}/tasks/{project_id}/deactivate": {},
        "/api/v1/projects/{project_id}/record-time": {"task_id": employee},
        # Ticket 22. The schedule code is fresh per call for the same reason the
        # project code is: it is unique for good. The schedule names no department
        # and is not the default, so running the matrix once per role that may
        # cannot collide with itself.
        "/api/v1/schedules": {
            "code": f"mx{suffix}",
            "name_es": "Matriz",
            "name_en": "Matrix",
            "days": [
                {
                    "weekday": 0,
                    "expected_minutes": 480,
                    "start_time": "08:00",
                    "end_time": "16:00",
                }
            ],
        },
        "/api/v1/schedules/expected-hours/snapshots": {
            "year": 2026,
            "month": 3,
            "employee_id": employee,
        },
        # A holiday date nobody has used. A fixed one would collide on the second
        # run — admin and then hr — and a 409 would read as a permission failure.
        # The year is far enough out that nothing else in the suite touches it.
        "/api/v1/holidays": {
            "date": holiday_date(suffix).isoformat(),
            "name_es": "Matriz",
            "name_en": "Matrix",
            "scope": "national",
        },
        # Ticket 28. The project and task ids are real uuids the matrix never creates,
        # which is the ordinary shape here: the guard lets the caller through and the
        # handler answers 404 — what this layer asserts is the guard. The entry's day
        # is `MATRIX_WEEK_START` itself, and the week travels in the query string, so
        # the two always agree about which week is meant.
        "/api/v1/timesheets/entries": {
            "entry_date": MATRIX_WEEK_START.isoformat(),
            "project_id": employee,
            "task_id": employee,
            "minutes": 480,
        },
        "/api/v1/timesheets/entries/{task_id}": {"minutes": 60},
        "/api/v1/timesheets/copy-previous": {},
        "/api/v1/timesheets/submit": {},
    }[path]


@pytest.mark.parametrize("role", sorted(SYSTEM_ROLES))
async def test_the_http_matrix_for(platform: Platform, role: str) -> None:
    """Each role against each endpoint, end to end.

    The kernel's matrix is exhaustive; this one proves the endpoints are wired to
    it, which is the part a unit test cannot see. A refusal must carry a
    *catalogued* code: the client routes on the code, and an uncatalogued 403 is a
    client that cannot tell "you may not" from "that was malformed".
    """
    actor = await platform.account(roles=(role,))
    subject = await platform.employee()
    department = await platform.department("matriz")
    accountless = await platform.employee()

    checked = 0
    failures: list[str] = []

    for method, template, action in HTTP_MATRIX:
        path = template.format(
            subject=actor.employee_id if action in SELF_ONLY_ACTIONS else subject,
            project=subject,
            project_id=subject,
            task_id=subject,
            correction_id=subject,
        )
        payload = (
            http_payload(template, department=department, employee=accountless)
            if method in {"POST", "PATCH", "PUT"}
            else None
        )
        # The timesheet surface names its week in the query string, as ticket 21's
        # routes do for a surface that answers about the caller: the week is a date
        # rather than a row id, and this is the one layer that has to supply it.
        params = {"week": MATRIX_WEEK_START.isoformat()} if "/timesheets/" in path else None
        response = (
            await actor.call(method, path, json=payload, params=params)
            if payload is not None
            else await actor.call(method, path, params=params)
        )
        checked += 1
        label = f"role={role} {method} {path} (action={action or 'none'})"

        if action is not None and not may(role, action):
            # The refusal, and its code: the client routes on the code, and an
            # uncatalogued 403 is a client that cannot tell "you may not" from "that
            # was malformed".
            if response.status_code != 403:
                failures.append(f"{label}: expected 403, got {response.status_code} — "
                                f"{response.text[:300]}")
            elif response.json().get("error", {}).get("code") != ErrorCode.FORBIDDEN.value:
                failures.append(
                    f"{label}: refused with {response.json()['error'].get('code')!r}, not "
                    f"{ErrorCode.FORBIDDEN.value}"
                )
            continue

        # Permitted: the guard let the call through, which is what this layer is
        # about. A 403 or a 5xx would mean the endpoint is not wired to the kernel —
        # and nothing else counts as a failure, because several routes name a row the
        # matrix cannot create (the project id is a fresh uuid per row) and answer 404
        # for a reason that has nothing to do with permission. `test_projects.py`
        # asserts what those routes do with a row that exists.
        expected = 200 if action is None else (201 if method == "POST" else 200)
        if response.status_code == 403 or response.status_code >= 500:
            failures.append(
                f"{label}: expected the action to be permitted ({expected}), got "
                f"{response.status_code} — {response.text[:300]}"
            )
        elif template in RESOURCE_FREE_ROUTES and response.status_code != expected:
            failures.append(
                f"{label}: expected {expected}, got {response.status_code} — "
                f"{response.text[:300]}"
            )

    assert checked == len(HTTP_MATRIX)
    assert failures == [], "\n".join(failures)


async def test_an_unauthenticated_request_reaches_no_endpoint(platform: Platform) -> None:
    """No cookie at all: 401 with the session error code, never a list."""
    failures: list[str] = []
    for method, template, _action in HTTP_MATRIX:
        path = template.format(
            subject=uuid4(), project=uuid4(), project_id=uuid4(), task_id=uuid4(),
            correction_id=uuid4(),
        )
        response = await platform.client.request(method, path)
        if (response.status_code, response.json()["error"]["code"]) != (
            401,
            ErrorCode.SESSION_INVALID.value,
        ):
            failures.append(f"{method} {path}: {response.status_code} {response.text[:200]}")

    assert failures == [], "\n".join(failures)


async def test_a_filtered_collection_omits_the_field_rather_than_blanking_it(
    platform: Platform,
) -> None:
    """Withheld means absent, not present-and-empty.

    A key that is present with a null value is indistinguishable from "not
    recorded", which turns a refusal into a data-quality question — and the
    *value* must not travel in the body at all, in any field, including one
    nobody thought about.
    """
    mine = await platform.department("mio")
    other = await platform.department("otro")
    viewer = await platform.account(roles=("employee",))
    await platform.assign(viewer.employee_id, mine, await platform.position(mine, "a"))
    colleague = await platform.account(roles=("employee",))
    await platform.assign(colleague.employee_id, mine, await platform.position(mine, "b"))
    outsider = await platform.account(roles=("employee",))
    await platform.assign(outsider.employee_id, other, await platform.position(other, "c"))

    response = await viewer.get("/api/v1/employees/directory")

    assert response.status_code == 200, response.text
    rows = {row["employee_id"]: row for row in response.json()}
    assert rows[colleague.employee_id]["email"] == colleague.email
    assert "email" not in rows[outsider.employee_id], (
        "a colleague outside the viewer's departments came back with an email key: "
        f"{rows[outsider.employee_id]}"
    )
    assert outsider.email not in response.text, (
        "the withheld address travelled in the response body"
    )


async def test_a_refused_collection_carries_none_of_the_rows_it_refused(
    platform: Platform,
) -> None:
    """A 403 that leaked a username or an audit record would be worse than a list.

    The refusal has to be a refusal, not a filtered payload with a warning
    attached, and not a body that names what was protected.
    """
    admin = await platform.admin(email="jefa@empresa.es")
    actor = await platform.account(roles=("employee",))

    accounts = await actor.get("/api/v1/accounts")
    assert accounts.status_code == 403
    assert accounts.json()["error"]["code"] == ErrorCode.FORBIDDEN.value
    assert admin.username not in accounts.text, "the refusal body named an account"
    assert admin.email not in accounts.text

    trail = await actor.get("/api/v1/audit-log")
    assert trail.status_code == 403
    assert trail.json()["error"]["code"] == ErrorCode.FORBIDDEN.value
    assert "occurred_at" not in trail.text, "the refusal body carried audit records"
    assert '"items"' not in trail.text


# --- layer 4: the escalations worth naming -----------------------------------


async def test_a_medium_clearance_document_of_my_own_department_is_still_refused(
    settings: Settings,
) -> None:
    """Prevents "same department" being read as "same clearance".

    Being in the department gets you the department's documents, not the ones
    classified above you. Ticket 31 adds the endpoint that returns the refusal;
    the decision and the database rule exist now.
    """
    decision = can(
        principal("employee", clearance="low"),
        Action.DOCUMENT_READ,
        Resource(
            ResourceKind.DOCUMENT,
            department_id=MY_DEPARTMENT,
            clearance="medium",
            is_company_kb=True,
        ),
    )

    assert decision.denied, decision.detail
    assert Reason.CLEARANCE_TOO_LOW in decision.reasons

    async with scratch_documents(settings) as connection:
        await publish_document_context(
            connection,
            employee_id=MY_EMPLOYEE,
            clearance_levels=clearances_up_to("low"),
            department_ids=frozenset({MY_DEPARTMENT}),
        )
        allowed = await predicate_allows(
            connection,
            owner=OTHER_EMPLOYEE,
            department=MY_DEPARTMENT,
            clearance="medium",
        )
        readable = await predicate_allows(
            connection,
            owner=OTHER_EMPLOYEE,
            department=MY_DEPARTMENT,
            clearance="low",
        )

    assert allowed is False, "the database let through a document above the caller's clearance"
    assert readable is True, "the database refused a document the caller may read"


async def test_a_medium_clearance_employee_cannot_read_a_finance_document(
    settings: Settings,
) -> None:
    """Prevents "cleared for medium" being read as "cleared for the company".

    §4.2 needs the department as well as the ceiling, and the exception belongs to
    hr and compliance alone — finance is privileged for payroll, which is a
    different rule for a different question. Ticket 31 adds the endpoint.
    """
    finance_document = Resource(
        ResourceKind.DOCUMENT,
        department_id=FINANCE_DEPARTMENT,
        clearance="medium",
        is_company_kb=True,
    )
    decision = can(
        principal("employee", clearance="medium"), Action.DOCUMENT_READ, finance_document
    )

    assert decision.denied, decision.detail
    assert Reason.DEPARTMENT_NOT_REACHABLE in decision.reasons

    async with scratch_documents(settings) as connection:
        await publish_document_context(
            connection,
            employee_id=MY_EMPLOYEE,
            clearance_levels=clearances_up_to("medium"),
            department_ids=frozenset({MY_DEPARTMENT}),
        )
        assert await predicate_allows(
            connection,
            owner=OTHER_EMPLOYEE,
            department=FINANCE_DEPARTMENT,
            clearance="medium",
        ) is False

        # The backstop's direction, recorded: it is narrower than the application
        # rule, never wider. The very same document is allowed for hr by §4.2
        # clause 4, and the predicate above refuses it because a row predicate
        # cannot see the roles a caller holds.
        assert can(
            principal("hr", clearance="medium"), Action.DOCUMENT_READ, finance_document
        ).allowed


async def test_a_manager_cannot_read_a_non_reports_attendance(
    platform: Platform, app_connection: async_sessionmaker
) -> None:
    """Prevents "manager" being read as "manager of everybody".

    §4.1 gives a managerial position their direct reports and nothing wider, which
    is what makes a manager in one team unable to browse another. Attendance
    arrives in ticket 21; what exists today is the rule that will govern it —
    `can()` on an employee resource, and the row-level policy over the table the
    sensitive personnel columns live in.
    """
    report = Resource(
        ResourceKind.EMPLOYEE, department_id=OTHER_DEPARTMENT, owner_employee_id=REPORT_EMPLOYEE
    )
    stranger = Resource(
        ResourceKind.EMPLOYEE, department_id=OTHER_DEPARTMENT, owner_employee_id=OTHER_EMPLOYEE
    )
    manager = principal("manager", reports=(REPORT_EMPLOYEE,))

    assert can(manager, Action.EMPLOYEE_READ, report).allowed

    refused = can(manager, Action.EMPLOYEE_READ, stranger)
    assert refused.denied, f"a manager reached a non-report: {refused.detail}"
    assert Reason.DEPARTMENT_NOT_REACHABLE in refused.reasons

    # Ticket 24's version of the same line, and the one a department would have got
    # wrong: the attendance read is decided by the reporting relationship, so a
    # manager reaches their report's record and is refused a colleague's — including
    # one who sits in their own department, which the generated resource above
    # deliberately does not distinguish.
    assert can(manager, Action.ATTENDANCE_READ_REPORT, report).allowed
    attendance_refusal = can(manager, Action.ATTENDANCE_READ_REPORT, stranger)
    assert attendance_refusal.denied, f"a manager read a non-report's hours: {attendance_refusal}"
    assert attendance_refusal.primary_reason is Reason.NOT_MANAGER_OF_SUBJECT

    first, second = await seed_withheld_details(platform)
    async with app_connection() as session:
        await publish(
            session,
            **{
                "app.current_employee_id": first,
                "app.current_roles": "{manager,employee}",
            },
        )
        visible = await visible_private_ids(session)

    assert visible == {first}, (
        f"a manager's context read {sorted(visible)} of employees {sorted({first, second})}"
    )


async def test_an_ordinary_employee_cannot_read_somebody_elses_salary(
    platform: Platform, app_connection: async_sessionmaker
) -> None:
    """Prevents the profile endpoint quietly becoming the payroll endpoint.

    Pay data is withheld by role, and there is no resource — not a colleague, not
    the caller's own department — that makes it readable for an ordinary employee.
    Salary records arrive in ticket 43; what exists today is the field-level rule
    (`employee.read_withheld`, where an address and a staff number already live)
    and the row-level policy over that table.
    """
    colleague = Resource(
        ResourceKind.EMPLOYEE, department_id=MY_DEPARTMENT, owner_employee_id=OTHER_EMPLOYEE
    )

    mine = can(principal("employee"), Action.EMPLOYEE_READ, colleague)
    refused = can(principal("employee"), Action.EMPLOYEE_READ_WITHHELD, colleague)

    assert mine.allowed, "the colleague's profile is readable inside the department"
    assert refused.denied, refused.detail
    assert refused.primary_reason is Reason.PRIVILEGED_ROLE_REQUIRED
    assert can(principal("hr"), Action.EMPLOYEE_READ_WITHHELD, colleague).allowed
    assert can(principal("admin"), Action.EMPLOYEE_READ_WITHHELD, colleague).denied

    first, second = await seed_withheld_details(platform)
    async with app_connection() as session:
        await publish(
            session,
            **{"app.current_employee_id": first, "app.current_roles": "{employee}"},
        )
        own = await visible_private_ids(session)

    async with app_connection() as session:
        await publish(
            session,
            **{"app.current_employee_id": first, "app.current_roles": "{hr,employee}"},
        )
        as_hr = await visible_private_ids(session)

    assert own == {first}, (
        f"an employee's context read {sorted(own)} of employees {sorted({first, second})}"
    )
    assert as_hr == {first, second}, "the personnel context is the control for the line above"


async def test_a_non_compliance_role_cannot_read_the_audit_trail(platform: Platform) -> None:
    """Prevents the arrangement audit exists to avoid: the administrator who is
    the subject of half the trail also being its reader.

    End to end, because the endpoint and the records exist: every role but
    compliance is refused, and the refusal carries none of what it refused.
    """
    await platform.admin()
    failures: list[str] = []

    for role in sorted(SYSTEM_ROLES - {"compliance"}):
        actor = await platform.account(roles=(role,))
        response = await actor.get("/api/v1/audit-log")
        if response.status_code != 403:
            failures.append(f"role={role} read the audit trail: {response.status_code}")
        elif response.json()["error"]["code"] != ErrorCode.FORBIDDEN.value:
            failures.append(f"role={role} was refused with {response.json()['error']['code']}")
        elif "occurred_at" in response.text:
            failures.append(f"role={role} was refused, and received audit records anyway")
        await actor.close()

    assert failures == [], "\n".join(failures)

    reader = await platform.account(roles=("compliance",))
    allowed = await reader.get("/api/v1/audit-log")

    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["total"] > 0, "the control: compliance reads a trail that has records"
    assert can(principal("compliance"), Action.AUDIT_READ).allowed


async def test_an_administrator_cannot_read_withheld_employee_details(
    platform: Platform,
) -> None:
    """Prevents the reflex that "administrator" means "may see everything".

    §4.1 separates the duties: administration configures the system, and the
    withheld block — address, staff number, emergency contact, and with them the
    payroll material — belongs to hr, finance and compliance.

    End to end in both directions. The administrator is put *inside* the subject's
    department, so the refusal cannot be explained away as an outsider's minimal
    projection; and the person themselves receives the same fields, so the absence
    is a decision and not a serializer that never fills the field.
    """
    department = await platform.department("sistemas")
    subject = await platform.employee(
        private={"address_line": "Calle Oculto 7", "postal_code": "28001", "employee_no": "E-0001"}
    )
    await platform.assign(subject, department, await platform.position(department, "tecnico"))

    admin = await platform.account(roles=("admin",))
    await platform.assign(
        admin.employee_id, department, await platform.position(department, "jefe")
    )

    response = await admin.get(f"/api/v1/employees/{subject}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["visibility"] == "directory", (
        f"expected a colleague's projection, got {body['visibility']}: the refusal below "
        "would otherwise be an outsider's minimal view rather than a withheld field"
    )
    assert "private" not in body, f"an administrator received the withheld block: {body}"
    assert "hire_date" not in body
    assert "Calle Oculto 7" not in response.text
    assert "E-0001" not in response.text

    decision = can(principal("admin"), Action.EMPLOYEE_READ_WITHHELD)
    assert decision.denied, decision.detail
    assert decision.primary_reason is Reason.PRIVILEGED_ROLE_REQUIRED

    self_service = await platform.account(roles=("employee",))
    written = await admin.put(
        f"/api/v1/employees/{self_service.employee_id}/private",
        json={"address_line": "Calle Propia 3"},
    )
    assert written.status_code == 200, written.text

    own = await self_service.get("/api/v1/employees/me")

    assert own.status_code == 200, own.text
    assert own.json()["private"]["address_line"] == "Calle Propia 3", (
        "the endpoint does serve withheld fields when the rule allows them; the "
        "administrator's response above is the rule refusing, not a broken projection"
    )


# --- layer 5: cache invalidation through the request path --------------------


async def principal_of(platform: Platform, user_id: str):
    """The principal the kernel would build for this account, cache included."""
    from app.domain.access.snapshot import resolve_principal

    async with platform.factory() as session:
        return await resolve_principal(session, UUID(user_id))


async def test_a_department_change_moves_access_on_the_very_next_request(
    platform: Platform,
) -> None:
    """Access must follow the assignment immediately, not after a five-minute TTL.

    The mistake this prevents is a snapshot that outlives the fact it describes:
    somebody moved into a team keeps reading that team's profiles, or somebody
    moved out keeps reading their old colleagues'. Both moves here are real writes
    through the endpoints, and the assertions are on the next request each time.
    """
    first = await platform.department("primero")
    second = await platform.department("segundo")
    viewer = await platform.account(roles=("employee",))
    await platform.assign(viewer.employee_id, first, await platform.position(first, "a"))
    outsider = await platform.account(roles=("employee",))
    await platform.assign(outsider.employee_id, second, await platform.position(second, "b"))

    before = await viewer.get(f"/api/v1/employees/{outsider.employee_id}")
    assert before.json()["visibility"] == "minimal", before.text

    await platform.assign(viewer.employee_id, second, await platform.position(second, "c"))
    active = await platform.scalar(
        "SELECT count(*) FROM employee_assignments WHERE employee_id = :id AND end_date IS NULL",
        {"id": viewer.employee_id},
    )
    assert active == 2, "the second assignment has to be real for the move to be real"

    after = await viewer.get(f"/api/v1/employees/{outsider.employee_id}")

    assert after.json()["visibility"] == "directory", (
        "the snapshot still described the departments from before the write"
    )
    assert after.json()["email"] == outsider.email

    assignment_id = await platform.scalar(
        "SELECT id FROM employee_assignments "
        "WHERE employee_id = :id AND department_id = :department AND end_date IS NULL",
        {"id": viewer.employee_id, "department": second},
    )
    ended = await platform.account(roles=("admin",))
    response = await ended.delete(
        f"/api/v1/employees/{viewer.employee_id}/assignments/{assignment_id}"
    )
    assert response.status_code == 200, response.text

    moved_away = await viewer.get(f"/api/v1/employees/{outsider.employee_id}")

    assert moved_away.json()["visibility"] == "minimal", (
        "access survived the assignment that granted it"
    )
    assert "email" not in moved_away.json()


async def test_a_clearance_change_reaches_the_very_next_request(platform: Platform) -> None:
    """Clearance is derived from the department, so the department is what changes.

    The change goes through the real endpoint and so does the request after it;
    what is asserted is the snapshot that request ran on, resolved the same way the
    request resolved it. Nothing reads a clearance over HTTP yet — the document
    and retrieval rules that do arrive with ticket 31 — so the end-to-end half (a
    clearance-bounded listing answering differently) belongs to that ticket, and
    this test does not pretend to be it.
    """
    parent = await platform.department("direccion")
    actor = await platform.account(roles=("employee",))
    await platform.assign(actor.employee_id, parent, await platform.position(parent, "lead"))
    admin = await platform.account(roles=("admin",))

    first = await actor.get("/api/v1/departments")
    assert first.status_code == 200
    assert (await principal_of(platform, actor.user_id)).clearance_level == "low"

    response = await admin.patch(
        f"/api/v1/departments/{parent}", json={"clearance_level": "medium"}
    )
    assert response.status_code == 200, response.text

    # A real request after the write, and then the snapshot that request ran on:
    # the cached entry from the request above is still inside its TTL and must not
    # be the one being read.
    second = await actor.get("/api/v1/departments")
    assert second.status_code == 200
    assert (await principal_of(platform, actor.user_id)).clearance_level == "medium", (
        "the snapshot still described the clearance from before the write"
    )
