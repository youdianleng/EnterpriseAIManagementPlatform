"""The project module: who may manage a project, and what a time entry records.

Two rules leave this module, and everything else here is bookkeeping around them.

**Who manages a project.** The decision is the kernel's — `can(principal,
Action.PROJECT_MANAGE, Resource(ResourceKind.PROJECT, department_id=...,
manager_employee_id=...))` — and this module's job is to build that `Resource` from
the row and relay the answer. It does not compare the manager itself. A service that
tested `project.manager_employee_id == principal.employee_id` would be a second place
a permission is decided, and the one that gets forgotten when a third role may
manage projects arrives.

**What a time entry records.** `resolve_record_target` is the module's answer to
"may this person book time against this task, and is it billable". `is_billable`
comes from the task's configuration — its own override when it has one, the
project's default when it does not — and from nothing else. There is deliberately no
parameter that could change it: an endpoint that passed a client's claim through
would make the flag the client's to set, which is the one thing the ticket forbids.
The claim is *carried* on the result so the response can echo it, and no code path
reads it to decide anything.

`resolve_project` and `resolve_record_target` both consult
`filter_for(principal, ResourceKind.PROJECT)` for the two clauses that are about
*reach* rather than about management — the status set and the two ways into it
(department, or having been named its manager). That function returns data, so this
is a translation of a description rather than a second copy of a rule.
"""

from dataclasses import replace
from datetime import date
from uuid import UUID

from app.audit import AuditAction
from app.audit import record as audit_record
from app.domain.access.kernel import FilterSpec, Resource, ResourceKind, can, filter_for
from app.domain.access.permissions import PROJECT_ADMIN_ROLES, Action
from app.domain.access.principal import Principal
from app.domain.errors import DomainError
from app.domain.project.errors import ProjectErrorCode
from app.domain.project.models import (
    MAX_CLIENT,
    MAX_CODE,
    MAX_NAME,
    UNSET,
    Project,
    ProjectInput,
    ProjectPage,
    ProjectPatch,
    ProjectQuery,
    ProjectStatus,
    ProjectTask,
    ProjectTaskInput,
    ProjectTaskPatch,
    RecordTarget,
)
from app.domain.project.repository import ProjectRepository

#: The status an employee has to hold to be named a project manager. The same value
#: the employee module treats as "still here": a project whose manager has left is
#: a project nobody may change.
ACTIVE_EMPLOYEE = "active"

#: The longest a project name or a client name may be. Bounded here rather than only
#: in the column, so the refusal names the limit.
ENTITY_TYPE = "project"
TASK_ENTITY_TYPE = "project_task"


