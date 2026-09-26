"""Departments: the organisation tree.

Revision ID: 0002
Revises: 0001
Created: 2026-09-26

`path` is an ltree materialised path and `depth` is denormalised from it. Both
exist for the same reason: "this department and all of its descendants" is asked
by every permission decision, and it must be one indexed comparison rather than
a recursive walk.

Code uniqueness is a partial unique index on `(parent_id, code) WHERE is_active`
rather than a global UNIQUE. A code belongs to a position in the tree, so the
same code under two different parents is legitimate, and a deactivated
department should not permanently reserve its code.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "departments",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name_es", sa.String(length=160), nullable=False),
        sa.Column("name_en", sa.String(length=160), nullable=False),
        sa.Column("parent_id", sa.UUID(), nullable=True),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.Column("clearance_level", sa.String(length=16), nullable=False),
        sa.Column("manager_employee_id", sa.UUID(), nullable=True),
        sa.Column("cost_center", sa.String(length=64), nullable=True),
        sa.Column("description_es", sa.Text(), nullable=True),
        sa.Column("description_en", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("depth >= 0", name="ck_departments_depth_non_negative"),
        sa.CheckConstraint(
            "clearance_level IN ('low', 'medium', 'high')",
            name="ck_departments_clearance_level",
        ),
        sa.ForeignKeyConstraint(["parent_id"], ["departments.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )

    # Declared as ltree here rather than relying on the model's UserDefinedType,
    # so the migration states the real column type even if the model changes.
    op.execute("ALTER TABLE departments ALTER COLUMN path TYPE ltree USING path::ltree")

    op.create_index("ix_departments_path", "departments", ["path"])
    op.create_index("ix_departments_parent_id", "departments", ["parent_id"])
    op.create_index(
        "uq_departments_active_code",
        "departments",
        ["parent_id", "code"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )


def downgrade() -> None:
    op.drop_index("uq_departments_active_code", table_name="departments")
    op.drop_index("ix_departments_parent_id", table_name="departments")
    op.drop_index("ix_departments_path", table_name="departments")
    op.drop_table("departments")
