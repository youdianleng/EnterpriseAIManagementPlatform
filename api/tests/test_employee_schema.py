"""Schema guards for the employee tables.

The ticket states that the system stores no ID numbers, bank details, health data
or biometrics. Two independent guards enforce it: a DDL event trigger installed
by migration 0003, and these assertions against the live schema. The test exists
because it fails during a test run rather than mid-deploy, and because a guard
nobody has seen fire is a guard nobody knows works.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

#: Column names that must never appear on an employee table.
FORBIDDEN_PATTERNS = [
    "national_id",
    "id_number",
    "id_card",
    "ssn",
    "nif",
    "nie",
    "passport",
    "bank",
    "iban",
    "account_number",
    "card_number",
    "salary_account",
    "health",
    "medical",
    "diagnosis",
    "sick_note",
    "disability",
    "biometric",
    "fingerprint",
    "face_template",
    "iris",
    "photo_embedding",
]

EMPLOYEE_TABLES = ["employees", "employee_private", "employee_assignments"]


async def _columns(connection: AsyncConnection, table: str) -> list[str]:
    rows = (
        await connection.execute(
            text(
                "SELECT attname FROM pg_attribute "
                "WHERE attrelid = to_regclass(:table) AND attnum > 0 ORDER BY attnum"
            ),
            {"table": table},
        )
    ).scalars()
    return list(rows)


@pytest.mark.parametrize("table", EMPLOYEE_TABLES)
async def test_no_forbidden_column_exists(connection: AsyncConnection, table: str) -> None:
    columns = await _columns(connection, table)
    assert columns, f"{table} has no columns; was the migration applied?"

    offending = [
        column
        for column in columns
        if any(pattern in column for pattern in FORBIDDEN_PATTERNS)
    ]
    assert offending == [], f"{table} contains fields the system must not store: {offending}"


async def test_the_private_table_stores_exactly_the_permitted_fields(
    connection: AsyncConnection,
) -> None:
    """Names and addresses are enough; everything else about a person that this
    system might be tempted to keep is deliberately absent."""
    columns = set(await _columns(connection, "employee_private"))

    permitted = {
        "employee_id",
        "address_line",
        "postal_code",
        "employee_no",
        "birth_date",
        "emergency_contact",
        "created_at",
        "updated_at",
    }
    assert columns == permitted, f"unexpected columns: {columns ^ permitted}"


async def test_the_directory_columns_are_the_ones_the_visibility_rule_expects(
    connection: AsyncConnection,
) -> None:
    """`employees` is the table the directory reads.

    If a new column lands here, the visibility rule needs revisiting — this test
    is the reminder, because a column added "just for convenience" would travel
    to every colleague by default.
    """
    columns = set(await _columns(connection, "employees"))

    allowed = {
        "id",
        "first_name",
        "last_name",
        "preferred_name",
        "email",
        "photo_path",
        "city",
        "country",
        "hire_date",
        "termination_date",
        "status",
        "created_at",
        "updated_at",
    }
    assert columns == allowed, f"unexpected columns: {columns ^ allowed}"


async def test_the_forbidden_column_trigger_is_installed(connection: AsyncConnection) -> None:
    count = await connection.scalar(
        text(
            "SELECT count(*) FROM pg_event_trigger "
            "WHERE evtname = 'employee_private_forbidden_columns'"
        )
    )
    assert count == 1, "migration 0003's DDL guard is not installed"


async def test_adding_a_forbidden_column_is_actually_refused(
    connection: AsyncConnection,
) -> None:
    """Drive the guard rather than trusting that it is wired up correctly.

    Wrapped in a savepoint: the failed DDL aborts the surrounding transaction, and
    without one the assertions below would fail on an aborted transaction instead
    of on the thing being tested.
    """
    with pytest.raises(Exception) as excinfo:
        async with connection.begin_nested():
            await connection.execute(
                text("ALTER TABLE employee_private ADD COLUMN iban varchar(34)")
            )

    assert "employee_private may not store" in str(excinfo.value)
    # The failed DDL must leave the schema untouched.
    assert "iban" not in await _columns(connection, "employee_private")


async def test_department_manager_foreign_key_exists(connection: AsyncConnection) -> None:
    """Deferred to revision 0003 because revision 0002 had no employees table."""
    count = await connection.scalar(
        text(
            """
            SELECT count(*) FROM pg_constraint
            WHERE conname = 'fk_departments_manager_employee'
            """
        )
    )
    assert count == 1
