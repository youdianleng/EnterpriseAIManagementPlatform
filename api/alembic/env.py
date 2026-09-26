"""Alembic environment.

The database URL comes from application settings rather than alembic.ini, so a
migration run can never target a different database than the application does.
`ALEMBIC_DATABASE_URL` overrides it, which is how the test suite migrates the
test database without touching configuration.
"""

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Allow `alembic` to import the application package when run from ./api.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Registering the vector type is what stops SQLAlchemy emitting
# "Did not recognize type 'vector'" every time it reflects the smoke table.
from pgvector.sqlalchemy import Vector  # noqa: E402,F401

from app.config import get_settings, to_libpq_dsn  # noqa: E402
from app.db_metadata import metadata  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = metadata

# Tables that exist in the database but have no ORM model.
#
# `_vector_smoke` is created by migration 0001 to prove the vector column and
# HNSW index work against a real server (ticket 04). Without this exclusion,
# every autogenerate run would propose dropping it as schema drift.
UNMANAGED_TABLES = {"_vector_smoke"}


def include_object(
    object_: object, name: str | None, type_: str, reflected: bool, compare_to: object | None
) -> bool:
    if type_ == "table" and name in UNMANAGED_TABLES:
        return False
    return True


def database_url() -> str:
    override = os.environ.get("ALEMBIC_DATABASE_URL")
    if override:
        return override
    # Alembic runs synchronously, so the async driver suffix must go.
    return to_libpq_dsn(get_settings().database_url)


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = database_url()

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
