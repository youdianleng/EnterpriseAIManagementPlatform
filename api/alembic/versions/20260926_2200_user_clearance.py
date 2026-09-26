"""A clearance level on the account.

Revision ID: 0006
Revises: 0005
Created: 2026-09-26

`docs/DESIGN.md` §10.5 puts the user's clearance on `users` and initialises it from
the primary position's department when the person is hired, so that HR configures
clearance once per department instead of once per person. The kernel then takes the
higher of that stored value and what the departments grant (D12).

Why both a stored value and a derived one: the department is the authority on what
its work may contain, and an individual can be raised above it for a specific
reason. Taking the higher of the two means a raise is possible without touching the
department, and a department-wide raise reaches everyone in it without a data
migration.

The stored value is *not* an override downward. Lowering it below what the
departments grant has no effect, because the departments are re-read on every
snapshot; that is the rule the design states, and it is recorded in ticket 12
rather than left to be discovered.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CLEARANCE_LEVELS = ("low", "medium", "high")


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "clearance_level",
            sa.String(16),
            server_default=sa.text("'low'"),
            nullable=False,
        ),
    )
    levels = ", ".join(f"'{level}'" for level in CLEARANCE_LEVELS)
    op.execute(
        f"""
        ALTER TABLE users
        ADD CONSTRAINT ck_users_clearance_level
        CHECK (clearance_level IN ({levels}))
        """
    )

    # Existing accounts inherit what their departments already grant, so the
    # column starts out agreeing with the derived value rather than silently
    # reading "low" for someone whose department says otherwise. The subtree is
    # included, matching how the snapshot derives clearance at runtime: a person
    # in a low-clearance department that contains a high-clearance team is
    # cleared for that team's material.
    op.execute(
        """
        UPDATE users u
        SET clearance_level = granted.level
        FROM (
            SELECT mine.employee_id,
                   CASE MIN(CASE d.clearance_level
                                WHEN 'high' THEN 0
                                WHEN 'medium' THEN 1
                                ELSE 2
                            END)
                       WHEN 0 THEN 'high'
                       WHEN 1 THEN 'medium'
                       ELSE 'low'
                   END AS level
            FROM (
                SELECT a.employee_id, d.path
                FROM employee_assignments a
                JOIN departments d ON d.id = a.department_id
                WHERE a.end_date IS NULL
            ) AS mine
            JOIN departments d ON d.path <@ mine.path
            GROUP BY mine.employee_id
        ) AS granted
        WHERE granted.employee_id = u.employee_id
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS ck_users_clearance_level")
    op.drop_column("users", "clearance_level")
