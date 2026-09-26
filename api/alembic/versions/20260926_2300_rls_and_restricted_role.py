"""A restricted runtime role, row-level security, and an append-only audit log.

Revision ID: 0007
Revises: 0006
Created: 2026-09-26

Three guarantees, all of them enforced by PostgreSQL rather than by application
code, because the point is what happens when the application is wrong:

* **The audit trail cannot be rewritten by the application.** The runtime role
  holds INSERT and SELECT on `audit_log` and nothing else. An UPDATE or a DELETE
  is refused by the database, not by a code path somebody could forget.
* **Sensitive rows are filtered by the database too.** `employee_private` carries
  row-level policies keyed on the session context the application publishes.
  With no context set — a forgotten call, a new code path, a psql session — the
  answer is no rows, because `current_setting(..., true)` returns NULL and the
  policy reads NULL as "not allowed".
* **The role that runs migrations is not the role that serves requests.** The
  owner can rewrite anything, and Postgres exempts a table's owner from its own
  policies; those are exactly the privileges a request must not have.

The two connections are configured separately (`DATABASE_URL` for migrations,
`APP_DATABASE_URL` for requests). Both may point at the owner in a minimal
development setup — `Settings.enforces_database_security` reports which mode is
in force rather than pretending the difference does not exist.

**On the password.** The role is created with a password from `APP_DB_PASSWORD`,
defaulting to the same development value compose uses. Production passes it in;
storing a development default here is what lets `docker compose up` work without
a manual step, and the alternative — a role nobody can log in as — is worse.
"""

import os
from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "eam_app"
DEFAULT_PASSWORD = "eam_app_dev_password"


def upgrade() -> None:
    password = os.environ.get("APP_DB_PASSWORD", DEFAULT_PASSWORD)
    # The role is cluster-wide, so this is idempotent across databases on the same
    # server. The password is interpolated rather than bound because PostgreSQL
    # does not accept parameters in DDL; it comes from the environment, and the
    # value is quoted here so a quote in the password cannot break the statement.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                CREATE ROLE {APP_ROLE} LOGIN;
            END IF;
        END
        $$;
        """
    )
    op.execute(f"ALTER ROLE {APP_ROLE} WITH LOGIN PASSWORD '{password.replace(chr(39), chr(39) * 2)}'")

    op.execute(f"GRANT CONNECT ON DATABASE {_database_name()} TO {APP_ROLE}")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")

    # Everything the application legitimately does to ordinary tables.
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}"
    )
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")

    # Then the one table where that is too much. Revoking beats not granting:
    # the blanket grant above is what keeps future tables working, and this is
    # the single exception it has to be corrected for.
    op.execute(f"REVOKE UPDATE, DELETE ON audit_log FROM {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON audit_log TO {APP_ROLE}")

    # Tables added by later migrations are covered without another grant
    # statement, because the one that forgets is the bug this avoids.
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT USAGE, SELECT ON SEQUENCES TO {APP_ROLE}"
    )

    _create_setting_helpers()
    _create_employee_private_policies()
    _create_document_policy_helper()


def _database_name() -> str:
    """The database being migrated.

    Read from the connection rather than from settings, because the test database
    and the development database are migrated by the same code path and only the
    connection knows which one this is.
    """
    return op.get_bind().exec_driver_sql("SELECT current_database()").scalar_one()


def _create_setting_helpers() -> None:
    """Two accessors every policy goes through.

    A custom setting that has been written once and then left behind by a
    committed transaction does **not** read back as NULL: it reads back as the
    empty string. A policy written as `COALESCE(current_setting(name, true),
    'false')::boolean` therefore fails with "invalid input syntax for type
    boolean: \\"\\"" on the *second* request over a pooled connection — after the
    first request has published a context and committed. That is the kind of
    failure that looks like a flaky test and is actually a rule with a hole in it.

    So empty string is folded into NULL here, once, and every policy reads
    through these functions instead of spelling out the same trap.
    """
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_setting(name text) RETURNS text AS $$
            SELECT NULLIF(current_setting(name, true), '')
        $$ LANGUAGE sql STABLE
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_setting_array(name text) RETURNS text[] AS $$
            SELECT COALESCE(NULLIF(current_setting(name, true), ''), '{}')::text[]
        $$ LANGUAGE sql STABLE
        """
    )


