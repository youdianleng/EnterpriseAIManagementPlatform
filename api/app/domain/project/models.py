"""Project and task value objects.

Two tables, and four decisions that are worth reading before the code:

* **A project's code is unique for good, not unique among live projects.** The code
  is the project's identity in the two records that outlive it — a timesheet entry
  and an invoice line — so a code that could be recycled after archiving would make
  "which project was this" a question with two answers and no way to tell them
  apart. The archived project stays readable, which is what makes the collision
  real rather than theoretical.
* **A task's code is unique within its project.** `01` is a drawing number in one
  project and means nothing in another; scoping the code to the project is what
  lets a client's numbering be recorded as the client numbers it.
* **`is_billable` is nullable, and NULL means "inherit the project's default".**
  Resolving it on write would look tidier and would be wrong: a task that has not
  decided is *following the project*, so that when the project's default is
  corrected the tasks that never overrode it follow the correction. A stored copy
  would freeze today's default onto every task and quietly make the project's own
  setting unable to change its mind. The resolution happens at the two points that
  need an answer — the response a client reads, and the row a time entry will
  write (ticket 28) — and `RecordTarget` is the second of those.
* **The status set is closed, and `draft` is the default.** A project that has been
  created and not yet started must not be bookable, and the alternative — creating
  it `active` and letting somebody archive it in a second request — leaves a window
  in which time can be recorded against something nobody has agreed to run.

`closed` and `archived` are deliberately different words rather than one flag. A
closed project is finished work whose late time is still legitimate — the eight-week
supplementary window (DESIGN §7.4) is exactly for it — and an archived one is withdrawn
from the catalogue altogether: no new time for anybody, and no changes to it. The
visibility rule below refuses both, because ticket 27 is about *new* records; the
difference matters to ticket 28, whose supplementary-filing path will have to tell
them apart.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from uuid import UUID


class ProjectStatus(StrEnum):
    """The states a project can be in. Closed set, and the database says so too."""

    #: Created and not running yet. Not bookable, and tasks may not be added yet
    #: either: a task on a project nobody has agreed to run is a plan, and this
    #: module does not keep plans.
    DRAFT = "draft"
    #: Running. The only status time may be recorded against.
    ACTIVE = "active"
    #: Finished. Readable, and closed to new time — see the module docstring for
    #: why it is not the same state as `archived`.
    CLOSED = "closed"
    #: Withdrawn from the catalogue. Readable for ever, closed to new time and to
    #: every change to its own configuration.
    ARCHIVED = "archived"


#: The statuses that may receive new time (ticket 27's 已启用). Spelled out rather
#: than derived by subtraction, because a status added later must be *named* here
#: before it becomes bookable — the failure mode of a derived set is a new state
#: that quietly accepts time.
RECORDABLE_STATUSES: frozenset[ProjectStatus] = frozenset({ProjectStatus.ACTIVE})

#: The project's own immutable-ish identity: unique for good, see the module
#: docstring. Bounded so a code stays a code rather than becoming a description.
MAX_CODE = 64
MAX_NAME = 160
MAX_CLIENT = 160


class Unset:
    """The type of `UNSET`: a distinct object, never a `None` in disguise."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "UNSET"


#: "This field was not in the patch." One module-level instance, compared by
#: identity, so a caller cannot accidentally construct a second one.
UNSET = Unset()

#: Every field a patch may carry. Written out rather than derived with
#: `dataclasses.fields` so that the repository's loop and the service's validation
#: are reading the same list, and a field added to the dataclass but forgotten in
#: one of them is a failing test rather than a column that never updates.
PATCH_FIELDS: tuple[str, ...] = (
    "code",
    "name_es",
    "name_en",
    "client_name",
    "department_id",
    "is_billable_default",
    "status",
    "start_date",
    "end_date",
)


@dataclass(frozen=True, slots=True)
class Project:
    """One project, as stored."""

    id: UUID
    code: str
    name_es: str
    name_en: str
    #: The client the work is for. Empty means internal, which is a real answer:
    #: not every project is billed to somebody outside the company.
    client_name: str | None
    department_id: UUID
    manager_employee_id: UUID
    is_billable_default: bool
    status: ProjectStatus
    start_date: date
    end_date: date | None
    created_at: datetime
    updated_at: datetime

    @property
    def is_archived(self) -> bool:
        return self.status is ProjectStatus.ARCHIVED

    @property
    def is_recordable(self) -> bool:
        """Whether this project may receive new time at all."""
        return self.status in RECORDABLE_STATUSES

    @property
    def effective_billable(self) -> bool:
        """The project's own answer to "is this work billable".

        A status that no longer accepts time is not billable either: `CLOSED` keeps
        a project readable, and reporting a finished project as billable work while
        refusing every new entry against it would be two answers to one question.
        """
        return self.is_billable_default and self.status is ProjectStatus.ACTIVE