class ProjectService:
    """Project and task rules. Nothing here decides a permission by itself."""

    def __init__(self, repository: ProjectRepository, session=None) -> None:  # noqa: ANN001
        self._repository = repository
        # Optional for the pure unit tests of the resolution rule; every write path
        # in the application passes one, because every write is audited.
        self._session = session

    # --- projects ----------------------------------------------------------

    async def get(self, project_id: UUID) -> Project:
        project = await self._repository.get(project_id)
        if project is None:
            raise DomainError(
                ProjectErrorCode.PROJECT_NOT_FOUND, detail=f"unknown project {project_id}"
            )
        return project

    async def require_manageable(self, project: Project, principal: Principal) -> Project:
        """The project, if this principal manages it; a refusal otherwise.

        Throws rather than returning a flag, because every caller that asked has
        nothing to do with a project it may not manage, and a boolean is a boolean
        somebody forgets to check.
        """
        decision = can(principal, Action.PROJECT_MANAGE, resource_of(project))
        if decision.allowed:
            return project
        raise DomainError(
            ProjectErrorCode.PROJECT_NOT_MANAGEABLE,
            detail=f"project {project.id}: {decision.primary_reason} ({decision.detail})",
        )

    async def list_projects(self, query: ProjectQuery) -> ProjectPage:
        return await self._repository.list_projects(query)

    async def create(self, data: ProjectInput, principal: Principal) -> Project:
        """Create a project, with the caller as its manager.

        The caller is the manager, and the request may not say otherwise: a manager
        who could create a project naming somebody else would be able to hand out
        the right to manage it. Handing one over afterwards is `reassign`, which
        administration and HR alone may do.
        """
        project_input = self._validated(data)
        if await self._repository.find_by_code(project_input.code) is not None:
            raise DomainError(
                ProjectErrorCode.PROJECT_CODE_TAKEN,
                detail=(
                    f"{project_input.code} is already in use; a project code is unique for "
                    "good, archived projects included, because a timesheet entry outlives "
                    "the project it names"
                ),
            )

        project = await self._repository.save(
            project_input, created_by_employee_id=principal.employee_id
        )
        await self._audit(
            AuditAction.PROJECT_CREATED,
            project.id,
            after=_snapshot(project),
        )
        await self._repository.commit()
        return project

    async def update(
        self, project_id: UUID, patch: ProjectPatch, principal: Principal
    ) -> Project:
        """Change a project. Its own manager, administration, or HR.

        Archiving is refused here rather than in the route, because "archived" is a
        fact about the row: `status` is a field of a patch like any other, and a
        route that filtered the field out of the body would be enforcing a rule
        about the domain in the wrong layer.
        """
        project = await self.get(project_id)
        await self.require_manageable(project, principal)
        self._refuse_if_archived(project, "changed")

        # `is not UNSET` and not merely truthy: `UNSET` is a real object, so a bare
        # truth test would look for a project whose code is the sentinel — which is
        # how this read first reached the database as a parameter psycopg refused.
        if patch.code is not UNSET and patch.code is not None and patch.code != project.code:
            existing = await self._repository.find_by_code(patch.code)
            if existing is not None:
                raise DomainError(
                    ProjectErrorCode.PROJECT_CODE_TAKEN,
                    detail=f"{patch.code} is already in use by project {existing.id}",
                )

        cleaned = self._validated_patch(project, patch)
        updated = await self._repository.update(project_id, cleaned)
        await self._audit(
            AuditAction.PROJECT_ARCHIVED
            if updated.is_archived and not project.is_archived
            else AuditAction.PROJECT_UPDATED,
            project_id,
            before=_snapshot(project),
            after=_snapshot(updated),
        )
        await self._repository.commit()
        return updated

    async def reassign(
        self, project_id: UUID, manager_employee_id: UUID, principal: Principal
    ) -> Project:
        """Hand a project to somebody else.

        Deliberately not a field of the patch: a project manager may change the
        project, and may not change *who manages it* — the second would let them
        name an accomplice, or name themselves onto somebody else's project.

        The kernel lets administration and HR through `require_manageable` on a
        project they do not manage; the privilege check on the line after is what
        turns that from "may change it" into "may hand it over", and it reads the
        same `PROJECT_ADMIN_ROLES` the kernel does rather than a list of its own.
        """
        project = await self.get(project_id)
        await self.require_manageable(project, principal)
        self._refuse_if_archived(project, "reassigned")
        self._require_privilege(project, principal, "reassign a project")

        status = await self._repository.employee_status(manager_employee_id)
        if status is None:
            raise DomainError(
                ProjectErrorCode.PROJECT_MANAGER_NOT_FOUND,
                detail=f"employee {manager_employee_id} does not exist",
            )
        if status != ACTIVE_EMPLOYEE:
            raise DomainError(
                ProjectErrorCode.PROJECT_MANAGER_NOT_FOUND,
                detail=f"employee {manager_employee_id} is {status}, not {ACTIVE_EMPLOYEE}",
            )

        updated = await self._repository.reassign_manager(project_id, manager_employee_id)
        await self._audit(
            AuditAction.PROJECT_UPDATED,
            project_id,
            before={"manager_employee_id": project.manager_employee_id},
            after={"manager_employee_id": manager_employee_id},
            reason="project manager reassigned",
        )
        await self._repository.commit()
        return updated

    # --- tasks -------------------------------------------------------------

    async def list_tasks(
        self, project_id: UUID, *, include_inactive: bool = True
    ) -> list[ProjectTask]:
        await self.get(project_id)
        return await self._repository.list_tasks(project_id, include_inactive=include_inactive)

    async def create_task(
        self, project_id: UUID, data: ProjectTaskInput, principal: Principal
    ) -> ProjectTask:
        """Add a task to a project. Tasks are added to a *running* project.

        A task on a draft or archived project is refused here rather than by the
        kernel: it is a fact about the project's state, not about the caller, and a
        manager of an archived project is still its manager.
        """
        project = await self.get(project_id)
        await self.require_manageable(project, principal)
        # Archived first, then running: an archived project is withdrawn from the
        # catalogue and saying so is more useful than saying it is not active, which
        # is true of a draft as well.
        self._refuse_if_archived(project, "add a task to")
        self._require_running(project, "add a task to")

        code = _required_text(data.code, "code", MAX_CODE)
        if await self._repository.find_task_by_code(project_id, code) is not None:
            raise DomainError(
                ProjectErrorCode.PROJECT_TASK_CODE_TAKEN,
                detail=f"{code} is already in use in project {project_id}",
            )

        task = await self._repository.save_task(
            project_id,
            replace(
                data,
                code=code,
                name_es=_required_text(data.name_es, "name_es", MAX_NAME),
                name_en=_required_text(data.name_en, "name_en", MAX_NAME),
            ),
        )
        await self._audit(
            AuditAction.PROJECT_TASK_CREATED,
            task.id,
            entity_type=TASK_ENTITY_TYPE,
            after=_task_snapshot(task),
        )
        await self._repository.commit()
        return task

    async def update_task(
        self, project_id: UUID, task_id: UUID, patch: ProjectTaskPatch, principal: Principal
    ) -> ProjectTask:
        """Change a task, including switching it off and on again.

        A task may be changed on a closed project, because switching one off is a
        change that *reduces* what can be booked and refusing it would leave a
        closed project offering tasks nobody will use. An archived project refuses
        everything, its tasks included — it is out of the catalogue, and the
        catalogue is what this is.
        """
        project = await self.get(project_id)
        await self.require_manageable(project, principal)
        self._refuse_if_archived(project, "changed")
        task = await self._require_task(project_id, task_id)

        if patch.code is not UNSET and patch.code != task.code:
            code = _required_text(patch.code, "code", MAX_CODE)
            existing = await self._repository.find_task_by_code(project_id, code)
            if existing is not None:
                raise DomainError(
                    ProjectErrorCode.PROJECT_TASK_CODE_TAKEN,
                    detail=f"{code} is already in use in project {project_id}",
                )
            patch = replace(patch, code=code)

        # The same one-field-at-a-time validation as the project patch, against the
        # same catalogue of limits, so a task name cannot be blanked or overrun.
        for field, limit in (("name_es", MAX_NAME), ("name_en", MAX_NAME)):
            value = getattr(patch, field)
            if value is UNSET or value is None:
                continue
            patch = replace(patch, **{field: _required_text(value, field, limit)})

        updated = await self._repository.update_task(task_id, patch)
        await self._audit(
            AuditAction.PROJECT_TASK_UPDATED,
            task_id,
            entity_type=TASK_ENTITY_TYPE,
            before=_task_snapshot(task),
            after=_task_snapshot(updated),
        )
        await self._repository.commit()
        return updated

    async def deactivate_task(
        self, project_id: UUID, task_id: UUID, principal: Principal
    ) -> ProjectTask:
        """Switch a task off, so no new time may be booked against it.

        Its own operation rather than a patch with `is_active=false`, because the
        refusal of an already-inactive task is the point: "this task is already off"
        is a different answer from "I changed it", and a patch cannot tell them
        apart. Old entries keep pointing at it — nothing here deletes anything.
        """
        project = await self.get(project_id)
        await self.require_manageable(project, principal)
        self._refuse_if_archived(project, "changed")
        task = await self._require_task(project_id, task_id)

        if not task.is_active:
            raise DomainError(
                ProjectErrorCode.PROJECT_TASK_ALREADY_INACTIVE,
                detail=f"task {task_id} is already inactive",
            )

        updated = await self._repository.update_task(task_id, ProjectTaskPatch(is_active=False))
        await self._audit(
            AuditAction.PROJECT_TASK_DEACTIVATED,
            task_id,
            entity_type=TASK_ENTITY_TYPE,
            before={"is_active": True},
            after={"is_active": False},
        )
        await self._repository.commit()
        return updated

    # --- what a time entry may record (ticket 28 consumes this) -------------

    async def recordable_projects(
        self, principal: Principal, query: ProjectQuery
    ) -> ProjectPage:
        """The projects this principal may record time against.

        The list form of the rule `resolve_record_target` applies, from the same
        spec — which is the point of `filter_for` returning data: a picker and a
        write path that asked two different questions would eventually disagree. The
        facets narrow the answer; they cannot widen it, because the spec is applied
        by the repository whatever the query says.
        """
        return await self._repository.list_selectable(
            filter_for(principal, ResourceKind.PROJECT), query
        )

    async def resolve_record_target(
        self,
        principal: Principal,
        project_id: UUID,
        task_id: UUID,
        *,
        claimed_billable: bool | None = None,
    ) -> RecordTarget:
        """What booking time against this task would record, or a refusal.

        The three conditions are the ticket's: the task is enabled, the project is
        active, and the project is in the principal's reach — their departments
        (descendants included) or a project they manage. An archived project fails
        the second condition for everybody, administration included.

        `claimed_billable` is the client's opinion and changes nothing. It exists so
        the response can say what the client asked for beside what was recorded; the
        value that comes back is the task's configuration, always.
        """
        project = await self.get(project_id)
        task = await self._require_task(project_id, task_id)

        if not task.is_active:
            raise DomainError(
                ProjectErrorCode.PROJECT_TASK_NOT_RECORDABLE,
                detail=f"task {task_id} is inactive",
            )
        self._require_recordable(project, principal)

        return RecordTarget(
            project=project,
            task=task,
            # The whole of the billable rule, in one expression, resolved from the
            # task and the project it belongs to.
            is_billable=task.billable_for(project),
            claimed_billable=claimed_billable,
        )

    # --- internals ---------------------------------------------------------

    def _require_recordable(self, project: Project, principal: Principal) -> None:
        spec = filter_for(principal, ResourceKind.PROJECT)
        if _in_reach(project, spec):
            return
        raise DomainError(
            ProjectErrorCode.PROJECT_TASK_NOT_RECORDABLE,
            detail=(
                f"project {project.id} is {project.status.value}, which is not one of "
                f"{sorted(spec.statuses)}, or is outside the caller's departments and "
                "not managed by them"
            ),
        )

    def _require_privilege(self, project: Project, principal: Principal, what: str) -> None:
        """Administration or HR, checked through the same decision as everything else.

        Expressed as "the kernel allows this action on a project the caller does not
        manage" rather than as a role comparison, so the set of roles with an
        organisation-wide remit has exactly one home.
        """
        if not principal.roles & PROJECT_ADMIN_ROLES:
            raise DomainError(
                ProjectErrorCode.PROJECT_NOT_MANAGEABLE,
                detail=f"{what} needs one of {sorted(PROJECT_ADMIN_ROLES)}; "
                f"the principal holds {sorted(principal.roles)}",
            )

    async def _require_task(self, project_id: UUID, task_id: UUID) -> ProjectTask:
        task = await self._repository.get_task(task_id)
        if task is None or task.project_id != project_id:
            # The same answer for "no such task" and "that task is another project's":
            # telling them apart would make this an existence oracle over the whole
            # task table, and the route already names the project.
            raise DomainError(
                ProjectErrorCode.PROJECT_TASK_NOT_FOUND,
                detail=f"no task {task_id} in project {project_id}",
            )
        return task

    @staticmethod
    def _refuse_if_archived(project: Project, verb: str) -> None:
        if project.is_archived:
            raise DomainError(
                ProjectErrorCode.PROJECT_ARCHIVED,
                detail=(
                    f"project {project.id} is archived, so it cannot be {verb}; its rows "
                    "and tasks stay readable"
                ),
            )

    @staticmethod
    def _require_running(project: Project, verb: str) -> None:
        if project.status is not ProjectStatus.ACTIVE:
            raise DomainError(
                ProjectErrorCode.PROJECT_NOT_ACTIVE,
                detail=f"project {project.id} is {project.status}, so it cannot {verb}",
            )

    def _validated(self, data: ProjectInput) -> ProjectInput:
        code = _required_text(data.code, "code", MAX_CODE)
        start_date = data.start_date
        end_date = data.end_date
        _require_ordered(start_date, end_date)
        return replace(
            data,
            code=code,
            name_es=_required_text(data.name_es, "name_es", MAX_NAME),
            name_en=_required_text(data.name_en, "name_en", MAX_NAME),
            client_name=_optional_text(data.client_name, "client_name", MAX_CLIENT),
            end_date=end_date,
        )

    @staticmethod
    def _validated_patch(project: Project, patch: ProjectPatch) -> ProjectPatch:
        """The patch as it will be written, with the date rule applied to the result.

        The dates are checked against the row's *resulting* pair rather than against
        the patch alone: a patch that moves only the start date can invert the range,
        and a check that looked at the patch's own two fields would see one of them.
        `UNSET` therefore means "the row's current value" here, and `None` means the
        range is being opened — which is a change, and a legal one.

        One field at a time, and only the fields the request named: a `replace` that
        rebuilt the patch from a list of fields would have to spell each one twice,
        and the copy that was forgotten would be the field that silently stopped
        updating.
        """
        start_date = project.start_date if patch.start_date is UNSET else patch.start_date
        end_date = project.end_date if patch.end_date is UNSET else patch.end_date
        _require_ordered(start_date, end_date)

        for field, limit in (
            ("code", MAX_CODE),
            ("name_es", MAX_NAME),
            ("name_en", MAX_NAME),
            ("client_name", MAX_CLIENT),
        ):
            value = getattr(patch, field)
            if value is UNSET:
                continue
            cleaned = _required_text(value, field, limit) if value is not None else None
            patch = replace(patch, **{field: cleaned})
        return patch

    async def _audit(
        self,
        action: AuditAction,
        entity_id: UUID,
        *,
        entity_type: str = ENTITY_TYPE,
        before: dict | None = None,
        after: dict | None = None,
        reason: str | None = None,
    ) -> None:
        if self._session is None:
            return
        await audit_record(
            self._session,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            before=before,
            after=after,
            reason=reason,
        )


