"""Extensions and the vector smoke schema.

Revision ID: 0001
Revises:
Created: 2026-09-25

This migration deliberately creates no business tables. Departments, employees
and the rest arrive with the tickets that build their behaviour, so each table
is introduced alongside the screen and tests that prove it correct.

What this does establish is the part that is expensive to change later:

* the `vector` and `ltree` extensions;
* the embedding column dimension, written literally in the DDL below and
  guarded by a test that compares it against
  `app.core.constants.EMBEDDING_DIMENSIONS`. A pgvector column's dimension is
  part of the schema, so a divergence between the constant and the database
  would only surface as a runtime insert failure.
* the HNSW index shape and operator class, on a throwaway table, so the choice
  is exercised by a real query rather than assumed.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Kept as a literal on purpose: a migration must describe the schema it applied,
# even if the application constant changes later. The test suite asserts they match.
VECTOR_DIMENSIONS = 1536

SMOKE_TABLE = "_vector_smoke"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    # ltree backs the department hierarchy in ticket 06.
    op.execute("CREATE EXTENSION IF NOT EXISTS ltree")

    # Throwaway table: exercises the vector column and index in a real database.
    # Dropped by the test fixture; it never holds application data.
    op.execute(
        f"""
        CREATE TABLE {SMOKE_TABLE} (
            id integer PRIMARY KEY,
            embedding vector({VECTOR_DIMENSIONS}) NOT NULL
        )
        """
    )
    op.execute(
        f"""
        CREATE INDEX {SMOKE_TABLE}_embedding_hnsw
        ON {SMOKE_TABLE} USING hnsw (embedding vector_cosine_ops)
        """
    )


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS {SMOKE_TABLE}")
    # Extensions are left installed: other schemas may rely on them and dropping
    # an extension is not the inverse of creating it.
