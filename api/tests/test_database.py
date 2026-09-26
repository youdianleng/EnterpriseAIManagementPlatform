"""Integration tests for the database foundation.

These need a real PostgreSQL: the vector type, the HNSW index and the dimension
guard are all database behaviour. Against a substitute they would assert nothing.
"""

import re
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.config import Settings, to_libpq_dsn
from app.core.constants import EMBEDDING_DIMENSIONS

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "alembic" / "versions"
SMOKE_TABLE = "_vector_smoke"


def _migration_source() -> str:
    files = sorted(MIGRATIONS_DIR.glob("*.py"))
    assert files, "no migration files found"
    return "\n".join(path.read_text(encoding="utf-8") for path in files)


async def test_vector_and_ltree_extensions_are_installed(connection: AsyncConnection) -> None:
    rows = (
        await connection.execute(
            text(
                "SELECT extname FROM pg_extension "
                "WHERE extname IN ('vector', 'ltree') ORDER BY extname"
            )
        )
    ).scalars()
    assert set(rows) == {"ltree", "vector"}


async def test_smoke_table_vector_column_has_the_expected_dimension(
    connection: AsyncConnection,
) -> None:
    dimension = await connection.scalar(
        text(
            """
            SELECT atttypmod
            FROM pg_attribute
            WHERE attrelid = to_regclass(:table) AND attname = 'embedding'
            """
        ),
        {"table": SMOKE_TABLE},
    )
    assert dimension == EMBEDDING_DIMENSIONS


async def test_smoke_table_has_an_hnsw_index_with_cosine_ops(
    connection: AsyncConnection,
) -> None:
    definitions = (
        await connection.execute(
            text("SELECT indexdef FROM pg_indexes WHERE tablename = :table"),
            {"table": SMOKE_TABLE},
        )
    ).scalars().all()

    # The primary key index is also present, so search rather than assume one row.
    hnsw = [definition for definition in definitions if "hnsw" in definition]
    assert hnsw, f"no HNSW index among {definitions}"
    assert "vector_cosine_ops" in hnsw[0]


async def test_cosine_ordering_returns_the_nearest_vector_first(
    connection: AsyncConnection,
) -> None:
    """Values are chosen so the expected order is obvious, not approximate.

    `unit_0` is the query exactly (distance 0) and `unit_1` is orthogonal to it
    (distance 1), so any tolerance in the assertion would be hiding a bug.
    """
    unit_0 = [1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1)
    unit_1 = [0.0, 1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 2)
    await connection.execute(
        text(f"INSERT INTO {SMOKE_TABLE} (id, embedding) VALUES (1, :a), (2, :b)"),
        {"a": str(unit_1), "b": str(unit_0)},
    )

    rows = (
        await connection.execute(
            text(
                f"SELECT id, embedding <=> :probe AS distance FROM {SMOKE_TABLE} "
                "ORDER BY distance LIMIT 2"
            ),
            {"probe": str(unit_0)},
        )
    ).all()

    assert [row.id for row in rows] == [2, 1]
    assert rows[0].distance == pytest.approx(0.0, abs=1e-6)
    assert rows[1].distance == pytest.approx(1.0, abs=1e-6)


async def test_wrong_dimension_is_rejected_by_the_database(connection: AsyncConnection) -> None:
    """The schema, not an application check, is what enforces the dimension."""
    with pytest.raises(Exception) as excinfo:
        await connection.execute(
            text(f"INSERT INTO {SMOKE_TABLE} (id, embedding) VALUES (99, :vec)"),
            {"vec": str([0.1] * (EMBEDDING_DIMENSIONS - 1))},
        )
    assert "dimension" in str(excinfo.value).lower()


def test_migration_dimension_matches_the_application_constant() -> None:
    """A pgvector column's dimension is part of the schema.

    The migration keeps a literal so it always describes what it applied; this
    test is what stops that literal drifting from the constant the application
    embeds with. Drift would only appear as an insert failure in production.
    """
    source = _migration_source()
    match = re.search(r"VECTOR_DIMENSIONS\s*=\s*(\d+)", source)
    assert match, "the migration no longer declares VECTOR_DIMENSIONS"
    assert int(match.group(1)) == EMBEDDING_DIMENSIONS


def test_migration_declares_its_own_revision_chain() -> None:
    source = _migration_source()
    assert 'revision: str = "0001"' in source
    assert "down_revision: str | None = None" in source


def test_libpq_dsn_conversion_strips_the_driver_suffix() -> None:
    assert (
        to_libpq_dsn("postgresql+psycopg://u:p@h:5432/db") == "postgresql://u:p@h:5432/db"
    )
    # Already a libpq DSN: returned untouched rather than mangled.
    assert to_libpq_dsn("postgresql://u:p@h:5432/db") == "postgresql://u:p@h:5432/db"


def test_the_application_under_test_uses_the_test_database(settings: Settings) -> None:
    """A test run must never be able to truncate development data.

    The earlier version of this test asserted that `settings.database_url` did
    *not* name the test database — which was true only because `conftest` used
    `setdefault` on an environment variable the container already exported, so
    the application quietly kept pointing at `eam`. That is the defect, written
    down as an expectation; it left 181 audit rows in the development database.

    What matters is that the settings the application actually uses name the test
    database, so this asserts that directly.
    """
    assert settings.database_url.endswith(f"/{settings.test_database_name}")
    assert settings.test_database_url.endswith(f"/{settings.test_database_name}")
    assert settings.admin_database_url.endswith("/postgres")
