"""What PostgreSQL refuses, independent of what the application remembers.

Every other test in this suite asks whether the application allows something.
These ask what happens when the application is *wrong*: a query with no filter, a
code path that never published its context, a bug that tries to rewrite the
audit trail. The answers come from the database, over a connection made with the
same restricted role requests use.

The owner connection is deliberately not what these run on. A table's owner is
exempt from its own row-level policies, so a test suite connected as the owner
would exercise none of this and look green while doing it.
"""

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.support.platform import Platform

#: The role requests connect as. Named here rather than imported from the
#: migration so that a rename shows up as a failing test rather than as two
#: places agreeing on the new name.
APP_ROLE = "eam_app"


@pytest.fixture
async def app_connection(settings) -> AsyncIterator[async_sessionmaker]:
    """Sessions bound to the restricted role, on the test database."""
    engine = create_async_engine(settings.runtime_test_database_url)
    try:
        yield async_sessionmaker(bind=engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def publish(session, **settings_values: str) -> None:
    """The context the application publishes, by hand.

    Written out here rather than called from the kernel: if this test used the
    kernel's own function, it would prove the two agree about a name and nothing
    about what the database does with it.
    """
    for name, value in settings_values.items():
        await session.execute(
            text("SELECT set_config(:name, :value, true)"),
            {"name": name, "value": value},
        )


async def seed_private(platform: Platform) -> tuple[str, str]:
    """Two employees with withheld details, written as the owner.

    The owner can write them without a personnel context; that asymmetry is the
    point — the fixtures set state up, and the restricted role is what has to
    read it back under the rule.
    """
    first = await platform.employee()
    second = await platform.employee()
    for employee_id in (first, second):
        await platform.sql(
            """
            INSERT INTO employee_private (employee_id, address_line, postal_code)
            VALUES (:id, 'Calle Mayor 1', '28001')
            ON CONFLICT (employee_id) DO UPDATE SET address_line = 'Calle Mayor 1'
            """,
            {"id": employee_id},
        )
    return first, second


async def test_the_application_connects_as_a_role_that_is_not_the_owner(
    settings,
) -> None:
    """The separation itself, asserted rather than assumed.

    If both connections pointed at the same role, every claim below would still
    pass on a developer's machine and mean nothing.
    """
    assert settings.enforces_database_security
    assert settings.runtime_test_database_url != settings.test_database_url

    engine = create_async_engine(settings.runtime_test_database_url)
    try:
        async with engine.connect() as connection:
            current = await connection.scalar(text("SELECT current_user"))
            owner = await connection.scalar(
                text("SELECT tableowner FROM pg_tables WHERE tablename = 'employee_private'")
            )
    finally:
        await engine.dispose()

    assert current == APP_ROLE
    assert owner != APP_ROLE


async def test_without_context_the_withheld_details_are_invisible(
    platform: Platform, app_connection
) -> None:
    """A forgotten context reads as "no rows", never as "every row".

    This is the whole design: the failure mode of a missing filter is silence,
    not disclosure.
    """
    first, _second = await seed_private(platform)

    async with app_connection() as session:
        rows = (
            await session.execute(
                text("SELECT employee_id FROM employee_private WHERE employee_id = :id"),
                {"id": first},
            )
        ).all()
        everything = (await session.execute(text("SELECT count(*) FROM employee_private"))).scalar()

    assert rows == []
    assert everything == 0


async def test_a_stranger_sees_nothing(platform: Platform, app_connection) -> None:
    """A context that names a third party still returns none of the two rows."""
    await seed_private(platform)

    async with app_connection() as session:
        await publish(session, **{"app.current_employee_id": str(uuid4())})
        visible = (await session.execute(text("SELECT count(*) FROM employee_private"))).scalar()

    assert visible == 0


async def test_the_person_sees_their_own_details(platform: Platform, app_connection) -> None:
    first, _second = await seed_private(platform)

    async with app_connection() as session:
        await publish(session, **{"app.current_employee_id": str(first)})
        visible = (
            await session.execute(text("SELECT employee_id FROM employee_private"))
        ).scalars().all()

    assert [str(value) for value in visible] == [first]


async def test_the_personnel_roles_see_everyone(platform: Platform, app_connection) -> None:
    """HR and compliance read withheld details; the policy admits both."""
    await seed_private(platform)

    async with app_connection() as session:
        await publish(session, **{"app.is_privileged": "true"})
        visible = (await session.execute(text("SELECT count(*) FROM employee_private"))).scalar()

    assert visible == 2


async def test_the_database_rule_is_coarser_than_the_product_rule(
    platform: Platform, app_connection
) -> None:
    """An administrator reads the row here; the product still withholds the fields.

    This is the honest boundary of row-level security, and it is asserted rather
    than glossed over. PostgreSQL applies the select policy to the rows an UPDATE
    reads, so a role that cannot SELECT a row cannot UPDATE it: keeping
    administrators out of the read clause made every administrative correction
    report "0 rows updated" and then collide on the insert. The field-level rule
    — an administrator may correct these fields and may not receive them — is a
    projection rule, and it stays in `domain/employee/visibility.py`, where
    `test_employees_api.py` checks it.
    """
    await seed_private(platform)

    async with app_connection() as session:
        await publish(
            session,
            **{
                "app.current_employee_id": str(uuid4()),
                "app.current_roles": "{admin,employee}",
            },
        )
        visible = (await session.execute(text("SELECT count(*) FROM employee_private"))).scalar()

    assert visible == 2


@pytest.mark.parametrize(
    "statement",
    ["UPDATE audit_log SET action = 'tampered'", "DELETE FROM audit_log"],
)
async def test_the_audit_trail_cannot_be_rewritten(
    platform: Platform, app_connection, statement: str
) -> None:
    """Refused by the database, not by a code path somebody has to remember.

    Driven over the restricted connection because that is the role requests use;
    the owner can still rewrite the table, which is exactly why the two
    connections are configured separately.
    """
    await platform.admin()

    async with app_connection() as session:
        with pytest.raises(Exception) as excinfo:
            await session.execute(text(statement))

    assert "permission denied" in str(excinfo.value).lower()


async def test_the_audit_trail_can_still_be_appended_to_and_read(
    platform: Platform, app_connection
) -> None:
    """Append-only, not read-only: a system that cannot record is not audited."""
    await platform.admin()

    async with app_connection() as session:
        await session.execute(
            text(
                """
                INSERT INTO audit_log (action, entity_type, actor_roles, initiated_by)
                VALUES ('probe.written_by_the_app_role', 'test', '[]'::jsonb, 'system')
                """
            )
        )
        await session.commit()
        written = (
            await session.execute(
                text(
                    "SELECT count(*) FROM audit_log "
                    "WHERE action = 'probe.written_by_the_app_role'"
                )
            )
        ).scalar()

    assert written == 1


async def test_the_published_context_is_scoped_to_the_transaction(
    platform: Platform, app_connection
) -> None:
    """`set_config(..., is_local => true)` must not leak to the next request.

    A pooled connection is reused, so a setting that outlived its transaction
    would hand the next caller somebody else's permissions — the failure this
    whole mechanism would otherwise introduce.
    """
    first, _second = await seed_private(platform)

    async with app_connection() as session:
        await publish(session, **{"app.current_employee_id": str(first)})
        inside = (await session.execute(text("SELECT count(*) FROM employee_private"))).scalar()
        await session.commit()

        after_commit = (
            await session.execute(text("SELECT count(*) FROM employee_private"))
        ).scalar()

    assert inside == 1
    assert after_commit == 0, "the context survived the transaction it belonged to"


async def test_the_context_published_by_the_kernel_is_what_the_policy_reads(
    platform: Platform, app_connection
) -> None:
    """The two halves of the mechanism, joined.

    The kernel's `apply_rls_context` builds the settings; the policy reads them.
    They are written in different languages and different files, so the only
    thing that keeps them in step is a test that runs one into the other.
    """
    from app.domain.access.kernel import apply_rls_context
    from app.domain.access.principal import Principal

    first, _second = await seed_private(platform)
    principal = Principal(
        user_id=uuid4(),
        employee_id=UUID(first),
        username="ana",
        roles=frozenset({"employee"}),
        clearance_level="low",
        department_ids=frozenset(),
        primary_department_id=None,
        is_manager=False,
        reports_employee_ids=frozenset(),
    )

    async with app_connection() as session:
        await apply_rls_context(session, principal)
        visible = (
            await session.execute(text("SELECT employee_id FROM employee_private"))
        ).scalars().all()
        # And the department list arrives as an array the database can compare,
        # not as a string it would silently never match.
        departments = (
            await session.execute(
                text("SELECT current_setting('app.department_ids', true)::uuid[]")
            )
        ).scalar()

    assert [str(value) for value in visible] == [first]
    assert departments == []
