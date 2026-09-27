"""Project and task models.

Two tables for 项目 → 任务 (DESIGN §3.3), and the columns that carry a decision:

* **`projects.code` is unique, unconditionally.** Not unique among live projects:
  the code is what a `time_entries` row and an invoice line name, and both outlive
  the project. A code recycled after archiving would make two projects answer to one
  name in a four-year-old record, and the archived row is still there to be
  confused with — which is what makes this a constraint rather than a preference.
* **`project_tasks.is_billable` is nullable, and NULL means inherit.** The
  inheritance is resolved when an answer is needed, never stored: a task that has
  not decided is *following the project*, so correcting the project's default has to
  reach it. Storing the resolved value would freeze today's default onto every task.
* **`project_tasks.code` is unique within the project**, matching how a client
  numbers their own work.
* **No `ON DELETE CASCADE` anywhere.** A task may not be deleted while it is
  referenced, and nothing here is deletable through the API: a task is switched off
  and a project is archived. `project_tasks.project_id` is RESTRICT for the same
  reason `time_entries.task_id` will be — deleting a project would take the record
  of what was billed against it.

No row-level policy, deliberately. A project is not a secret: the rule that governs
it is *who may change it* and *what time may be booked against it*, and both are
decided in the kernel from columns this table already has. A policy here would be a
second, coarser copy of the visibility rule (ticket 28 is where a row has to be
protected), and a policy that could only express "the same department" would refuse
a manager the project they run from another one.
"""

from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db_metadata import Base

#: The closed status set, as the database states it. Written out rather than built
#: from the enum, so a status added to the module is a migration rather than a
#: silent widening of what the column accepts.
STATUSES_SQL = "('draft', 'active', 'closed', 'archived')"


class Project(Base):
    """One project."""

    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint(f"status IN {STATUSES_SQL}", name="ck_projects_status"),
        CheckConstraint(
            "end_date IS NULL OR end_date >= start_date",
            name="ck_projects_dates_ordered",
        ),
        CheckConstraint("length(btrim(code)) > 0", name="ck_projects_code_not_blank"),
        # Unique for good, not among live rows: see the module docstring.
        UniqueConstraint("code", name="uq_projects_code"),
        # The filter the list endpoint offers: by department, by status, by client.
        Index("ix_projects_department_status", "department_id", "status"),
        Index("ix_projects_client_name", "client_name"),
        # "The projects this person manages" is asked on every project write.
        Index("ix_projects_manager", "manager_employee_id"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name_es: Mapped[str] = mapped_column(String(160), nullable=False)
    name_en: Mapped[str] = mapped_column(String(160), nullable=False)
    #: Null means internal work. Not every project is billed to somebody outside.
    client_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    #: The owning department. RESTRICT, like every other reference to the tree: a
    #: department with projects in it is deactivated, never deleted.
    department_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("departments.id", ondelete="RESTRICT"),
        nullable=False,
    )
    #: Who runs it. Required, and a real employee: a project whose manager is
    #: unset is a project nobody but administration may change.
    manager_employee_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="RESTRICT"),
        nullable=False,
    )
    is_billable_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_by_employee_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Project {self.code} {self.status}>"


class ProjectTask(Base):
    """One task, one level below a project."""

    __tablename__ = "project_tasks"
    __table_args__ = (
        CheckConstraint("length(btrim(code)) > 0", name="ck_project_tasks_code_not_blank"),
        # Scoped to the project: the same code in two projects is two different
        # tasks, which is how a client's own numbering is recorded as they write it.
        UniqueConstraint("project_id", "code", name="uq_project_tasks_project_code"),
        # The read that matters: the tasks of one project, the live ones first.
        Index("ix_project_tasks_project_active", "project_id", "is_active"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name_es: Mapped[str] = mapped_column(String(160), nullable=False)
    name_en: Mapped[str] = mapped_column(String(160), nullable=False)
    #: NULL is "inherit the project's default", and it is a value rather than a
    #: missing one: see the module docstring.
    is_billable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ProjectTask {self.code} active={self.is_active}>"


__all__ = ["STATUSES_SQL", "Project", "ProjectTask"]