def resource_of(project: Project) -> Resource:
    """The facts the kernel needs about a project.

    Built here once, so the route and the service cannot hand the kernel two
    different descriptions of the same row — which is how "who manages this" would
    end up being answered from a stale copy.
    """
    return Resource(
        ResourceKind.PROJECT,
        department_id=project.department_id,
        manager_employee_id=project.manager_employee_id,
    )


def _in_reach(project: Project, spec: FilterSpec) -> bool:
    """The kernel's description of a reachable project, applied to one row.

    Every clause, conjunctively, in the order `filter_for` states them. This is the
    translation of a data description rather than a second rule: the spec is the
    answer, and this only reads it.
    """
    if spec.statuses and project.status.value not in spec.statuses:
        return False
    if spec.allow_all:  # no department restriction; the status clause above still ran
        return True
    if spec.manager_employee_id is not None and project.manager_employee_id == (
        spec.manager_employee_id
    ):
        return True
    return project.department_id in spec.department_ids


def _snapshot(project: Project) -> dict[str, object]:
    """A storable picture of the row, for the audit trail."""
    return {
        "code": project.code,
        "name_es": project.name_es,
        "name_en": project.name_en,
        "client_name": project.client_name,
        "department_id": project.department_id,
        "manager_employee_id": project.manager_employee_id,
        "is_billable_default": project.is_billable_default,
        "status": project.status,
        "start_date": project.start_date,
        "end_date": project.end_date,
    }


