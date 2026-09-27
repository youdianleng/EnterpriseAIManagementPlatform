"""Project and task API schemas.

Two shapes here carry a decision rather than a field list.

**`ProjectUpdate` and `ProjectTaskUpdate` distinguish "omitted" from "explicitly
null"** — Pydantic's `exclude_unset` is what preserves it, and the domain's `UNSET`
is where it lands. Without it, clearing a client name or reopening a project's end
date would be unsayable: a patch that read `null` as "leave alone" can only ever add
information.

**`is_billable` appears on the record-time request and is not a field of the
answer's business.** It is accepted because a real form *does* post it back — the
checkbox the employee saw — and refusing the key outright would turn an honest client
into a 422. It is echoed beside the value the server recorded, so a client that
claimed `true` for an unbillable task is told, in the response, that the server
disagreed. `ProjectTaskRead.is_billable_effective` is the same idea at rest: the
stored `is_billable` (null meaning inherit) next to the resolved answer, because a
reader that had to resolve the inheritance itself would be a second implementation
of it.
"""

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.api.v1.schemas.base import StrictModel
from app.domain.project.models import ProjectStatus


class ProjectCreate(StrictModel):
    """A project to create.

    No `manager_employee_id`: the caller is the manager. A manager who could create
    a project naming somebody else would be handing out the right to manage it, and
    handing one over afterwards is `reassign`, which administration and HR own.
    """

    code: str = Field(min_length=1, max_length=64)
    name_es: str = Field(min_length=1, max_length=160)
    name_en: str = Field(min_length=1, max_length=160)
    department_id: UUID
    start_date: date
    client_name: str | None = Field(default=None, max_length=160)
    end_date: date | None = None
    is_billable_default: bool = True
    #: Defaults to `draft`. A caller that means "start now" says `active`; the
    #: default is the safe direction, because an active project accepts time.
    status: ProjectStatus = ProjectStatus.DRAFT


class ProjectUpdate(StrictModel):
    """A change. An omitted key means "leave alone"; an explicit `null` means
    "clear it", which is the only way to reopen an end date or drop a client."""

    code: str | None = Field(default=None, min_length=1, max_length=64)
    name_es: str | None = Field(default=None, min_length=1, max_length=160)
    name_en: str | None = Field(default=None, min_length=1, max_length=160)
    client_name: str | None = Field(default=None, max_length=160)
    department_id: UUID | None = None
    is_billable_default: bool | None = None
    status: ProjectStatus | None = None
    start_date: date | None = None
    end_date: date | None = None


class ProjectManagerChange(StrictModel):
    """Who runs the project. Its own endpoint because a manager may change the
    project and may not change who manages it."""

    manager_employee_id: UUID


class ProjectTaskCreate(StrictModel):
    """A task to add.

    `is_billable` is tri-state on purpose: omitted or `null` means "follow the
    project's default", `true` and `false` are an explicit override. This is the
    project manager's field — the person who configures which work is billable —
    and it is *not* the employee's, who never reaches this endpoint.
    """

    code: str = Field(min_length=1, max_length=64)
    name_es: str = Field(min_length=1, max_length=160)
    name_en: str = Field(min_length=1, max_length=160)
    is_billable: bool | None = None
    is_active: bool = True


class ProjectTaskUpdate(StrictModel):
    """A task change. `null` for `is_billable` means "follow the project again",
    which is a real change and not the same as leaving it alone."""

    code: str | None = Field(default=None, min_length=1, max_length=64)
    name_es: str | None = Field(default=None, min_length=1, max_length=160)
    name_en: str | None = Field(default=None, min_length=1, max_length=160)
    is_billable: bool | None = None
    is_active: bool | None = None


class ProjectTaskRead(BaseModel):
    """One task, with the billable question answered rather than posed.

    `is_billable` is what the row says (`null` = inherit) and
    `is_billable_effective` is what a time entry would record. Both travel, because
    the first is the configuration a manager edits and the second is the answer a
    timesheet depends on, and a client that had to derive one from the other would
    be re-implementing the inheritance.
    """

    id: UUID
    project_id: UUID
    code: str
    name_es: str
    name_en: str
    is_billable: bool | None
    is_billable_effective: bool
    is_active: bool
    created_at: datetime
    updated_at: datetime


class ProjectRead(BaseModel):
    id: UUID
    code: str
    name_es: str
    name_en: str
    client_name: str | None
    department_id: UUID
    manager_employee_id: UUID
    is_billable_default: bool
    status: ProjectStatus
    start_date: date
    end_date: date | None
    created_at: datetime
    updated_at: datetime


class ProjectDetail(ProjectRead):
    """A project with its tasks, so one request fills the project page."""

    tasks: list[ProjectTaskRead] = Field(default_factory=list)


class ProjectPage(BaseModel):
    items: list[ProjectRead]
    total: int
    limit: int
    offset: int


class RecordTimeRequest(StrictModel):
    """What a timesheet entry will record against a task (ticket 27's server rule).

    The task is named in the body rather than in the path, which is the shape ticket
    28 needs anyway: an entry names a project, a task, minutes and a date, and the
    project and the task are two fields of one document rather than two levels of a
    URL.

    `is_billable` is accepted and **never** believed: the value recorded comes from
    the task's configuration. It is here so that a form which posts its own checkbox
    back is not refused for saying something the server was going to ignore anyway —
    and so the response can show the client what it claimed next to what was stored.

    `minutes` and `note` are the entry's own fields and belong to ticket 28; they are
    not accepted yet, because accepting a payload this module does not write would
    be a promise the record cannot keep.
    """

    task_id: UUID
    is_billable: bool | None = Field(
        default=None,
        description=(
            "Ignored. The recorded value comes from the task's configuration; the "
            "field is echoed back beside it."
        ),
    )


class RecordTimeRead(BaseModel):
    """What would be recorded, and who decided it.

    `is_billable` is the server's answer. `claimed_billable` is what the request
    said, returned only so a client can see the two differ.
    """

    project_id: UUID
    task_id: UUID
    project_code: str
    task_code: str
    is_billable: bool
    claimed_billable: bool | None
    #: True when the task has no override of its own and is following the project.
    follows_project_default: bool