def _create_employee_private_policies() -> None:
    """Row-level security on the withheld employee details.

    The rule is the one `domain/employee/visibility.py` states: your own details,
    or somebody whose job includes personnel. Colleagues and outsiders get
    nothing, and a missing context gets nothing — `app_setting` returns NULL,
    NULL is not true, and the row is filtered out.

    **Administrators are in the read clause for a reason worth stating.** The
    product rule is that an administrator may correct these fields and may not
    read them, and that rule lives in the kernel and in
    `domain/employee/visibility.py`, where it is tested. It cannot be the rule
    here: PostgreSQL applies the *select* policy to the rows an UPDATE reads, so
    a role that may not SELECT a row cannot UPDATE it either — proved with
    `EXPLAIN` rather than assumed, and the reason the first version of this
    policy made every administrative correction return "0 rows updated" and then
    fail on a duplicate insert. A row-level rule is coarser than a field-level
    one; this one says "personnel and the person themselves", and the projection
    decides which fields travel.
    """
    op.execute("ALTER TABLE employee_private ENABLE ROW LEVEL SECURITY")

    # HR is in this list as well as in the writes below because a write needs the
    # read that precedes it; leaving it out would make personnel corrections fail
    # the same way administrative ones did.
    personnel = "app_setting_array('app.current_roles') && ARRAY['admin', 'hr']"

    op.execute(
        f"""
        CREATE POLICY employee_private_read ON employee_private
        FOR SELECT
        USING (
            app_setting('app.is_privileged')::boolean
            OR employee_id = app_setting('app.current_employee_id')::uuid
            OR {personnel}
        )
        """
    )

    # Writes are the personnel roles' job, declared one verb at a time rather than
    # as `FOR ALL`, so that each one is visible in `pg_policies` as the decision
    # it is.
    op.execute(
        f"""
        CREATE POLICY employee_private_insert ON employee_private
        FOR INSERT WITH CHECK ({personnel})
        """
    )
    op.execute(
        f"""
        CREATE POLICY employee_private_update ON employee_private
        FOR UPDATE USING ({personnel}) WITH CHECK ({personnel})
        """
    )
    op.execute(
        f"""
        CREATE POLICY employee_private_delete ON employee_private
        FOR DELETE USING ({personnel})
        """
    )


def _create_document_policy_helper() -> None:
    """A helper the document tables will use, written before they exist.

    Ticket 31 adds `documents` and `document_chunks`. A policy can only be
    attached to a table that exists, so the *shape* of the rule is captured here
    as a function the migration for those tables calls, rather than being written
    out a second time by somebody reading §4.2 from memory.
    """
    op.execute(
        """
        CREATE OR REPLACE FUNCTION document_visibility_predicate(
            owner_employee_id uuid,
            department_id uuid,
            clearance_level text
        ) RETURNS boolean AS $$
        BEGIN
            RETURN
                owner_employee_id = app_setting('app.current_employee_id')::uuid
                OR (
                    clearance_level = ANY (app_setting_array('app.clearance_levels'))
                    AND department_id = ANY (
                        app_setting_array('app.department_ids')::uuid[]
                    )
                );
        END;
        $$ LANGUAGE plpgsql STABLE
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS document_visibility_predicate(uuid, uuid, text)")
    for policy in (
        "employee_private_insert",
        "employee_private_update",
        "employee_private_delete",
        "employee_private_read",
    ):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON employee_private")
    op.execute("ALTER TABLE employee_private DISABLE ROW LEVEL SECURITY")
    op.execute("DROP FUNCTION IF EXISTS app_setting_array(text)")
    op.execute("DROP FUNCTION IF EXISTS app_setting(text)")
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM " + APP_ROLE
    )
    op.execute(
        "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM " + APP_ROLE
    )
    op.execute(f"REVOKE ALL ON SCHEMA public FROM {APP_ROLE}")
    # The role itself is left in place: other databases on this server may be
    # using it, and dropping a role is not something a schema migration should do
    # behind the operator's back.