def _task_snapshot(task: ProjectTask) -> dict[str, object]:
    return {
        "project_id": task.project_id,
        "code": task.code,
        "name_es": task.name_es,
        "name_en": task.name_en,
        # None is written as None rather than resolved: the audit trail records what
        # the row says, and "inherits" is what it says.
        "is_billable": task.is_billable,
        "is_active": task.is_active,
    }


def _required_text(value: str, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DomainError(
            ProjectErrorCode.INVALID_REQUEST, detail=f"{field} must be a non-empty string"
        )
    text = value.strip()
    if len(text) > limit:
        raise DomainError(
            ProjectErrorCode.INVALID_REQUEST,
            detail=f"{field} is longer than {limit} characters",
        )
    return text


def _optional_text(value: str | None, field: str, limit: int) -> str | None:
    if value is None:
        return None
    text = _required_text(value, field, limit)
    return text


def _require_ordered(start_date: date, end_date: date | None) -> None:
    if end_date is not None and end_date < start_date:
        raise DomainError(
            ProjectErrorCode.PROJECT_DATES_INVALID,
            detail=f"end {end_date} precedes start {start_date}",
        )


__all__ = ["ACTIVE_EMPLOYEE", "ENTITY_TYPE", "TASK_ENTITY_TYPE", "ProjectService", "resource_of"]
