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
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

API_ROOT = Path(__file__).resolve().parents[1]

# The application reads settings at import time, so the test database must be
# selected before anything imports app.config.
os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg://eam:eam_dev_password@postgres:5432/eam_test"
)
os.environ.setdefault("APP_ENV", "test")

from app.config import Settings, get_settings, to_libpq_dsn  # noqa: E402


@pytest.fixture(scope="session")
def settings() -> Settings:
    return get_settings()


@pytest.fixture(scope="session")
def test_database_url(settings: Settings) -> str:
    return settings.sync_test_database_url


@pytest.fixture(scope="session", autouse=True)
def migrated_database(settings: Settings) -> Iterator[None]:
    """Create the test database if missing, then migrate it to head."""
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


@pytest.fixture
async def redis_client(settings: Settings) -> AsyncIterator["object"]:
    """Redis client on a flushed database, so counters never leak between tests."""
    import redis.asyncio as redis

    client = redis.from_url(settings.redis_url, decode_responses=True)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


@pytest.fixture
def libpq_dsn(settings: Settings) -> str:
    """Raw psycopg DSN for assertions that must bypass the ORM."""
    return to_libpq_dsn(settings.test_database_url)
