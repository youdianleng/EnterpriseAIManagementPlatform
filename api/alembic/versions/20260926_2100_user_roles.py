"""Roles on the account.

Revision ID: 0005
Revises: 0004
Created: 2026-09-26

Ticket 08's second half (roles and the permission mapping) was deferred to 08b
because it needs the users table. Ticket 11 needs it *now*: the authorization
kernel's matrix cannot be exercised over real requests if every account is only
an employee, and a matrix that cannot be tested is a matrix that is not true.

So the storage lands here, with the kernel that consumes it. 08b keeps the
administrative surface — granting and revoking — which is a UI and audit concern
on top of this column.

A JSONB array rather than a join table: the role set is fixed by ticket 08, small,
and read on every request as part of the permission snapshot. A join would add a
query to the hot path for no benefit at this size. If roles ever gain attributes
or per-role scope, this becomes a table and the kernel changes in one place.

**On validating the values.** The obvious form — a CHECK that every element is a
known role — is impossible: PostgreSQL refuses a subquery inside a CHECK
constraint, scalar or not. Three attempts were rejected before settling here:

  * `roles <@ ARRAY[...]::jsonb`        -> a text[] cannot be cast to jsonb
  * rewriting `roles::text` with replace()/string_to_array() -> works, but
    brittle around escaping and unreadable as a rule
  * `COALESCE((SELECT bool_and(...) FROM jsonb_array_elements_text(roles)), false)`
    -> the form the rule wants, refused by PostgreSQL

The last one works as a row trigger, which is where it now lives. A trigger is
also the only form that can give a message naming the offending role.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SYSTEM_ROLES = ("admin", "hr", "finance", "it", "compliance", "manager", "employee")


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "roles",
            postgresql.JSONB(),
            server_default=sa.text("'[\"employee\"]'::jsonb"),
            nullable=False,
        ),
    )

    # A CHECK can still cover the shape: an empty array is not "no roles", it is
    # an account that can do nothing, and it must never be stored by accident.
    op.execute(
        """
        ALTER TABLE users
        ADD CONSTRAINT ck_users_roles_not_empty
        CHECK (jsonb_array_length(roles) > 0)
        """
    )

    # Value validation needs to look at every element, which a CHECK cannot do.
    # A BEFORE trigger also names the offending role, so a failed insert explains
    # itself instead of reporting a constraint name.
    roles_literal = ", ".join(f"'{role}'" for role in SYSTEM_ROLES)
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION users_validate_roles()
        RETURNS trigger AS $$
        DECLARE
            offending text;
        BEGIN
            SELECT string_agg(held.role, ', ')
            INTO offending
            FROM jsonb_array_elements_text(NEW.roles) AS held(role)
            WHERE held.role <> ALL (ARRAY[{roles_literal}]::text[]);

            IF offending IS NOT NULL THEN
                RAISE EXCEPTION 'unknown role(s): %', offending;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER users_roles_are_known
        BEFORE INSERT OR UPDATE OF roles ON users
        FOR EACH ROW EXECUTE FUNCTION users_validate_roles();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS users_roles_are_known ON users")
    op.execute("DROP FUNCTION IF EXISTS users_validate_roles()")
    # IF EXISTS so a constraint already removed by hand cannot make the downgrade
    # fail half-way, leaving the migration table and the schema disagreeing.
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS ck_users_roles_not_empty")
    op.drop_column("users", "roles")
