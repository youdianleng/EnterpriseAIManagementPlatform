"""Projects and tasks, driven over a real database and real sessions.

No mocks, for the reason the design records: what this ticket has to get right is a
statement about *rows* — whose project a manager may change, and what a task's
billable configuration resolves to — and a substitute would answer with the test's
own assumptions about both.

The ticket's own vocabulary is used throughout. Each test below names the checklist
line it pins, and the four that matter most are:

* `test_a_client_cannot_make_an_unbillable_task_billable` — the server's answer, not
  the client's. This is the requirement the flag is not a request field exists for.
* `test_a_project_manager_may_not_manage_somebody_elses_project` and its kernel
  counterpart — the resource rule, asserted where it lives *and* where it is used.
* `test_the_project_filter_says_what_may_be_recorded_against` — the visibility
  description `filter_for(ResourceKind.PROJECT)` returns, asserted as data.
* `test_an_archived_project_is_refused_by_every_write_and_stays_readable` — archiving
  stops new work and keeps history.
"""

from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest

from app.core.errors import ErrorCode
from app.domain.access.kernel import Reason, Resource, ResourceKind, can, filter_for
from app.domain.access.permissions import Action
from app.domain.access.principal import Principal
from app.domain.project.models import ProjectStatus, Unset
from tests.support.platform import Actor, Platform

# --- the cast ---------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class Cast:
    """The people and places these tests move between."""

    department: str
    other_department: str
    #: A project manager: holds `manager` through a managerial position, and is not
    #: an administrator, which is what makes the refusal meaningful.
    manager: Actor
    #: A second project manager, for the "somebody else's project" cases.
    other_manager: Actor
    admin: Actor
    hr: Actor
    #: An ordinary employee, assigned to the same department as the manager.
    employee: Actor
    #: A code unique to this test run, so the global uniqueness of `projects.code`
    #: is never the reason a fixture fails.
    prefix: str


def code_suffix() -> str:
    return uuid4().hex[:8]


@pytest.fixture
async def cast(platform: Platform) -> Cast:
    """Two departments, three managers and one employee, all committed.

    Real accounts signed in for real: a permission decision made against an injected
    principal would prove nothing about the snapshot the endpoint actually builds,
    and the manager's role is *derived* from a managerial position — which is part
    of what is under test.
    """
    prefix = code_suffix()
    department = await platform.department(f"proyectos{prefix}")
    other_department = await platform.department(f"otro{prefix}")
    position = await platform.position(department, f"jefe{prefix}", is_managerial=True)
    other_position = await platform.position(other_department, f"jefe{prefix}b", is_managerial=True)
    plain_position = await platform.position(department, f"tecnico{prefix}")

    manager = await platform.account(roles=("employee",))
    await platform.assign(manager.employee_id, department, position)

    other_manager = await platform.account(roles=("employee",))
    await platform.assign(other_manager.employee_id, other_department, other_position)

    employee = await platform.account(roles=("employee",))
    await platform.assign(employee.employee_id, department, plain_position)

    return Cast(
        department=department,
        other_department=other_department,
        manager=manager,
        other_manager=other_manager,
        admin=await platform.admin(),
        hr=await platform.account(roles=("hr",)),
        employee=employee,
        prefix=prefix,
    )


# --- helpers ----------------------------------------------------------------


async def create_project(
    actor: Actor,
    cast: Cast,
    *,
    code: str | None = None,
    status: str = "active",
    is_billable_default: bool = True,
    department_id: str | None = None,
    client_name: str | None = None,
):
    """A project, created through the endpoint, by the manager who will run it."""
    return await actor.post(
        "/api/v1/projects",
        json={
            "code": code or f"p{code_suffix()}",
            "name_es": "Proyecto",
            "name_en": "Project",
            "department_id": department_id or cast.department,
            "start_date": "2026-01-01",
            "status": status,
            "is_billable_default": is_billable_default,
            "client_name": client_name,
        },
    )


