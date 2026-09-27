"""Persistence contract for the project module.

Three things about this interface are load-bearing:

* **Nothing commits.** The service commits once, so a project and the audit record
  of who created it land together or not at all. A project nobody can account for
  is exactly the row an incident review cannot explain.
* **`find_by_code` takes an optional project id.** Codes are unique for good, so
  the uniqueness check is a lookup by code alone; the id is passed when a *patch*
  changes a code, so a project may keep its own.
* **`list` returns the page and the total together.** A caller that counted
  separately would have to repeat the filter, and the two would eventually be
  written against different ones — the defect being a page count that disagrees
  with the page.
"""

from typing import Protocol
from uuid import UUID

from app.domain.access.kernel import FilterSpec
from app.domain.project.models import (
    Project,
    ProjectInput,
    ProjectPage,
    ProjectPatch,
    ProjectQuery,
    ProjectTask,
    ProjectTaskInput,
    ProjectTaskPatch,
)


class ProjectRepository(Protocol):
    async def get(self, project_id: UUID) -> Project | None: ...

    async def find_by_code(self, code: str) -> Project | None:
        """The project holding this code, archived ones included: the uniqueness is
        for good, so an archived row is a collision rather than free space."""
        ...

    async def list_projects(self, query: ProjectQuery) -> ProjectPage:
        """One page of the catalogue, with its total.

        Named `list_projects` rather than `list`: a method called `list` shadows the
        built-in inside this class body, and the annotations below it are evaluated
        there — `-> list[ProjectTask]` then fails at import with "'function' object is
        not subscriptable", which is a confusing way to learn about name shadowing.
        """
        ...

    async def list_selectable(self, spec: FilterSpec, query: ProjectQuery) -> ProjectPage:
        """The projects a principal may record time against, narrowed by facets.

        The one read in this module that takes a `FilterSpec`, and the spec is not
        optional: the facets are a catalogue's questions ("this department, this
        status, this client") and the spec is the permission, and a signature that
        allowed one without the other is how a filter-free query gets written. Every
        field of the spec is applied — a store that used `department_ids` alone would
        drop the projects a manager runs from another department, and one that
        ignored `statuses` would offer an archived project.

        A page rather than a list because the picker it serves is paginated; the same
        spec without facets is what a write path checks a single row against, through
        the service rather than here.
        """
        ...

    async def save(self, data: ProjectInput, *, created_by_employee_id: UUID) -> Project: ...

    async def update(self, project_id: UUID, patch: ProjectPatch) -> Project: ...

    async def reassign_manager(self, project_id: UUID, manager_employee_id: UUID) -> Project:
        """Hand the project to somebody else. Administration and HR alone."""
        ...

    async def get_task(self, task_id: UUID) -> ProjectTask | None: ...

    async def find_task_by_code(self, project_id: UUID, code: str) -> ProjectTask | None: ...

    async def list_tasks(
        self, project_id: UUID, *, include_inactive: bool = True
    ) -> list[ProjectTask]: ...

    async def save_task(self, project_id: UUID, data: ProjectTaskInput) -> ProjectTask: ...

    async def update_task(self, task_id: UUID, patch: ProjectTaskPatch) -> ProjectTask: ...

    async def department_exists(self, department_id: UUID) -> bool: ...

    async def employee_status(self, employee_id: UUID) -> str | None:
        """`employees.status`, or None when there is no such employee."""
        ...

    async def commit(self) -> None: ...


__all__ = ["ProjectRepository"]
