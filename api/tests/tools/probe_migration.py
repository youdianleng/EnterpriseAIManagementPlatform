"""Acceptance probe for ticket 04.

Checks the migration lifecycle on a scratch database: upgrade to head, verify
the schema the ticket promises, downgrade, and upgrade again. A migration that
cannot be reverted is a migration nobody can fix in production.

Run inside the compose network:
    docker compose exec -T api python /app/tests/tools/probe_migration.py
"""

import os
import sys
from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.config import get_settings  # noqa: E402
from app.core.constants import EMBEDDING_DIMENSIONS  # noqa: E402

SCRATCH_DB = "eam_migration_probe"
API_ROOT = Path(__file__).resolve().parents[2]

failures: list[str] = []


def check(label: str, condition: bool, observed: object = "") -> None:
    print(f"[{'ok  ' if condition else 'FAIL'}] {label}: {observed}")
    if not condition:
        failures.append(label)


def alembic_config(scratch_url: str) -> Config:
    config = Config(str(API_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(API_ROOT / "alembic"))
    os.environ["ALEMBIC_DATABASE_URL"] = scratch_url
    return config


def main() -> None:
    settings = get_settings()
    base = settings.sync_test_database_url.rsplit("/", 1)[0]
    scratch_url = f"{base}/{SCRATCH_DB}"

    with psycopg.connect(settings.sync_admin_database_url, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}"')
        admin.execute(f'CREATE DATABASE "{SCRATCH_DB}"')

    config = alembic_config(scratch_url)
    # Read the head from the migration directory rather than pinning a revision:
    # a probe that has to be edited whenever a migration is added stops being run.
    head = ScriptDirectory.from_config(config).get_current_head()

    # --- upgrade ---
    command.upgrade(config, "head")
    with psycopg.connect(scratch_url) as connection:
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        check("upgrade lands on head", revision == head, f"{revision} (head {head})")

        extensions = {
            row[0]
            for row in connection.execute(
                "SELECT extname FROM pg_extension WHERE extname IN ('vector','ltree')"
            ).fetchall()
        }
        check("vector and ltree installed", extensions == {"vector", "ltree"}, extensions)

        dimension = connection.execute(
            """
            SELECT atttypmod FROM pg_attribute
            WHERE attrelid = to_regclass('_vector_smoke') AND attname = 'embedding'
            """
        ).fetchone()
        check(
            "smoke column carries the locked dimension",
            dimension is not None and dimension[0] == EMBEDDING_DIMENSIONS,
            dimension[0] if dimension else None,
        )

        index_defs = [
            row[0]
            for row in connection.execute(
                "SELECT indexdef FROM pg_indexes WHERE tablename = '_vector_smoke'"
            ).fetchall()
        ]
        check(
            "HNSW cosine index present",
            any("hnsw" in d and "vector_cosine_ops" in d for d in index_defs),
            len(index_defs),
        )

        # A real query, not just a schema assertion.
        unit0 = str([1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1))
        unit1 = str([0.0, 1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 2))
        connection.execute(
            "INSERT INTO _vector_smoke (id, embedding) VALUES (1, %s), (2, %s)", (unit1, unit0)
        )
        order = connection.execute(
            "SELECT id FROM _vector_smoke ORDER BY embedding <=> %s LIMIT 2", (unit0,)
        ).fetchall()
        check("cosine ordering puts the nearest first", [r[0] for r in order] == [2, 1], order)

        connection.rollback()

    # --- downgrade ---
    command.downgrade(config, "base")
    with psycopg.connect(scratch_url) as connection:
        smoke = connection.execute("SELECT to_regclass('_vector_smoke')").fetchone()[0]
        check("downgrade removes the smoke table", smoke is None, smoke)

    # --- upgrade again (idempotent re-apply) ---
    command.upgrade(config, "head")
    with psycopg.connect(scratch_url) as connection:
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        check("re-upgrade returns to head", revision == head, revision)

    with psycopg.connect(settings.sync_admin_database_url, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}"')

    print()
    print("FAIL" if failures else "ALL CHECKS PASSED")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
