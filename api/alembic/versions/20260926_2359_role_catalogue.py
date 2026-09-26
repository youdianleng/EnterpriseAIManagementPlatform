"""The role catalogue, published as tables.

Revision ID: 0008
Revises: 0007
Created: 2026-09-26

Ticket 08b asks for the role-to-permission mapping to be expressed as a queryable
table rather than scattered through code branches. The branches were never the
problem — `domain/access/permissions.py` has held one readable table since ticket
11 — but the requirement's *purpose* is real: an auditor, the interface that
explains what a role may do, and whoever joins the project next should be able to
ask the system without reading Python.

So these tables are a **projection of the code**, rewritten from the catalogue at
application start (`domain/access/catalogue.py`). The application keeps deciding
from `RULES`: a permission decided by querying the database would be slower on
every request, and a permission change that is a row edit alone is a change nobody
reviewed. `tests/test_roles_api.py` asserts the two agree row for row, so drift is
a failing test rather than a quiet disagreement.

The runtime role gets SELECT and nothing else, so the published form is genuinely
read-only to the application; publishing runs on the owner connection, like
migrations and the seed.

`user_roles` is deliberately absent: the roles an account holds live in
`users.roles` (migration 0005), and a join table beside it would be a second answer
to the same question.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "roles",
        sa.Column("name", sa.String(32), primary_key=True),
        sa.Column("description", sa.Text(), nullable=False),
        #: True for a role a managerial position can confer without anybody
        #: granting it by hand.
        sa.Column(
            "is_derived", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.create_table(
        "role_permissions",
        sa.Column(
            "role",
            sa.String(32),
            sa.ForeignKey("roles.name", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("action", sa.String(64), primary_key=True),
    )
    op.create_index("ix_role_permissions_action", "role_permissions", ["action"])

    # Read-only to the application. The blanket grant in migration 0007 gave every
    # table in the schema INSERT/UPDATE/DELETE, which is right for operational
    # tables and wrong for a published description of the permission system.
    op.execute("REVOKE INSERT, UPDATE, DELETE ON roles FROM eam_app")
    op.execute("REVOKE INSERT, UPDATE, DELETE ON role_permissions FROM eam_app")
    op.execute("GRANT SELECT ON roles, role_permissions TO eam_app")


def downgrade() -> None:
    op.drop_index("ix_role_permissions_action", table_name="role_permissions")
    op.drop_table("role_permissions")
    op.drop_table("roles")
