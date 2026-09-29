"""Shared test fixtures.

Integration tests run against a real PostgreSQL server and a real Redis, because
the behaviour under test — row-level security, vector search, permission
filtering — does not exist in a substitute. The design doc records this as a
hard constraint rather than a preference.

Isolation strategy: one connection per test with an outer transaction that is
rolled back afterwards, so tests never see each other's rows and nothing needs
truncating.
"""

import os
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

if TYPE_CHECKING:
    from tests.support.platform import Actor, Platform

API_ROOT = Path(__file__).resolve().parents[1]

# The application reads settings at import time, so the test database must be
# selected before anything imports app.config.
#
# Assigned, not `setdefault`: the container exports DATABASE_URL pointing at the
# development database, so a default never applies and the application's own
# engine would read and write `eam` during a test run. That is not hypothetical —
# it left 181 audit rows in the development database before being caught.
#
# The name comes from `TEST_DATABASE_NAME`, defaulting to `eam_test`. Both
# connections below have to name the same database, and the suite wipes it between
# tests, so a run that sets this variable owns a database of its own instead of
# fighting one beside it for the same rows.
TEST_DATABASE_NAME = os.environ.get("TEST_DATABASE_NAME", "eam_test")
os.environ["DATABASE_URL"] = (
    f"postgresql+psycopg://eam:eam_dev_password@postgres:5432/{TEST_DATABASE_NAME}"
)
# The application must serve requests from the *restricted* role, in tests as
# well as in production: the row-level policies ticket 13 installs are skipped
# for a table's owner, so a suite that connected as the owner would exercise
# none of them and still look green.
os.environ["APP_DATABASE_URL"] = (
    f"postgresql+psycopg://eam_app:eam_app_dev_password@postgres:5432/{TEST_DATABASE_NAME}"
)
# The payslip files (ticket 44) go somewhere of this run's own.
#
# Derived from the database name rather than left at the container's `/data/payslips`, so
# two scratch runs — and a run beside the development stack — never share a storage root.
# It matters more here than for documents because the *file* is the artifact: two runs
# sharing a root would let one run's payslip satisfy another's "the bytes are on disk"
# check, and the suite would pass while proving nothing about the code that wrote them.
os.environ["PAYSLIP_STORAGE_PATH"] = f"/tmp/eam-payslips/{TEST_DATABASE_NAME}"
os.environ.setdefault("APP_ENV", "test")

from app.config import Settings, get_settings, to_libpq_dsn  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_loop_bound_singletons() -> Iterator[None]:
    """Drop cached engine/Redis around every test.

    `get_engine()` and `get_redis()` are lru_cached, so the first test to use one
    binds it to that test's event loop; the next test runs in a different loop and
    fails with "attached to a different loop". Clearing the caches keeps the
    per-test isolation the suite relies on, without weakening the application's
    one-engine-per-process rule.

    `flushdb()` is part of the same problem: a Redis connection pool is also
    loop-bound, so a client cached before a loop change is unusable even though
    the server is healthy.
    """
    import app.cache as cache
    import app.db as db

    db.get_engine.cache_clear()
    db.get_session_factory.cache_clear()
    cache.get_redis.cache_clear()
    yield
    cache.get_redis.cache_clear()
    db.get_session_factory.cache_clear()
    db.get_engine.cache_clear()


@pytest.fixture(scope="session")
def settings() -> Settings:
    return get_settings()


@pytest.fixture(scope="session")
def test_database_url(settings: Settings) -> str:
    return settings.sync_test_database_url