@dataclass(frozen=True, slots=True)
class ProjectTask:
    """One task, as stored. `is_billable` is None when it inherits."""

    id: UUID
    project_id: UUID
    code: str
    name_es: str
    name_en: str
    #: None means "whatever the project says", and it is stored as None on purpose:
    #: see the module docstring.
    is_billable: bool | None
    is_active: bool
    created_at: datetime
    updated_at: datetime

    def billable_for(self, project: Project) -> bool:
        """The one place inheritance is resolved.

        Called with the project the task belongs to, never with one the caller
        chose: resolving a task against a different project's default is how a task
        becomes billable by accident.
        """
        if self.is_billable is None:
            return project.effective_billable
        return self.is_billable


@dataclass(frozen=True, slots=True)
class ProjectInput:
    """A project to write."""

    code: str
    name_es: str
    name_en: str
    department_id: UUID
    manager_employee_id: UUID
    start_date: date
    client_name: str | None = None
    is_billable_default: bool = True
    end_date: date | None = None
    #: Defaults to `draft` rather than `active`: see the module docstring. A caller
    #: that means "start now" says so.
    status: ProjectStatus = ProjectStatus.DRAFT

@dataclass(frozen=True, slots=True)
class ProjectPatch:
    """What to change.

    `UNSET` means "leave alone", which is deliberately *not* the same as `None`:
    clearing a client name and setting an end date back to open are real operations,
    and a patch convention that read `null` as "leave alone" would make both
    impossible to express — the failure the department module's separate
    `PUT /manager` endpoint exists to work around. The API builds this from
    Pydantic's `exclude_unset`, so "omitted" and "explicitly null" survive the
    trip.

    A patch is not a place to state the manager: handing a project to somebody else
    is `reassign`, which is administration's and HR's alone. Making it a field here
    would mean the manager of a project could appoint themselves out of, or into, a
    project — the second of which is the escalation the rule exists to prevent.
    """

    code: str | None | Unset = UNSET
    name_es: str | None | Unset = UNSET
    name_en: str | None | Unset = UNSET
    client_name: str | None | Unset = UNSET
    department_id: UUID | None | Unset = UNSET
    is_billable_default: bool | None | Unset = UNSET
    status: ProjectStatus | None | Unset = UNSET
    start_date: date | None | Unset = UNSET
    end_date: date | None | Unset = UNSET


@dataclass(frozen=True, slots=True)
class ProjectTaskInput:
    """A task to write.

    No `is_billable` from the request path: the field is on this object because a
    project manager may override the project's default per task (the ticket says
    so), and the service is the only caller that may construct one from a request
    that an employee sent.
    """

    code: str
    name_es: str
    name_en: str
    is_billable: bool | None = None
    is_active: bool = True


@dataclass(frozen=True, slots=True)
class ProjectTaskPatch:
    """A task change.

    `UNSET` means "leave alone", for the reason `ProjectPatch` gives, and it is the
    default of every field. In particular `is_billable=False` is a *value* — a task
    overridden to unbillable — and `is_billable=None` is another one, "follow the
    project again"; neither may be confused with a request that never mentioned the
    field, which is the whole reason `None` could not be the absent value here.
    """

    code: str | None | Unset = UNSET
    name_es: str | None | Unset = UNSET
    name_en: str | None | Unset = UNSET
    is_billable: bool | None | Unset = UNSET
    is_active: bool | None | Unset = UNSET


@dataclass(frozen=True, slots=True)
class ProjectQuery:
    """What the list endpoint is asked for. Filters are conjunctive."""

    department_id: UUID | None = None
    status: ProjectStatus | None = None
    client_name: str | None = None
    limit: int = 50
    offset: int = 0


@dataclass(frozen=True, slots=True)
class RecordTarget:
    """What a time entry against a task would record (ticket 28 consumes this).

    **`is_billable` is the server's answer.** It is stored rather than derived from
    `task` and `project` at the point of use so that a caller cannot re-resolve it
    from something else, and so that "the value recorded comes from the task's
    configuration" is one line a test can hold: nothing in the construction path
    reads a request.

    The pair of rows travels with it so ticket 28 can write a `time_entries` row
    without a second read, and so a caller that has been handed one has no reason
    to go looking for a project it might resolve the flag against instead.
    """

    project: Project
    task: ProjectTask
    #: Resolved: the task's override when it has one, the project's default when it
    #: does not.
    is_billable: bool
    #: What the client claimed, when it claimed anything. Carried for one reason:
    #: the API echoes it next to the value it recorded, so a client that sent
    #: `is_billable=true` for an unbillable task can see that the server disagreed
    #: rather than having to infer it. It is never read to decide anything.
    claimed_billable: bool | None = None


@dataclass(frozen=True, slots=True)
class ProjectPage:
    items: list[Project] = field(default_factory=list)
    total: int = 0
    limit: int = 50
    offset: int = 0


__all__ = [
    "MAX_CLIENT",
    "MAX_CODE",
    "MAX_NAME",
    "PATCH_FIELDS",
    "RECORDABLE_STATUSES",
    "UNSET",
    "Project",
    "ProjectInput",
    "ProjectPage",
    "ProjectPatch",
    "ProjectQuery",
    "ProjectStatus",
    "ProjectTask",
    "ProjectTaskInput",
    "ProjectTaskPatch",
    "RecordTarget",
    "Unset",
]
