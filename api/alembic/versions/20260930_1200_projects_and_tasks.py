"""Projects and their tasks.

Revision ID: 0014
Revises: 0013
Created: 2026-09-30

Two tables (DESIGN §3.3), and five decisions worth reading before the DDL:

* **`projects.code` is unique, unconditionally.** The code is what a timesheet
  entry and an invoice line name, and both outlive the project; an archived project
  is still readable, so a code recycled after archiving would leave two projects
  answering to one name in a four-year-old record with no way to tell them apart.
  The partial-index trick `departments` uses for its own code ("unique among active
  siblings") is deliberately not used here.
* **`project_tasks.is_billable` is nullable, and NULL means "inherit the project's
  default".** Resolving on write would look tidier and would freeze today's default
  onto every task, so correcting the project could no longer reach the tasks that
  never overrode it. The resolution happens when an answer is needed — the response
  a client reads, and the `is_billable` column ticket 28's `time_entries` row will
  carry — and this column keeps the *decision* rather than a copy of an answer.
* **The status set is closed and `draft` is the default.** A project created and
  not yet started must not be bookable, and creating it `active` would leave a
  window in which time lands against something nobody agreed to run. `closed` is a
  finished project whose late time is still legitimate (the eight-week window,
  DESIGN §7.4); `archived` is one withdrawn from the catalogue, which accepts
  nothing at all.
* **No `ON DELETE CASCADE`, and nothing here is deletable.** A task is switched off
  and a project is archived. `project_tasks.project_id` is RESTRICT so that deleting
  a project cannot take the tasks a timesheet will point at; `time_entries.task_id`
  (ticket 28) will be RESTRICT for the same reason. The columns are RESTRICT rather
  than CASCADE because a cascade runs with the *referenced* table's owner privileges
  and would walk past any revoked DELETE, which is the argument migration 0012 makes
  for `attendance_events`.
* **No row-level policy.** A project is not a secret; what governs it is who may
  change it and what time may be booked against it, and the kernel decides both from
  columns this table already has. A policy could only express "the same department",
  which would refuse a manager the project they run from another one — narrower than
  the rule in one direction and wrong in the other.

No `GRANT` statement: migration 0007's default privileges cover tables added later,
which `tests/test_permission_matrix.py` asserts against a table created at test time.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name_es", sa.String(length=160), nullable=False),
        sa.Column("name_en", sa.String(length=160), nullable=False),
        sa.Column("client_name", sa.String(length=160), nullable=True),
        sa.Column("department_id", sa.UUID(), nullable=False),
        sa.Column("manager_employee_id", sa.UUID(), nullable=False),
        sa.Column(
            "is_billable_default",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "status", sa.String(length=16), server_default=sa.text("'draft'"), nullable=False
        ),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("created_by_employee_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("status IN ('draft', 'active', 'closed', 'archived')",
                           name="ck_projects_status"),
        sa.CheckConstraint(
            "end_date IS NULL OR end_date >= start_date", name="ck_projects_dates_ordered"
        ),
        sa.CheckConstraint("length(btrim(code)) > 0", name="ck_projects_code_not_blank"),
        # Not a partial index: see the module docstring. A unique *constraint*
        # rather than an index, because the repository relies on it by name.
        sa.UniqueConstraint("code", name="uq_projects_code"),
        # RESTRICT for every reference: a department or a person with projects is
        # deactivated, never deleted.
        sa.ForeignKeyConstraint(["department_id"], ["departments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["manager_employee_id"], ["employees.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_projects_department_status", "projects", ["department_id", "status"]
    )
    op.create_index("ix_projects_client_name", "projects", ["client_name"])
    op.create_index("ix_projects_manager", "projects", ["manager_employee_id"])

    op.create_table(
        "project_tasks",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name_es", sa.String(length=160), nullable=False),
        sa.Column("name_en", sa.String(length=160), nullable=False),
        # Nullable on purpose: NULL is "inherit", see the module docstring.
        sa.Column("is_billable", sa.Boolean(), nullable=True),
        sa.Column(
            "is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(btrim(code)) > 0", name="ck_project_tasks_code_not_blank"
        ),
        # Scoped to the project: `01` is a drawing number in one project and means
        # nothing in another.
        sa.UniqueConstraint("project_id", "code", name="uq_project_tasks_project_code"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_project_tasks_project_active", "project_tasks", ["project_id", "is_active"]
    )


def downgrade() -> None:
    # Tasks first: they reference the projects.
    op.drop_index("ix_project_tasks_project_active", table_name="project_tasks")
    op.drop_table("project_tasks")
    op.drop_index("ix_projects_manager", table_name="projects")
    op.drop_index("ix_projects_client_name", table_name="projects")
    op.drop_index("ix_projects_department_status", table_name="projects")
    op.drop_table("projects")
