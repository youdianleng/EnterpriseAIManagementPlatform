"""Project endpoints.

Six routes, and the authorisation convention is the one ticket 21 established for a
resource the request names rather than the route:

* `Depends(require(...))` answers the **role** question — may this caller list
  projects, may this caller manage one at all. It cannot answer the resource
  question, because the project is named by the path and the dependency runs before
  the handler has read it.
* The handler then asks the kernel again, with a `Resource` built from the row
  (`_manageable` below), and records its refusal the same way the dependency does.
  The rule itself is in the kernel — this file contains no comparison of
  `manager_employee_id` to anything, and adding one would be the bug.

`is_billable` is the other convention worth naming: on the project and task surfaces
it is the project manager's configuration, and on `record-time` it is the *server's*
answer. A request may state it; the value that comes back is resolved from the task
and its project, and a test asserts exactly that.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import audit_refusal, current_principal, db_session, require
from app.api.v1.schemas.project import (
    ProjectCreate,
    ProjectDetail,
    ProjectManagerChange,
    ProjectPage,
    ProjectRead,
    ProjectTaskCreate,
    ProjectTaskRead,
    ProjectTaskUpdate,
    ProjectUpdate,
    RecordTimeRead,
    RecordTimeRequest,
)
from app.core.errors import AppError, ErrorCode
from app.domain.access import Action, Principal, ResourceKind, can
from app.domain.project.models import (
    Project,
    ProjectInput,
    ProjectPatch,
    ProjectQuery,
    ProjectStatus,
    ProjectTask,
    ProjectTaskInput,
    ProjectTaskPatch,
    RecordTarget,
)
from app.domain.project.service import ProjectService, resource_of
from app.repositories.project import PostgresProjectRepository

router = APIRouter(prefix="/projects", tags=["projects"])

#: Reading and managing, at the role level. `PROJECT_MANAGE` is held by managers as
#: well as by administration and HR, because a project manager manages their own;
#: which ones is the kernel's project rule, applied per row below.
read_projects = require(Action.PROJECT_READ, ResourceKind.PROJECT)
manage_projects = require(Action.PROJECT_MANAGE, ResourceKind.PROJECT)
read_tasks = require(Action.PROJECT_TASK_READ, ResourceKind.PROJECT)
manage_tasks = require(Action.PROJECT_TASK_MANAGE, ResourceKind.PROJECT)


def _service(session: AsyncSession) -> ProjectService:
    return ProjectService(PostgresProjectRepository(session), session)


def _read(project: Project) -> ProjectRead:
    return ProjectRead.model_validate(project, from_attributes=True)


def _task_read(task: ProjectTask, project: Project) -> ProjectTaskRead:
    """One task, with the billable question already answered.

    `is_billable_effective` is resolved through the task's own project, never through
    one the caller named: resolving against a project the task does not belong to is
    how a task becomes billable by accident.
    """
    return ProjectTaskRead(
        id=task.id,
        project_id=task.project_id,
        code=task.code,
        name_es=task.name_es,
        name_en=task.name_en,
        is_billable=task.is_billable,
        is_billable_effective=task.billable_for(project),
        is_active=task.is_active,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


async def _manageable(
    request: Request, service: ProjectService, project_id: UUID, principal: Principal
) -> Project:
    """The project, if this caller manages it — the kernel's answer, audited.

    Not a `require()` dependency, because the resource is named by the path rather
    than by the route: it makes the same `can()` call, records the same refusal and
    raises the same catalogued code, so there is one convention for "you may not do
    that" instead of two.
    """
    project = await service.get(project_id)
    decision = can(principal, Action.PROJECT_MANAGE, resource_of(project))
    if decision.allowed:
        return project

    await audit_refusal(request, principal, Action.PROJECT_MANAGE, decision, ResourceKind.PROJECT)
    raise AppError(
        ErrorCode.FORBIDDEN,
        detail=f"project {project_id} is not {principal.employee_id}'s to manage: "
        f"{decision.primary_reason}",
    )


# --- projects ---------------------------------------------------------------


@router.post(
    "",
    response_model=ProjectRead,
    status_code=201,
    summary="Create a project",
    dependencies=[Depends(manage_projects)],
)
async def create_project(
    payload: ProjectCreate,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> ProjectRead:
    """Create a project with the caller as its manager, in `draft` unless told
    otherwise.

    Creation has no resource to restrict — the project does not exist yet — so the
    role check is the whole answer, and every manager may create one. Handing it to
    somebody else afterwards is `reassign`, below.
    """
    service = _service(session)
    project = await service.create(_input(payload, principal), principal)
    return _read(project)


@router.get(
    "",
    response_model=ProjectPage,
    summary="List projects, filtered and paginated",
    dependencies=[Depends(read_projects)],
)
async def list_projects(
    department_id: UUID | None = Query(default=None, description="Owning department"),
    status: ProjectStatus | None = Query(default=None, description="Project status"),
    client_name: str | None = Query(
        default=None, description="Substring of the client name; case-insensitive"
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(db_session),
) -> ProjectPage:
    """The catalogue, as facets rather than as a permission.

    Filters are conjunctive and all of them are optional. There is no filter that
    *widens* what a caller sees: this route answers "which projects match these
    facets" for somebody the guard already allowed to list projects, and the
    question "what may I book against" is `filter_for`'s, in the kernel.
    """
    page = await _service(session).list_projects(
        ProjectQuery(
            department_id=department_id,
            status=status,
            client_name=client_name,
            limit=limit,
            offset=offset,
        )
    )
    return ProjectPage(
        items=[_read(project) for project in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get(
    "/selectable",
    response_model=ProjectPage,
    summary="The projects this caller may record time against",
    dependencies=[Depends(read_projects)],
)
async def list_selectable(
    department_id: UUID | None = Query(default=None),
    status: ProjectStatus | None = Query(default=None),
    client_name: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> ProjectPage:
    """The catalogue filtered by the kernel's answer, not by the caller's roles.

    Two questions that look alike and are not: `GET /projects` is "what exists,
    matching these facets", and this is "what may I book against". The difference is
    a status (an archived project exists and accepts nothing), a department (a
    colleague's project exists and is not yours to book), and the manager clause
    (a project you run is yours to book wherever it sits). It is the same
    `filter_for` a time entry will consume — this endpoint exists so the *data
    description* has a consumer today and cannot quietly rot before ticket 28 needs
    it.

    Declared before `/{project_id}` on purpose: a literal path that comes after the
    parameterised one is never reached.
    """
    page = await _service(session).recordable_projects(
        principal,
        ProjectQuery(
            department_id=department_id,
            status=status,
            client_name=client_name,
            limit=limit,
            offset=offset,
        ),
    )
    return ProjectPage(
        items=[_read(project) for project in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get(
    "/{project_id}",
    response_model=ProjectDetail,
    summary="Read a project and its tasks",
    dependencies=[Depends(read_projects)],
)
async def get_project(
    project_id: UUID,
    session: AsyncSession = Depends(db_session),
) -> ProjectDetail:
    """One project, with its tasks — active and inactive alike.

    Inactive tasks travel: an inactive task is one that may not receive *new* time,
    and the entries already recorded against it name it. Hiding it would make a
    historical timesheet point at a task no reader can resolve.
    """
    service = _service(session)
    project = await service.get(project_id)
    tasks = await service.list_tasks(project_id)
    detail = ProjectDetail.model_validate(project, from_attributes=True)
    detail.tasks = [_task_read(task, project) for task in tasks]
    return detail


@router.patch(
    "/{project_id}",
    response_model=ProjectRead,
    summary="Update a project",
    dependencies=[Depends(manage_projects)],
)
async def update_project(
    request: Request,
    project_id: UUID,
    payload: ProjectUpdate,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> ProjectRead:
    """Change a project, including archiving it.

    Archiving is a status change here rather than an endpoint of its own, because it
    *is* one: `status` is a column, the domain refuses anything on an archived
    project afterwards, and a separate route would be the same write with a second
    name.
    """
    service = _service(session)
    await _manageable(request, service, project_id, principal)
    project = await service.update(project_id, _patch(payload), principal)
    return _read(project)


@router.put(
    "/{project_id}/manager",
    response_model=ProjectRead,
    summary="Hand the project to somebody else",
    dependencies=[Depends(manage_projects)],
)
async def reassign_project(
    request: Request,
    project_id: UUID,
    payload: ProjectManagerChange,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> ProjectRead:
    """Administration and HR only, and its own endpoint for that reason.

    A project manager may change the project and may not change who manages it: the
    second would let them name an accomplice, or name themselves onto somebody
    else's project. The distinction is the kernel's privilege clause, reached
    through the same `can()` call as everything else in this file.
    """
    service = _service(session)
    await _manageable(request, service, project_id, principal)
    project = await service.reassign(project_id, payload.manager_employee_id, principal)
    return _read(project)


# --- tasks ------------------------------------------------------------------


@router.post(
    "/{project_id}/tasks",
    response_model=ProjectTaskRead,
    status_code=201,
    summary="Add a task to a project",
    dependencies=[Depends(manage_tasks)],
)
async def create_task(
    request: Request,
    project_id: UUID,
    payload: ProjectTaskCreate,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> ProjectTaskRead:
    service = _service(session)
    project = await _manageable(request, service, project_id, principal)
    task = await service.create_task(
        project_id,
        ProjectTaskInput(
            code=payload.code,
            name_es=payload.name_es,
            name_en=payload.name_en,
            is_billable=payload.is_billable,
            is_active=payload.is_active,
        ),
        principal,
    )
    return _task_read(task, project)


@router.patch(
    "/{project_id}/tasks/{task_id}",
    response_model=ProjectTaskRead,
    summary="Update a task",
    dependencies=[Depends(manage_tasks)],
)
async def update_task(
    request: Request,
    project_id: UUID,
    task_id: UUID,
    payload: ProjectTaskUpdate,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> ProjectTaskRead:
    """Change a task, including overriding the project's billable default for it.

    `is_billable: null` is not "leave it alone" — it is "follow the project again",
    which is the one way back from an override. An omitted key leaves it alone; see
    the schema.
    """
    service = _service(session)
    project = await _manageable(request, service, project_id, principal)
    task = await service.update_task(project_id, task_id, _task_patch(payload), principal)
    return _task_read(task, project)


@router.post(
    "/{project_id}/tasks/{task_id}/deactivate",
    response_model=ProjectTaskRead,
    summary="Switch a task off",
    dependencies=[Depends(manage_tasks)],
)
async def deactivate_task(
    request: Request,
    project_id: UUID,
    task_id: UUID,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> ProjectTaskRead:
    """No new time may be booked against this task; what was booked stays.

    Its own route rather than `PATCH {"is_active": false}`, because the refusal of an
    already-inactive task is part of the interface: "it is already off" and "I turned
    it off" are different answers, and a patch cannot tell them apart.
    """
    service = _service(session)
    project = await _manageable(request, service, project_id, principal)
    task = await service.deactivate_task(project_id, task_id, principal)
    return _task_read(task, project)


# --- what a time entry would record (ticket 28 writes the entry) -------------


@router.post(
    "/{project_id}/record-time",
    response_model=RecordTimeRead,
    summary="Ask what a time entry against a task would record",
    dependencies=[Depends(read_tasks)],
)
async def record_time(
    project_id: UUID,
    payload: RecordTimeRequest,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> RecordTimeRead:
    """The server's answer to "is this billable", and nothing else yet.

    This is the endpoint the ticket's requirement is about: a request that names a
    task cannot make it billable. `payload.is_billable` is read here for one purpose
    — echoing it back beside the recorded value — and the recorded value comes from
    `ProjectTask.billable_for(project)`, which reads the task's override and the
    project's default and nothing else.

    It answers rather than writes because `time_entries` arrives with ticket 28. What
    exists now is the decision, and the refusal: an inactive task, a project that is
    not active, or one outside the caller's reach is a catalogued 422 here, which is
    the guarantee the ticket asks ticket 28 to inherit.
    """
    target = await _service(session).resolve_record_target(
        principal,
        project_id,
        payload.task_id,
        claimed_billable=payload.is_billable,
    )
    return _record_read(target)


def _record_read(target: RecordTarget) -> RecordTimeRead:
    return RecordTimeRead(
        project_id=target.project.id,
        task_id=target.task.id,
        project_code=target.project.code,
        task_code=target.task.code,
        # The server's answer, straight off the domain object that resolved it.
        is_billable=target.is_billable,
        claimed_billable=target.claimed_billable,
        follows_project_default=target.task.is_billable is None,
    )


# --- request bodies to domain value objects ---------------------------------


def _input(payload: ProjectCreate, principal: Principal) -> ProjectInput:
    return ProjectInput(
        code=payload.code,
        name_es=payload.name_es,
        name_en=payload.name_en,
        department_id=payload.department_id,
        # The caller, never the body: see the schema.
        manager_employee_id=principal.employee_id,
        start_date=payload.start_date,
        client_name=payload.client_name,
        is_billable_default=payload.is_billable_default,
        end_date=payload.end_date,
        status=payload.status,
    )


def _patch(payload: ProjectUpdate) -> ProjectPatch:
    """Only the keys the client actually sent.

    `exclude_unset` is what keeps "omitted" apart from "explicitly null": the first
    leaves the field `UNSET` (leave alone) and the second carries a `None` (clear
    it). Merging the two is how a project's end date became impossible to reopen.
    """
    return ProjectPatch(**payload.model_dump(exclude_unset=True))


def _task_patch(payload: ProjectTaskUpdate) -> ProjectTaskPatch:
    return ProjectTaskPatch(**payload.model_dump(exclude_unset=True))


__all__ = ["router"]