@pytest.fixture(scope="session", autouse=True)
def migrated_database(settings: Settings) -> Iterator[None]:
    """Create the test database if missing, then migrate it to head.

    `upgrade head` runs on every session, not only when the database is created.
    An earlier version migrated inside the `if not exists` branch, so an existing
    test database stayed on whatever schema it had the day it was made — new
    columns and extensions were simply absent, and the failures that produced
    ("column roles does not exist", "ltree syntax error") pointed at the code
    rather than at the fixture.
    """
    with psycopg.connect(settings.sync_admin_database_url, autocommit=True) as admin:
        exists = admin.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (settings.test_database_name,)
        ).fetchone()
        if not exists:
            # Identifier cannot be parameterised; the name comes from settings,
            # not from user input.
            admin.execute(f'CREATE DATABASE "{settings.test_database_name}"')

    config = Config(str(API_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(API_ROOT / "alembic"))
    os.environ["ALEMBIC_DATABASE_URL"] = settings.sync_test_database_url
    command.upgrade(config, "head")

    yield


@pytest.fixture
async def connection(settings: Settings, migrated_database: None) -> AsyncIterator[AsyncConnection]:
    """A connection wrapped in a transaction that is rolled back after the test."""
    # NullPool: the connection must not outlive the test that opened it.
    engine = create_async_engine(settings.test_database_url, poolclass=NullPool)
    async with engine.connect() as conn:
        transaction = await conn.begin()
        try:
            yield conn
        finally:
            await transaction.rollback()
    await engine.dispose()


@pytest.fixture
async def session(connection: AsyncConnection) -> AsyncIterator[AsyncSession]:
    """ORM session bound to the rolled-back connection."""
    async with AsyncSession(bind=connection, expire_on_commit=False) as db_session:
        yield db_session


#: Cleared before and after any test that asks for `redis_client`. Sessions,
#: session epochs and permission snapshots are deliberately absent: a test can
#: hold a live session cookie while this fixture runs, and wiping it would turn
#: an unrelated assertion into a confusing 401. What actually leaks between
#: tests is the login throttle, which is keyed by username.
VOLATILE_KEY_PATTERNS = ("auth:failures:*", "probe:*")


@pytest.fixture
async def redis_client(settings: Settings) -> AsyncIterator["object"]:
    """Redis client on a cleared keyspace, so counters never leak between tests."""
    import redis.asyncio as redis

    client = redis.from_url(settings.redis_url, decode_responses=True)

    async def clear_volatile_keys() -> None:
        for pattern in VOLATILE_KEY_PATTERNS:
            keys = [key async for key in client.scan_iter(match=pattern, count=100)]
            if keys:
                await client.delete(*keys)

    await clear_volatile_keys()
    try:
        yield client
    finally:
        await clear_volatile_keys()
        await client.aclose()


@pytest.fixture
def libpq_dsn(settings: Settings) -> str:
    """Raw psycopg DSN for assertions that must bypass the ORM."""
    return to_libpq_dsn(settings.test_database_url)


@pytest.fixture
async def platform(settings: Settings) -> AsyncIterator["Platform"]:
    """A clean database plus a client, for API tests.

    Committing, not rolled back: the endpoints under test read committed rows
    through their own sessions, so a per-test transaction is invisible to them.
    See `tests/support/platform.py` for why that is the right shape here.
    """
    from tests.support.platform import running_platform

    async with running_platform(settings) as running:
        yield running


@dataclass(slots=True)
class Cast:
    """The people and places the document tests move between.

    A shared fixture rather than one per module, and it lives here because pytest does
    not collect fixtures from an imported test module: `test_chunking.py` and
    `test_embeddings.py` are separate files about the same pipeline, and a second copy of
    this would be a second set of departments and employees that had to keep agreeing
    with the first.
    """

    #: An ordinary employee in the first department, with an account.
    uploader: "Actor"
    #: Somebody in the same department. Used for "the same file, another person".
    colleague: "Actor"
    #: Somebody in another department: the caller the RLS test hides a row from.
    outsider: "Actor"
    #: Administration, which is who creates company knowledge-base documents.
    admin: "Actor"
    department: str
    other_department: str


@pytest.fixture
async def cast(platform: "Platform") -> Cast:
    """Two departments, four employees, and real signed-in sessions.

    Real logins rather than an injected principal: the permission decision this module
    leans on is made from the snapshot the endpoint builds, and a test that bypassed
    that would prove nothing about which documents a caller actually reaches.
    """
    from uuid import uuid4

    suffix = uuid4().hex[:8]
    department = await platform.department(f"docs{suffix}")
    other_department = await platform.department(f"otros{suffix}")
    position = await platform.position(department, f"gestor{suffix}")
    other_position = await platform.position(other_department, f"otro{suffix}")

    uploader = await platform.account(roles=("employee",))
    await platform.assign(uploader.employee_id, department, position)
    colleague = await platform.account(roles=("employee",))
    await platform.assign(colleague.employee_id, department, position)
    outsider = await platform.account(roles=("employee",))
    await platform.assign(outsider.employee_id, other_department, other_position)

    return Cast(
        uploader=uploader,
        colleague=colleague,
        outsider=outsider,
        admin=await platform.account(roles=("admin",)),
        department=department,
        other_department=other_department,
    )


# --- authenticating API tests ----------------------------------------------


class SignedIn:
    """A caller with a real session cookie.

    Authorisation is exercised through the same path production uses: a session in
    Redis, the epoch check, and a principal resolved from the database. Nothing
    here injects permissions directly, because a test that bypasses the mechanism
    it is testing proves nothing about it.
    """

    def __init__(self, client, account) -> None:  # noqa: ANN001
        self.client = client
        self.account = account

    @property
    def user_id(self):
        return self.account["user_id"]

    async def call(self, method: str, path: str, **kwargs):
        return await self.client.request(method, path, **kwargs)


async def grant_account(
    factory,  # noqa: ANN001 - async_sessionmaker
    *,
    email_prefix: str = "user",
    must_change_password: bool = False,
    department_id=None,  # noqa: ANN001
    job_position_id=None,  # noqa: ANN001
    is_managerial: bool = False,
    clearance_level: str = "low",
    role_extra: str | None = None,
) -> dict:
    """Create an employee, an account, and optionally a position, all committed.

    Committed on purpose: the API reads these rows from a different session, so a
    rolled-back fixture would be invisible to it. This is why the API test modules
    that need a signed-in caller do not use the shared rolled-back session.
    """
    from uuid import uuid4

    from app.core.security import hash_password

    employee_id = uuid4()
    user_id = uuid4()
    username = f"{email_prefix}{uuid4().hex[:8]}"

    async with factory() as session:
        await session.execute(
            text(
                """
                INSERT INTO employees (id, first_name, last_name, email, hire_date, status)
                VALUES (:id, 'Ana', 'Martín', :email, '2024-01-15', 'active')
                """
            ),
            {"id": employee_id, "email": f"{username}@empresa.es"},
        )
        await session.execute(
            text(
                """
                INSERT INTO users (id, employee_id, username, password_hash,
                                   must_change_password, is_active, session_epoch)
                VALUES (:id, :employee_id, :username, :password_hash, :must_change, true, 1)
                """
            ),
            {
                "id": user_id,
                "employee_id": employee_id,
                "username": username,
                "password_hash": hash_password("Str0ng!Password1"),
                "must_change": must_change_password,
            },
        )

        if department_id is not None and job_position_id is not None:
            await session.execute(
                text(
                    """
                    INSERT INTO employee_assignments (id, employee_id, department_id,
                                                      job_position_id, is_primary,
                                                      is_part_time, start_date)
                    VALUES (:id, :employee_id, :department_id, :job_position_id, true,
                            false, '2024-01-15')
                    """
                ),
                {
                    "id": uuid4(),
                    "employee_id": employee_id,
                    "department_id": department_id,
                    "job_position_id": job_position_id,
                },
            )
        await session.commit()

    if is_managerial and department_id is not None:
        async with factory() as session:
            await session.execute(
                text("UPDATE job_positions SET is_managerial = true WHERE id = :id"),
                {"id": job_position_id},
            )
            await session.commit()

    if clearance_level != "low":
        async with factory() as session:
            await session.execute(
                text("UPDATE departments SET clearance_level = :level WHERE id = :id"),
                {"level": clearance_level, "id": department_id},
            )
            await session.commit()

    return {
        "user_id": str(user_id),
        "employee_id": str(employee_id),
        "username": username,
        "password": "Str0ng!Password1",
    }


async def sign_in(client, account: dict) -> SignedIn:  # noqa: ANN001
    response = await client.post(
        "/api/v1/auth/login",
        json={"username": account["username"], "password": account["password"]},
    )
    assert response.status_code == 200, response.text
    return SignedIn(client, account)
