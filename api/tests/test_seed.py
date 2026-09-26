"""The seed dataset, driven against the test database.

A seed script is data, and data has invariants: every person holds a position,
every department has somebody responsible for it, the tree really is four levels
deep, and running the thing twice does not produce two of everything. Those are
the claims this file makes, on the same code path `python -m app.seed` runs.
"""

from tests.support.platform import Platform


async def seed(platform: Platform):
    """One full pass of the loader, exactly as the command runs it."""
    from app.seed import Seeder

    async with platform.factory() as session:
        seeder = Seeder(session)
        await seeder.index_positions()
        await seeder.load_departments()
        await seeder.load_employees()
        await seeder.load_managers()
        await seeder.load_logins()
    return seeder.created


async def test_the_seed_loads_a_dataset_the_rules_can_be_tested_against(
    platform: Platform,
) -> None:
    created = await seed(platform)

    assert created.employees >= 100, created.summary()
    assert created.departments >= 6, created.summary()
    assert created.logins >= 1, created.summary()

    headcount = await platform.scalar("SELECT count(*) FROM employees")
    assert headcount == created.employees


async def test_every_seeded_person_holds_a_position(platform: Platform) -> None:
    await seed(platform)

    unassigned = await platform.scalar(
        """
        SELECT count(*) FROM employees e
        WHERE NOT EXISTS (
            SELECT 1 FROM employee_assignments a
            WHERE a.employee_id = e.id AND a.end_date IS NULL
        )
        """
    )

    assert unassigned == 0


async def test_every_department_has_a_manager(platform: Platform) -> None:
    """Every level, not only the top: an approval route that stops halfway down
    is a route that cannot approve a transfer."""
    await seed(platform)

    orphans = await platform.sql(
        "SELECT code FROM departments WHERE manager_employee_id IS NULL ORDER BY code"
    )

    assert orphans == []


async def test_the_tree_is_four_levels_deep(platform: Platform) -> None:
    await seed(platform)

    assert await platform.scalar("SELECT max(depth) FROM departments") == 3


async def test_the_dataset_contains_the_awkward_cases(platform: Platform) -> None:
    """One multi-position person and one part-timer at least.

    These are the cases the permission union and the attendance rules exist for,
    and a dataset without them makes those rules look simpler than they are.
    """
    await seed(platform)

    multi = await platform.scalar(
        """
        SELECT count(*) FROM employees e
        WHERE (SELECT count(*) FROM employee_assignments a
               WHERE a.employee_id = e.id AND a.end_date IS NULL) > 1
        """
    )
    part_time = await platform.scalar(
        "SELECT count(*) FROM employee_assignments WHERE is_part_time"
    )

    assert multi > 0
    assert part_time > 0


async def test_running_the_seed_twice_adds_nothing(platform: Platform) -> None:
    """Idempotent by natural key, which is what makes it safe on a live database.

    Asserted on what the *second* pass created rather than only on the totals: a
    loader that deleted and recreated everything would pass a totals check and
    still be destructive.
    """
    await seed(platform)
    before = await counts(platform)

    created = await seed(platform)

    assert created.departments == 0
    assert created.positions == 0
    assert created.employees == 0
    assert created.assignments == 0
    assert created.logins == 0
    assert await counts(platform) == before


async def test_the_seeded_people_only_carry_permitted_fields(platform: Platform) -> None:
    """Q9's list, checked as a claim about the data rather than about the code."""
    await seed(platform)

    columns = await platform.sql(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_name IN ('employees', 'employee_private', 'users')
        """
    )
    present = {row[0] for row in columns}
    forbidden = {
        "national_id",
        "id_number",
        "iban",
        "bank_account",
        "social_security_number",
        "health_data",
        "biometric_data",
        "salary",
    }

    assert present & forbidden == set()


async def counts(platform: Platform) -> tuple:
    counted = []
    for table in ("departments", "job_positions", "employees", "employee_assignments"):
        counted.append(await platform.scalar(f"SELECT count(*) FROM {table}"))
    return tuple(counted)