async def project(
    actor: Actor,
    cast: Cast,
    *,
    status: str = "active",
    is_billable_default: bool = True,
    department_id: str | None = None,
    client_name: str | None = None,
) -> dict:
    response = await create_project(
        actor,
        cast,
        status=status,
        is_billable_default=is_billable_default,
        department_id=department_id,
        client_name=client_name,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def add_task(
    actor: Actor,
    project_id: str,
    *,
    code: str | None = None,
    is_billable: bool | None = None,
    is_active: bool = True,
):
    return await actor.post(
        f"/api/v1/projects/{project_id}/tasks",
        json={
            "code": code or f"t{code_suffix()}",
            "name_es": "Tarea",
            "name_en": "Task",
            "is_billable": is_billable,
            "is_active": is_active,
        },
    )


async def task(actor: Actor, project_id: str, **kwargs) -> dict:
    response = await add_task(actor, project_id, **kwargs)
    assert response.status_code == 201, response.text
    return response.json()


async def record_time(actor: Actor, project_id: str, task_id: str, *, is_billable=None):
    body: dict = {"task_id": task_id}
    if is_billable is not None:
        body["is_billable"] = is_billable
    return await actor.post(f"/api/v1/projects/{project_id}/record-time", json=body)


def principal_for(
    *,
    employee_id: str,
    user_id: str = "",
    roles: tuple[str, ...],
    departments: tuple[str, ...] = (),
) -> Principal:
    """A principal for the kernel tests: the same facts the snapshot would build.

    Used only where the claim is about `can()` itself. The endpoint tests use a real
    session, because a test that injected a principal would bypass the mechanism it
    is testing.
    """
    return Principal(
        user_id=UUID(user_id) if user_id else uuid4(),
        employee_id=UUID(employee_id),
        username="kernel",
        roles=frozenset(set(roles) | {"employee"}),
        clearance_level="low",
        department_ids=frozenset(UUID(value) for value in departments),
        primary_department_id=UUID(departments[0]) if departments else None,
        is_manager="manager" in roles,
    )


# --- 项目含：编码、名称、客户名、归属部门、项目经理、默认是否可计费、状态、起止日期 ---


async def test_a_manager_creates_a_project_and_it_carries_every_field(
    platform: Platform, cast: Cast
) -> None:
    """The ticket's first line, field by field."""
    response = await create_project(
        cast.manager,
        cast,
        client_name="ACME",
        is_billable_default=False,
        status="active",
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name_es"] == "Proyecto"
    assert body["name_en"] == "Project"
    assert body["client_name"] == "ACME"
    assert body["department_id"] == cast.department
    # The caller is the manager, and no request field could have said otherwise.
    assert body["manager_employee_id"] == cast.manager.employee_id
    assert body["is_billable_default"] is False
    assert body["status"] == "active"
    assert body["start_date"] == "2026-01-01"
    assert body["end_date"] is None
    assert body["code"].startswith("p")

    stored = await platform.sql(
        "SELECT code, status, department_id, manager_employee_id FROM projects WHERE id = :id",
        {"id": body["id"]},
    )
    assert stored[0][0] == body["code"]
    assert str(stored[0][3]) == cast.manager.employee_id


async def test_a_new_project_is_a_draft_until_somebody_starts_it(
    platform: Platform, cast: Cast
) -> None:
    """The status default is `draft`, which is the safe direction: a draft project
    accepts neither tasks nor time."""
    response = await cast.manager.post(
        "/api/v1/projects",
        json={
            "code": f"p{code_suffix()}",
            "name_es": "Proyecto",
            "name_en": "Project",
            "department_id": cast.department,
            "start_date": "2026-01-01",
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["status"] == "draft"

    refused = await add_task(cast.manager, response.json()["id"])
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == ErrorCode.PROJECT_NOT_ACTIVE.value


async def test_a_project_code_is_unique_for_good(platform: Platform, cast: Cast) -> None:
    """Archiving does not release a code.

    The code is what a timesheet entry and an invoice line name, and an archived
    project stays readable — so a recycled code would leave two projects answering to
    one name in a record that outlives both.
    """
    existing = await project(cast.manager, cast)
    await cast.manager.patch(f"/api/v1/projects/{existing['id']}", json={"status": "archived"})

    response = await create_project(cast.manager, cast, code=existing["code"])

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == ErrorCode.PROJECT_CODE_TAKEN.value
    assert await platform.scalar("SELECT count(*) FROM projects") == 1


async def test_a_project_whose_end_precedes_its_start_is_refused(
    platform: Platform, cast: Cast
) -> None:
    response = await cast.manager.post(
        "/api/v1/projects",
        json={
            "code": f"p{code_suffix()}",
            "name_es": "Proyecto",
            "name_en": "Project",
            "department_id": cast.department,
            "start_date": "2026-06-01",
            "end_date": "2026-05-01",
        },
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == ErrorCode.PROJECT_DATES_INVALID.value
    assert await platform.scalar("SELECT count(*) FROM projects") == 0


async def test_an_omitted_key_is_left_alone_and_a_null_clears(
    platform: Platform, cast: Cast
) -> None:
    """A patch distinguishes "not sent" from "sent as null".

    Without that, an end date could be set and never reopened, and a client name
    could be added and never removed — the failure the department module's separate
    manager endpoint exists to work around.
    """
    existing = await project(cast.manager, cast, client_name="ACME")
    await cast.manager.patch(
        f"/api/v1/projects/{existing['id']}", json={"end_date": "2026-12-31"}
    )

    renamed = await cast.manager.patch(
        f"/api/v1/projects/{existing['id']}", json={"name_es": "Otro nombre"}
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["end_date"] == "2026-12-31", "an omitted key was cleared"
    assert renamed.json()["client_name"] == "ACME", "an omitted key was cleared"

    cleared = await cast.manager.patch(
        f"/api/v1/projects/{existing['id']}", json={"end_date": None, "client_name": None}
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["end_date"] is None, "an explicit null was ignored"
    assert cleared.json()["client_name"] is None, "an explicit null was ignored"


# --- 项目下有二级任务，每个任务有编码、名称、是否可计费、是否启用 ---


async def test_a_project_has_tasks_with_a_code_a_name_a_flag_and_a_switch(
    platform: Platform, cast: Cast
) -> None:
    existing = await project(cast.manager, cast)

    response = await add_task(cast.manager, existing["id"], code="01")

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["code"] == "01"
    assert body["name_es"] == "Tarea"
    assert body["name_en"] == "Task"
    assert body["is_active"] is True
    assert body["project_id"] == existing["id"]

    reread = await cast.manager.get(f"/api/v1/projects/{existing['id']}")
    assert [row["code"] for row in reread.json()["tasks"]] == ["01"]


async def test_a_task_code_is_unique_within_its_project_and_nowhere_else(
    platform: Platform, cast: Cast
) -> None:
    """`01` is a drawing number in one project and means nothing in another."""
    first = await project(cast.manager, cast)
    second = await project(cast.manager, cast)
    await task(cast.manager, first["id"], code="01")

    duplicate = await add_task(cast.manager, first["id"], code="01")
    elsewhere = await add_task(cast.manager, second["id"], code="01")

    assert duplicate.status_code == 409, duplicate.text
    assert duplicate.json()["error"]["code"] == ErrorCode.PROJECT_TASK_CODE_TAKEN.value
    assert elsewhere.status_code == 201, elsewhere.text


async def test_deactivating_a_task_keeps_the_row_and_refuses_a_second_attempt(
    platform: Platform, cast: Cast
) -> None:
    """The ticket says "deactivate", not "delete", and the row stays: a historical
    entry points at it."""
    existing = await project(cast.manager, cast)
    created = await task(cast.manager, existing["id"])

    response = await cast.manager.post(
        f"/api/v1/projects/{existing['id']}/tasks/{created['id']}/deactivate"
    )

    assert response.status_code == 200, response.text
    assert response.json()["is_active"] is False
    assert await platform.scalar(
        "SELECT count(*) FROM project_tasks WHERE id = :id", {"id": created["id"]}
    ) == 1

    again = await cast.manager.post(
        f"/api/v1/projects/{existing['id']}/tasks/{created['id']}/deactivate"
    )
    assert again.status_code == 409, again.text
    assert again.json()["error"]["code"] == ErrorCode.PROJECT_TASK_ALREADY_INACTIVE.value


# --- 任务的可计费标记继承项目默认值，项目经理可逐任务覆盖 ---


async def test_a_task_with_no_flag_of_its_own_inherits_the_project_default(
    platform: Platform, cast: Cast
) -> None:
    """Both directions, so the inheritance is a rule rather than a coincidence."""
    billable_project = await project(cast.manager, cast, is_billable_default=True)
    unbillable_project = await project(cast.manager, cast, is_billable_default=False)

    inherits_true = await task(cast.manager, billable_project["id"])
    inherits_false = await task(cast.manager, unbillable_project["id"])

    assert inherits_true["is_billable"] is None, "the row must store 'inherit', not a copy"
    assert inherits_true["is_billable_effective"] is True
    assert inherits_false["is_billable"] is None
    assert inherits_false["is_billable_effective"] is False


async def test_a_manager_overrides_the_default_per_task(platform: Platform, cast: Cast) -> None:
    """The ticket's 项目经理可逐任务覆盖, in both directions and back again."""
    existing = await project(cast.manager, cast, is_billable_default=True)
    created = await task(cast.manager, existing["id"], is_billable=False)

    assert created["is_billable"] is False
    assert created["is_billable_effective"] is False

    overridden = await cast.manager.patch(
        f"/api/v1/projects/{existing['id']}/tasks/{created['id']}",
        json={"is_billable": True},
    )
    assert overridden.json()["is_billable_effective"] is True

    back_to_inheriting = await cast.manager.patch(
        f"/api/v1/projects/{existing['id']}/tasks/{created['id']}",
        json={"is_billable": None},
    )
    assert back_to_inheriting.json()["is_billable"] is None, "null must mean 'inherit again'"
    assert back_to_inheriting.json()["is_billable_effective"] is True

    # An omitted key leaves the flag alone — the same distinction as the project
    # patch, and the reason a bare null cannot be the only way to say "inherit".
    renamed = await cast.manager.patch(
        f"/api/v1/projects/{existing['id']}/tasks/{created['id']}",
        json={"name_es": "Tarea dos"},
    )
    assert renamed.json()["is_billable"] is None


# --- 员工无法通过接口参数把不可计费任务提交为可计费 ---


async def test_a_client_cannot_make_an_unbillable_task_billable(
    platform: Platform, cast: Cast
) -> None:
    """**The ticket's explicit requirement.**

    The client sends `is_billable: true` for a task whose configuration says
    otherwise, and the stored and echoed value is the task's. This is why the flag
    is not a request field that decides anything: a request that names a task cannot
    make it billable.

    The task inherits an unbillable project, so the value recorded is the *resolved*
    configuration rather than a column read back — the inheritance is exercised on
    the same path the lie travels down.
    """
    existing = await project(cast.manager, cast, is_billable_default=False)
    created = await task(cast.manager, existing["id"])

    response = await record_time(
        cast.employee, existing["id"], created["id"], is_billable=True
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["is_billable"] is False, "the client's claim decided the recorded value"
    assert body["claimed_billable"] is True, "the claim should be echoed, not hidden"
    assert body["follows_project_default"] is True
    # And what would be written is what the domain resolved, not what was sent.
    assert body["is_billable"] == created["is_billable_effective"]


async def test_a_client_cannot_make_a_billable_task_unbillable_either(
    platform: Platform, cast: Cast
) -> None:
    """The same rule in the other direction: the flag is configuration, not a choice.

    An employee sending `false` for a billable task would otherwise be claiming
    non-billable work as their own decision — which is the same defect, and the one
    a one-sided test would miss.
    """
    existing = await project(cast.manager, cast, is_billable_default=True)
    created = await task(cast.manager, existing["id"], is_billable=True)

    response = await record_time(
        cast.employee, existing["id"], created["id"], is_billable=False
    )

    assert response.status_code == 200, response.text
    assert response.json()["is_billable"] is True
    assert response.json()["claimed_billable"] is False


async def test_the_recorded_value_follows_the_task_override_not_the_project(
    platform: Platform, cast: Cast
) -> None:
    """The override wins over the project's default, which is what makes the flag
    per-task rather than a project setting in disguise."""
    existing = await project(cast.manager, cast, is_billable_default=True)
    created = await task(cast.manager, existing["id"], is_billable=False)

    response = await record_time(cast.employee, existing["id"], created["id"])

    assert response.status_code == 200, response.text
    assert response.json()["is_billable"] is False
    assert response.json()["follows_project_default"] is False


# --- 员工只能对"已启用且在其可见范围内"的项目与任务填报工时 ---


async def test_a_deactivated_task_cannot_receive_time(platform: Platform, cast: Cast) -> None:
    existing = await project(cast.manager, cast)
    created = await task(cast.manager, existing["id"])
    await cast.manager.post(f"/api/v1/projects/{existing['id']}/tasks/{created['id']}/deactivate")

    response = await record_time(cast.employee, existing["id"], created["id"], is_billable=True)

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == ErrorCode.PROJECT_TASK_NOT_RECORDABLE.value


async def test_a_draft_project_cannot_receive_time(platform: Platform, cast: Cast) -> None:
    """Not started yet. The task has to be created while the project is active and
    the project then put back to draft, because a task cannot be added to a draft."""
    existing = await project(cast.manager, cast)
    created = await task(cast.manager, existing["id"])
    await cast.manager.patch(f"/api/v1/projects/{existing['id']}", json={"status": "draft"})

    response = await record_time(cast.employee, existing["id"], created["id"])

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == ErrorCode.PROJECT_TASK_NOT_RECORDABLE.value


async def test_a_project_outside_the_employees_departments_cannot_receive_their_time(
    platform: Platform, cast: Cast
) -> None:
    """In reach means the caller's departments, descendants included — or a project
    they manage. Somebody else's department is neither."""
    existing = await project(
        cast.other_manager, cast, department_id=cast.other_department
    )
    created = await task(cast.other_manager, existing["id"])

    response = await record_time(cast.employee, existing["id"], created["id"])

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == ErrorCode.PROJECT_TASK_NOT_RECORDABLE.value

    # The control: the manager of that project is in reach of it, and may.
    allowed = await record_time(cast.other_manager, existing["id"], created["id"])
    assert allowed.status_code == 200, allowed.text


async def test_a_task_cannot_be_reached_through_another_project(
    platform: Platform, cast: Cast
) -> None:
    """The task is checked against the project in the path, not merely looked up."""
    mine = await project(cast.manager, cast)
    theirs = await project(cast.manager, cast)
    created = await task(cast.manager, theirs["id"])

    response = await record_time(cast.manager, mine["id"], created["id"])

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == ErrorCode.PROJECT_TASK_NOT_FOUND.value


# --- 项目归档后不可再新增工时，但历史工时保留可查 ---


async def test_an_archived_project_is_refused_by_every_write_and_stays_readable(
    platform: Platform, cast: Cast
) -> None:
    """The guarantee ticket 27 can make today.

    No `time_entries` table exists yet, so "no new time" is asserted through the
    visibility rule and through every write endpoint; "history is kept" is asserted
    by reading the project and its tasks back afterwards. Ticket 28 must add the
    third leg — an inserted entry refused at the database level for an archived
    project — and the ticket file says so.
    """
    existing = await project(cast.manager, cast, is_billable_default=True)
    created = await task(cast.manager, existing["id"], is_billable=True)

    archived = await cast.manager.patch(
        f"/api/v1/projects/{existing['id']}", json={"status": "archived"}
    )
    assert archived.status_code == 200, archived.text
    assert archived.json()["status"] == "archived"

    written = [
        await record_time(cast.employee, existing["id"], created["id"], is_billable=True),
        await cast.manager.patch(
            f"/api/v1/projects/{existing['id']}", json={"name_es": "Renombrado"}
        ),
        await add_task(cast.manager, existing["id"]),
        await cast.manager.patch(
            f"/api/v1/projects/{existing['id']}/tasks/{created['id']}",
            json={"is_billable": False},
        ),
    ]
    codes = [response.json()["error"]["code"] for response in written]
    assert codes == [
        ErrorCode.PROJECT_TASK_NOT_RECORDABLE.value,
        ErrorCode.PROJECT_ARCHIVED.value,
        ErrorCode.PROJECT_ARCHIVED.value,
        ErrorCode.PROJECT_ARCHIVED.value,
    ], written

    # History stays: the rows and the tasks are still there, and still readable.
    reread = await cast.manager.get(f"/api/v1/projects/{existing['id']}")
    assert reread.status_code == 200, reread.text
    assert reread.json()["status"] == "archived"
    assert reread.json()["is_billable_default"] is True
    assert [row["id"] for row in reread.json()["tasks"]] == [created["id"]]
    assert reread.json()["tasks"][0]["is_billable"] is True
    assert await platform.scalar(
        "SELECT count(*) FROM project_tasks WHERE project_id = :id", {"id": existing["id"]}
    ) == 1
    # And an administrator reads it too: archiving hides nothing.
    assert (await cast.admin.get(f"/api/v1/projects/{existing['id']}")).status_code == 200


async def test_a_project_with_a_closed_status_cannot_take_new_tasks(
    platform: Platform, cast: Cast
) -> None:
    """Adding a task to a project that is over is refused, and with its own code:
    the project is not archived, it is simply not running."""
    existing = await project(cast.manager, cast)
    await cast.manager.patch(f"/api/v1/projects/{existing['id']}", json={"status": "closed"})

    response = await add_task(cast.manager, existing["id"])

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == ErrorCode.PROJECT_NOT_ACTIVE.value


async def test_an_archived_project_refuses_a_reassignment_too(
    platform: Platform, cast: Cast
) -> None:
    existing = await project(cast.manager, cast)
    await cast.manager.patch(f"/api/v1/projects/{existing['id']}", json={"status": "archived"})

    response = await cast.admin.put(
        f"/api/v1/projects/{existing['id']}/manager",
        json={"manager_employee_id": cast.other_manager.employee_id},
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == ErrorCode.PROJECT_ARCHIVED.value


# --- 项目经理只能管理自己负责的项目；管理员与人力资源可管理全部 ---


async def test_the_kernel_refuses_a_non_manager_and_allows_administration(
    platform: Platform, cast: Cast
) -> None:
    """The rule asserted where it lives, before the endpoint is considered.

    A stranger in the project's own department is refused — "in my department" must
    not be read as "mine to edit" — and administration and HR are allowed by the
    catalogue's privilege clause.
    """
    project_resource = Resource(
        ResourceKind.PROJECT,
        department_id=UUID(cast.department),
        manager_employee_id=UUID(cast.manager.employee_id),
    )

    manager = principal_for(
        employee_id=cast.manager.employee_id,
        roles=("manager",),
        departments=(cast.department,),
    )
    # A *manager* who does not manage this project, which is the interesting refusal:
    # a plain employee never reaches the resource clause, because the catalogue's role
    # list stops them first — and that is a different reason, asserted below.
    other_manager = principal_for(
        employee_id=cast.other_manager.employee_id,
        roles=("manager",),
        departments=(cast.other_department,),
    )
    employee = principal_for(
        employee_id=cast.employee.employee_id,
        roles=("employee",),
        departments=(cast.department,),
    )
    admin = principal_for(employee_id=cast.admin.employee_id, roles=("admin",))
    hr = principal_for(employee_id=cast.hr.employee_id, roles=("hr",))

    assert can(manager, Action.PROJECT_MANAGE, project_resource).allowed

    refused = can(other_manager, Action.PROJECT_MANAGE, project_resource)
    assert refused.denied, "a project manager reached somebody else's project"
    assert refused.primary_reason is Reason.NOT_PROJECT_MANAGER

    # The colleague in the project's own department is refused too, and for the
    # *role* reason: "in my department" is not a way into somebody else's project.
    colleague = can(employee, Action.PROJECT_MANAGE, project_resource)
    assert colleague.denied
    assert colleague.primary_reason is Reason.ROLE_LACKS_PERMISSION

    assert can(admin, Action.PROJECT_MANAGE, project_resource).allowed
    assert can(hr, Action.PROJECT_MANAGE, project_resource).allowed
    # Finance reads projects and does not manage them, whatever its privilege for
    # payroll: the two sets are deliberately not the same.
    finance = principal_for(employee_id=cast.hr.employee_id, roles=("finance",))
    assert can(finance, Action.PROJECT_MANAGE, project_resource).denied


async def test_a_project_manager_may_not_manage_somebody_elses_project(
    platform: Platform, cast: Cast
) -> None:
    """The same rule at the endpoint, where it is actually used.

    A *project manager* — the role the rule is about, holding an actual managerial
    position — is refused on somebody else's project, and the refusal is audited like
    every other one.
    """
    theirs = await project(cast.other_manager, cast, department_id=cast.other_department)

    response = await cast.manager.patch(
        f"/api/v1/projects/{theirs['id']}", json={"name_es": "Secuestrado"}
    )

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == ErrorCode.FORBIDDEN.value

    unchanged = await cast.other_manager.get(f"/api/v1/projects/{theirs['id']}")
    assert unchanged.json()["name_es"] == "Proyecto", "the rename went through anyway"

    refused_actions = await platform.sql(
        """
        SELECT entity_type, after -> 'action', after -> 'reasons'
        FROM audit_log WHERE action = 'access.refused'
        """
    )
    assert refused_actions, "the refusal was not audited"
    assert refused_actions[-1][0] == "project"
    assert refused_actions[-1][1] == "project.manage"
    assert "not_project_manager" in refused_actions[-1][2]


async def test_a_project_manager_may_manage_their_own(platform: Platform, cast: Cast) -> None:
    """The control for the refusal above: the rule refuses a project, not a role."""
    mine = await project(cast.manager, cast)

    response = await cast.manager.patch(
        f"/api/v1/projects/{mine['id']}", json={"name_es": "Renombrado"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["name_es"] == "Renombrado"


async def test_administration_and_hr_manage_any_project(platform: Platform, cast: Cast) -> None:
    """§4.1: administration and HR manage all of them, whoever the manager is."""
    for actor in (cast.admin, cast.hr):
        theirs = await project(cast.other_manager, cast, department_id=cast.other_department)

        renamed = await actor.patch(
            f"/api/v1/projects/{theirs['id']}", json={"name_es": "Gestionado"}
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["name_es"] == "Gestionado"


async def test_a_manager_may_not_hand_their_project_to_somebody_else(
    platform: Platform, cast: Cast
) -> None:
    """Reassignment is administration's and HR's, and the manager who runs the
    project still may not do it: naming an accomplice is the escalation."""
    existing = await project(cast.manager, cast)

    response = await cast.manager.put(
        f"/api/v1/projects/{existing['id']}/manager",
        json={"manager_employee_id": cast.other_manager.employee_id},
    )

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == ErrorCode.PROJECT_NOT_MANAGEABLE.value
    unchanged = await cast.manager.get(f"/api/v1/projects/{existing['id']}")
    assert unchanged.json()["manager_employee_id"] == cast.manager.employee_id


async def test_an_administrator_hands_a_project_over(platform: Platform, cast: Cast) -> None:
    existing = await project(cast.manager, cast)

    response = await cast.admin.put(
        f"/api/v1/projects/{existing['id']}/manager",
        json={"manager_employee_id": cast.other_manager.employee_id},
    )

    assert response.status_code == 200, response.text
    assert response.json()["manager_employee_id"] == cast.other_manager.employee_id

    # And the rule follows the row: the old manager is now the stranger.
    refused = await cast.manager.patch(
        f"/api/v1/projects/{existing['id']}", json={"name_es": "Todavía mío"}
    )
    assert refused.status_code == 403, refused.text
    assert (
        await cast.other_manager.patch(
            f"/api/v1/projects/{existing['id']}", json={"name_es": "Ahora mío"}
        )
    ).status_code == 200


async def test_a_project_cannot_be_handed_to_somebody_who_has_left(
    platform: Platform, cast: Cast
) -> None:
    """A project whose manager has left is a project nobody may change, so the
    reassignment is refused while somebody can still pick another name."""
    existing = await project(cast.manager, cast)
    gone = await platform.employee()
    await platform.sql("UPDATE employees SET status = 'terminated' WHERE id = :id", {"id": gone})

    response = await cast.admin.put(
        f"/api/v1/projects/{existing['id']}/manager", json={"manager_employee_id": gone}
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == ErrorCode.PROJECT_MANAGER_NOT_FOUND.value


# --- 项目列表支持按部门、状态、客户筛选，并分页 ---


async def test_the_list_filters_by_department_status_and_client(
    platform: Platform, cast: Cast
) -> None:
    mine = await project(
        cast.manager, cast, status="active", client_name="ACME", department_id=cast.department
    )
    theirs_in_my_department = await project(
        cast.manager,
        cast,
        status="draft",
        client_name="ACME",
        department_id=cast.department,
    )
    other = await project(
        cast.other_manager,
        cast,
        status="active",
        client_name="Globex",
        department_id=cast.other_department,
    )

    by_department = await cast.manager.get(
        "/api/v1/projects", params={"department_id": cast.department}
    )
    assert {row["id"] for row in by_department.json()["items"]} == {
        mine["id"],
        theirs_in_my_department["id"],
    }, "the department filter let another department's project through"

    by_status = await cast.manager.get("/api/v1/projects", params={"status": "active"})
    assert {row["id"] for row in by_status.json()["items"]} == {mine["id"], other["id"]}

    by_client = await cast.manager.get("/api/v1/projects", params={"client_name": "globex"})
    assert [row["id"] for row in by_client.json()["items"]] == [other["id"]]

    combined = await cast.manager.get(
        "/api/v1/projects",
        params={"department_id": cast.department, "status": "active", "client_name": "acme"},
    )
    assert [row["id"] for row in combined.json()["items"]] == [mine["id"]]

    missing = await cast.manager.get("/api/v1/projects", params={"client_name": "nadie"})
    assert missing.json()["items"] == []


async def test_the_list_is_paginated_and_reports_the_total(platform: Platform, cast: Cast) -> None:
    for index in range(3):
        await create_project(
            cast.manager,
            cast,
            code=f"page{cast.prefix}{index}",
            client_name="Paginado",
        )

    first = await cast.manager.get(
        "/api/v1/projects", params={"client_name": "Paginado", "limit": 2}
    )
    second = await cast.manager.get(
        "/api/v1/projects", params={"client_name": "Paginado", "limit": 2, "offset": 2}
    )

    assert first.json()["total"] == 3
    assert first.json()["limit"] == 2
    assert first.json()["offset"] == 0
    assert len(first.json()["items"]) == 2
    assert second.json()["total"] == 3
    assert len(second.json()["items"]) == 1
    # Pages do not overlap, and are ordered by code so a client can page safely.
    assert not {row["id"] for row in first.json()["items"]} & {
        row["id"] for row in second.json()["items"]
    }
    assert [row["code"] for row in first.json()["items"]] == sorted(
        row["code"] for row in first.json()["items"]
    )


async def test_an_employee_reads_the_catalogue_and_cannot_create_a_project(
    platform: Platform, cast: Cast
) -> None:
    """Reading is what filling in a timesheet needs; creating is not."""
    existing = await project(cast.manager, cast)

    listed = await cast.employee.get("/api/v1/projects")
    assert listed.status_code == 200, listed.text
    assert existing["id"] in {row["id"] for row in listed.json()["items"]}

    created = await create_project(cast.employee, cast)
    assert created.status_code == 403, created.text
    assert created.json()["error"]["code"] == ErrorCode.FORBIDDEN.value


# --- the visibility description ticket 28 consumes --------------------------


async def test_the_project_filter_says_what_may_be_recorded_against(
    platform: Platform, cast: Cast
) -> None:
    """**Asserted as data, not as a query.**

    `filter_for` returns a description because the same decision is consumed by a
    list, by a write path and by a test — so the test is about the description. The
    four fields say: active projects only, no department restriction for
    administration, and for everybody else the two ways in (the caller's departments,
    or a project they manage).
    """
    employee = principal_for(
        employee_id=cast.employee.employee_id,
        roles=("employee",),
        departments=(cast.department,),
    )
    spec = filter_for(employee, ResourceKind.PROJECT)

    assert spec.kind is ResourceKind.PROJECT
    assert spec.statuses == frozenset({"active"}), "only active projects accept new time"
    assert spec.allow_all is False
    assert spec.department_ids == frozenset({UUID(cast.department)})
    assert spec.manager_employee_id == UUID(cast.employee.employee_id), (
        "the manager clause must carry the caller's own id"
    )

    admin = principal_for(employee_id=cast.admin.employee_id, roles=("admin",))
    admin_spec = filter_for(admin, ResourceKind.PROJECT)
    assert admin_spec.allow_all is True, "administration is not bounded by department"
    assert admin_spec.statuses == frozenset({"active"}), (
        "and is still bounded by status: an archived project is not bookable by the "
        "person who archived it either"
    )

    manager = principal_for(
        employee_id=cast.manager.employee_id,
        roles=("manager",),
        departments=(cast.department,),
    )
    assert filter_for(manager, ResourceKind.PROJECT).manager_employee_id == UUID(
        cast.manager.employee_id
    )


async def test_a_closed_project_is_out_of_reach_for_time_even_though_it_is_readable(
    platform: Platform, cast: Cast
) -> None:
    """`closed` and `archived` are different states with the same effect on new time.

    A closed project stays readable — a manager administers it, and its history is
    what a report reads — and refuses time, because ticket 27 is about new records.
    """
    existing = await project(cast.manager, cast)
    created = await task(cast.manager, existing["id"])
    await cast.manager.patch(f"/api/v1/projects/{existing['id']}", json={"status": "closed"})

    assert (await cast.manager.get(f"/api/v1/projects/{existing['id']}")).status_code == 200
    response = await record_time(cast.manager, existing["id"], created["id"])
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == ErrorCode.PROJECT_TASK_NOT_RECORDABLE.value


async def test_the_selectable_list_and_the_row_check_agree(platform: Platform, cast: Cast) -> None:
    """One rule, two shapes: the list a picker shows and the check a write makes.

    `filter_for` returns data so that both are built from it; this asserts the two
    do not drift, which is exactly the failure a second implementation would produce.
    The list is read from `/projects/selectable` — the endpoint that *uses* the spec,
    rather than the facet list, which answers a different question.
    """
    ours = await project(cast.manager, cast, department_id=cast.department)
    theirs = await project(cast.other_manager, cast, department_id=cast.other_department)
    archived = await project(cast.manager, cast, department_id=cast.department)
    ours_task = await task(cast.manager, ours["id"])
    theirs_task = await task(cast.other_manager, theirs["id"])
    archived_task = await task(cast.manager, archived["id"])
    await cast.manager.patch(f"/api/v1/projects/{archived['id']}", json={"status": "archived"})

    listed = await cast.employee.get("/api/v1/projects/selectable")
    assert listed.status_code == 200, listed.text
    reachable = {row["id"] for row in listed.json()["items"]}
    assert ours["id"] in reachable
    assert theirs["id"] not in reachable, "another department's project was offered"
    assert archived["id"] not in reachable, "an archived project was offered"

    # The three rows the list describes, judged one at a time by the write path.
    for project_id, task_id, expected in (
        (ours["id"], ours_task["id"], 200),
        (theirs["id"], theirs_task["id"], 422),
        (archived["id"], archived_task["id"], 422),
    ):
        response = await record_time(cast.employee, project_id, task_id)
        assert response.status_code == expected, (project_id, response.text)
        assert (project_id in reachable) is (expected == 200), (
            "the list and the row check disagree"
        )

    # An administrator is not bounded by department, and is still bounded by status.
    admin_list = await cast.admin.get("/api/v1/projects/selectable")
    admin_reachable = {row["id"] for row in admin_list.json()["items"]}
    assert {ours["id"], theirs["id"]} <= admin_reachable
    assert archived["id"] not in admin_reachable


# --- the module's own bookkeeping -------------------------------------------


async def test_every_write_is_audited(platform: Platform, cast: Cast) -> None:
    """A project and a task each leave a trail, including the archive."""
    existing = await project(cast.manager, cast)
    created = await task(cast.manager, existing["id"])
    await cast.manager.patch(
        f"/api/v1/projects/{existing['id']}/tasks/{created['id']}", json={"is_billable": True}
    )
    await cast.manager.post(f"/api/v1/projects/{existing['id']}/tasks/{created['id']}/deactivate")
    await cast.manager.patch(f"/api/v1/projects/{existing['id']}", json={"status": "archived"})

    actions = [
        row[0]
        for row in await platform.sql("SELECT action FROM audit_log ORDER BY id")
    ]
    assert "project.created" in actions
    assert "project_task.created" in actions
    assert "project_task.updated" in actions
    assert "project_task.deactivated" in actions
    assert "project.archived" in actions

    archived = await platform.sql(
        "SELECT after -> 'status' FROM audit_log WHERE action = 'project.archived'"
    )
    assert archived[0][0] == "archived"


async def test_the_patch_field_lists_cover_every_field(platform: Platform) -> None:
    """A field added to a patch and forgotten by the repository would silently never
    update. The lists are read by the repository's loop, so this pins them together —
    and it pins the sentinel, which is what makes "omitted" different from "null"."""
    from dataclasses import fields

    from app.domain.project.models import PATCH_FIELDS, UNSET, ProjectPatch, ProjectTaskPatch
    from app.repositories.project import TASK_PATCH_FIELDS

    assert set(PATCH_FIELDS) == {field.name for field in fields(ProjectPatch)}
    assert set(TASK_PATCH_FIELDS) == {field.name for field in fields(ProjectTaskPatch)}

    # Every field defaults to the sentinel. A patch whose default was `None` would
    # write a null over a column the request never mentioned — which is how the first
    # version of this update path blanked a project's code.
    for patch in (ProjectPatch(), ProjectTaskPatch()):
        for field in fields(patch):
            assert getattr(patch, field.name) is UNSET, field.name

    # And the sentinel is a distinct object, not something a value can be equal to.
    assert ProjectPatch().is_billable_default is UNSET
    assert isinstance(ProjectPatch().end_date, Unset)


async def test_a_task_of_another_project_is_not_found_rather_than_forbidden(
    platform: Platform, cast: Cast
) -> None:
    """The route names the project, so the answer for a mismatched task is 404.

    Not 403: "that task exists and is not yours" would make this an existence oracle
    over the whole task table, and the caller already named the project they meant.
    """
    mine = await project(cast.manager, cast)
    theirs = await project(cast.manager, cast)
    created = await task(cast.manager, theirs["id"])

    response = await cast.manager.patch(
        f"/api/v1/projects/{mine['id']}/tasks/{created['id']}", json={"name_es": "No"}
    )

    assert response.status_code == 404, response.text


async def test_the_status_set_is_closed(platform: Platform, cast: Cast) -> None:
    """A status outside the set is refused by the schema, and the set is the one the
    module documents."""
    existing = await project(cast.manager, cast)

    response = await cast.manager.patch(
        f"/api/v1/projects/{existing['id']}", json={"status": "finished"}
    )

    assert response.status_code == 422, response.text
    assert {status.value for status in ProjectStatus} == {
        "draft",
        "active",
        "closed",
        "archived",
    }
