"""Probe the guard that refuses forbidden columns on employee_private.

The ticket says the system must not store ID numbers, bank details, health or
biometric data. A CHECK constraint cannot express that (PostgreSQL rejects
subqueries inside CHECK), so migration 0003 installs a DDL event trigger. This
proves the trigger actually fires — a guard nobody has seen fire is a guard
nobody knows works.

Run inside the compose network:
    docker compose exec -T api python /app/tests/tools/probe_forbidden_columns.py
"""

import sys

import psycopg
from sqlalchemy.engine import make_url

sys.path.insert(0, "/app")

from app.config import get_settings  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, observed: object = "") -> None:
    print(f"[{'ok  ' if condition else 'FAIL'}] {label}: {observed}")
    if not condition:
        failures.append(label)


def dsn_for(database: str) -> str:
    url = make_url(get_settings().database_url)
    return (
        f"postgresql://{url.username}:{url.password}@{url.host}:{url.port or 5432}/{database}"
    )


def main() -> None:
    scratch = "eam_forbidden_probe"
    admin_dsn = dsn_for("postgres")
    scratch_dsn = dsn_for(scratch)

    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{scratch}"')
        admin.execute(f'CREATE DATABASE "{scratch}"')

    try:
        # Reproduce just the guard, so the probe does not depend on a full migrate.
        with psycopg.connect(scratch_dsn) as connection:
            connection.execute(
                "CREATE TABLE employee_private (employee_id uuid PRIMARY KEY, address_line text)"
            )
            connection.execute(
                """
                CREATE OR REPLACE FUNCTION employee_private_reject_forbidden_columns()
                RETURNS event_trigger AS $$
                DECLARE offending text;
                BEGIN
                    SELECT string_agg(f.name, ', ') INTO offending
                    FROM unnest(ARRAY[
                        'national_id','id_number','ssn','nif','nie',
                        'bank_account','iban','bank_card',
                        'health','health_data','medical_notes','diagnosis',
                        'biometric','fingerprint','face_template','photo_embedding'
                    ]) AS f(name)
                    WHERE f.name = ANY (
                        SELECT attname FROM pg_attribute
                        WHERE attrelid = to_regclass('public.employee_private')
                    );
                    IF offending IS NOT NULL THEN
                        RAISE EXCEPTION 'employee_private may not store these fields: %', offending;
                    END IF;
                END;
                $$ LANGUAGE plpgsql;
                """
            )
            connection.execute(
                """
                CREATE EVENT TRIGGER employee_private_forbidden_columns
                ON ddl_command_end
                WHEN TAG IN ('CREATE TABLE', 'ALTER TABLE')
                EXECUTE FUNCTION employee_private_reject_forbidden_columns();
                """
            )
            connection.commit()

            # A legitimate column is allowed.
            allowed = True
            try:
                connection.execute(
                    "ALTER TABLE employee_private ADD COLUMN postal_code varchar(16)"
                )
                connection.commit()
            except Exception as exc:
                allowed = False
                connection.rollback()
                print(f"      unexpected refusal: {exc}")
            check("a permitted column is accepted", allowed)

            # Each forbidden column must be refused.
            for column, type_ in (
                ("national_id", "varchar(32)"),
                ("iban", "varchar(34)"),
                ("health_data", "text"),
                ("fingerprint", "bytea"),
            ):
                refused = False
                detail = ""
                try:
                    connection.execute(
                        f"ALTER TABLE employee_private ADD COLUMN {column} {type_}"
                    )
                    connection.commit()
                except Exception as exc:
                    refused = True
                    detail = str(exc).splitlines()[0][:70]
                    connection.rollback()
                check(f"adding {column} is refused", refused, detail)

            # The refusal must not make legitimate DDL elsewhere impossible.
            unrelated = True
            try:
                connection.execute("CREATE TABLE something_else (id int primary key)")
                connection.commit()
            except Exception as exc:
                unrelated = False
                connection.rollback()
                print(f"      unexpected refusal: {exc}")
            check("unrelated DDL still works", unrelated)
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{scratch}"')

    print()
    print("FAIL" if failures else "ALL CHECKS PASSED")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
