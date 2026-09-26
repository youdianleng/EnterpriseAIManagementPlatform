"""Employees, private details, positions and assignments.

Revision ID: 0003
Revises: 0002
Created: 2026-09-26

Three things worth noting:

* `employees` and `employee_private` are split along the visibility line. The
  directory reads the first table only, so withholding the address is a property
  of the query rather than something each call site must remember.
* `employee_private` has a CHECK constraint over its own column names, asserting
  that no ID-number, bank, health or biometric column exists. The ticket says
  these must not be in the system; a constraint makes that refusal structural.
* `departments.manager_employee_id` finally gets its foreign key here, because
  until this revision there was no employees table to point at.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FORBIDDEN_COLUMNS = [
    "national_id",
    "id_number",
    "ssn",
    "nif",
    "nie",
    "bank_account",
    "iban",
    "bank_card",
    "health",
    "health_data",
    "medical_notes",
    "diagnosis",
    "biometric",
    "fingerprint",
    "face_template",
    "photo_embedding",
]


def upgrade() -> None:
    op.create_table(
        "employees",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("first_name", sa.String(length=80), nullable=False),
        sa.Column("last_name", sa.String(length=120), nullable=False),
        sa.Column("preferred_name", sa.String(length=80), nullable=True),
        sa.Column("email", sa.String(length=200), nullable=False),
        sa.Column("photo_path", sa.String(length=400), nullable=True),
        sa.Column("city", sa.String(length=120), nullable=True),
        sa.Column("country", sa.String(length=80), nullable=True),
        sa.Column("hire_date", sa.Date(), nullable=False),
        sa.Column("termination_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'on_leave', 'terminated')", name="ck_employees_status"
        ),
        sa.CheckConstraint(
            "termination_date IS NULL OR termination_date >= hire_date",
            name="ck_employees_termination_after_hire",
        ),
        sa.CheckConstraint(
            "email ~ '^[^@[:space:]]+@[^@[:space:]]+$'", name="ck_employees_email"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    op.create_index("ix_employees_last_name", "employees", ["last_name", "first_name"])

    op.create_table(
        "employee_private",
        sa.Column("employee_id", sa.UUID(), nullable=False),
        sa.Column("address_line", sa.Text(), nullable=True),
        sa.Column("postal_code", sa.String(length=16), nullable=True),
        sa.Column("employee_no", sa.String(length=32), nullable=True),
        sa.Column("birth_date", sa.Date(), nullable=True),
        sa.Column("emergency_contact", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "address_line IS NULL OR length(btrim(address_line)) > 0",
            name="ck_employee_private_address_not_blank",
        ),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("employee_id"),
        sa.UniqueConstraint("employee_no"),
    )

    # Refuse to exist alongside a forbidden column.
    #
    # A CHECK constraint cannot do this: PostgreSQL rejects subqueries inside
    # CHECK expressions. A DDL event trigger can, and it fires on the change that
    # would actually introduce the problem — an ALTER TABLE ADD COLUMN — rather
    # than only when a row happens to be written.
    names = ", ".join(f"'{name}'" for name in FORBIDDEN_COLUMNS)
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION employee_private_reject_forbidden_columns()
        RETURNS event_trigger AS $$
        DECLARE
            offending text;
        BEGIN
            SELECT string_agg(f.name, ', ')
            INTO offending
            FROM unnest(ARRAY[{names}]) AS f(name)
            WHERE f.name = ANY (
                SELECT attname FROM pg_attribute
                WHERE attrelid = to_regclass('public.employee_private')
            );

            IF offending IS NOT NULL THEN
                RAISE EXCEPTION
                    'employee_private may not store these fields: %', offending;
            END IF;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE EVENT TRIGGER employee_private_forbidden_columns
        ON ddl_command_end
        WHEN TAG IN ('CREATE TABLE', 'ALTER TABLE')
        EXECUTE FUNCTION employee_private_reject_forbidden_columns();
        """
    )

    op.create_table(
        "job_positions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("title_es", sa.String(length=160), nullable=False),
        sa.Column("title_en", sa.String(length=160), nullable=False),
        sa.Column("department_id", sa.UUID(), nullable=False),
        sa.Column("is_managerial", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["department_id"], ["departments.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_job_positions_department_id", "job_positions", ["department_id"])
    op.create_index(
        "uq_job_positions_active_code",
        "job_positions",
        ["department_id", "code"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )

    op.create_table(
        "employee_assignments",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("employee_id", sa.UUID(), nullable=False),
        sa.Column("department_id", sa.UUID(), nullable=False),
        sa.Column("job_position_id", sa.UUID(), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.Column("is_part_time", sa.Boolean(), nullable=False),
        sa.Column("manager_employee_id", sa.UUID(), nullable=True),
        sa.Column("notification_override_employee_id", sa.UUID(), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "end_date IS NULL OR end_date >= start_date", name="ck_assignments_end_after_start"
        ),
        sa.ForeignKeyConstraint(["department_id"], ["departments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_position_id"], ["job_positions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    # One active primary position per employee; ended rows keep the flag so
    # history stays readable.
    op.create_index(
        "uq_assignments_active_primary",
        "employee_assignments",
        ["employee_id"],
        unique=True,
        postgresql_where=sa.text("is_primary AND end_date IS NULL"),
    )
    op.create_index(
        "ix_assignments_employee_active",
        "employee_assignments",
        ["employee_id"],
        postgresql_where=sa.text("end_date IS NULL"),
    )
    op.create_index("ix_assignments_department_id", "employee_assignments", ["department_id"])
    op.create_index("ix_assignments_manager", "employee_assignments", ["manager_employee_id"])

    # Deferred from revision 0002: there was no employees table then.
    op.create_foreign_key(
        "fk_departments_manager_employee",
        "departments",
        "employees",
        ["manager_employee_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.execute("DROP EVENT TRIGGER IF EXISTS employee_private_forbidden_columns")
    op.execute("DROP FUNCTION IF EXISTS employee_private_reject_forbidden_columns()")
    op.drop_constraint("fk_departments_manager_employee", "departments", type_="foreignkey")
    op.drop_index("ix_assignments_manager", table_name="employee_assignments")
    op.drop_index("ix_assignments_department_id", table_name="employee_assignments")
    op.drop_index("ix_assignments_employee_active", table_name="employee_assignments")
    op.drop_index("uq_assignments_active_primary", table_name="employee_assignments")
    op.drop_table("employee_assignments")
    op.drop_index("uq_job_positions_active_code", table_name="job_positions")
    op.drop_index("ix_job_positions_department_id", table_name="job_positions")
    op.drop_table("job_positions")
    op.drop_table("employee_private")
    op.drop_index("ix_employees_last_name", table_name="employees")
    op.drop_table("employees")
