"""Chunk embeddings: which model made them, the full-text index, and the worklist.

Revision ID: 0022
Revises: 0021
Created: 2026-09-28

**Chain position:** 0021 (`20261004_1000_documents.py`, ticket 31) → 0022 (this,
ticket 32). The directory was read immediately before this file was written —
`alembic heads` reported `0021` and nothing else. The filename's date is behind the
revision it follows because that date is when ticket 32 was written rather than where
it sits in the chain; the revision id is the chain.

Ticket 31 created `document_chunks` with every column this ticket needs except three,
and the reason each one is a migration rather than a query is different:

* **`embedding_model` — a column that must be on the row.** §10.3 fixes the dimension
  and `core.constants` names the model; a model change is a re-embedding migration, and
  a migration cannot target rows whose model it cannot read. The design's risk register
  asks for exactly this ("保留 `chunking_version` 与 `embedding_model` 字段以便灰度重嵌"),
  and `chunking_version` has been there since ticket 31. Nullable, because a chunk with
  no vector has no model — and that pairing is the worklist: `embedding IS NULL` means
  "not embedded", and `embedding_model` means "by which model", which together answer
  "what is stale after a model change" in one query.

* **`search_vector` — a generated column, so the index cannot disagree with the text.**
  A trigger or an application-written column drifts the first time somebody updates
  `content` without updating it; a `GENERATED ALWAYS AS ... STORED` column is
  recomputed by the server on every write, which makes "the tsvector matches the text"
  a property of the table rather than of every code path that writes to it. Ticket 33's
  hybrid retrieval fuses this with the vector ranking.

  **The configuration is `spanish`, and the `language` column on `documents` is not
  used.** Three reasons, in order of weight: the corpus this product holds is Spanish
  (the design's own §10.4 default is `es`, and the ticket names Spanish explicitly);
  the configuration has to be *constant* for a generated column to be possible at all,
  and `to_tsvector(regconfig, text)` is immutable exactly when the configuration is a
  literal rather than a column; and Spanish stemming is the one that helps most here —
  `vacaciones`/`vacación` and `solicitar`/`solicitud` collapse to one stem, which is
  what makes a question match a policy that words the answer differently. English text
  in a Spanish configuration is stemmed with Spanish rules: `holidays` becomes
  `holiday` (harmless), and an English word ending in `-a` or `-o` may be mangled
  (rare, and the vector half of the hybrid covers it). The alternative — a second
  column and a second index per language — buys precision for a corpus that is
  overwhelmingly one language, and arrives with the ticket that has a second language
  to serve.

* **The partial index on unembedded children.** The re-embed worklist is "a child with
  no vector", and it is asked once per job pass over a table that is almost entirely
  filled afterwards. `parent_chunk_id IS NOT NULL` is part of the predicate and not a
  detail: a parent is never embedded, so an index (or a query) that asked only for a
  NULL embedding would name every document the split gave a parent row — the worklist
  would never empty and every pass would re-embed the corpus. Partial, so the index
  holds the rows that need work rather than the rows that do not.

No change to the row-level policies and none to the privileges: `document_chunks`
already carries `document_chunks_access` and the runtime role's full set, and this
migration adds columns to an existing table rather than a table. `INSERT`/`UPDATE` on
the two new columns is the same privilege the pipeline already had.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The text search configuration, written as a literal because that is what makes the
#: generated column possible: `to_tsvector` is immutable in its one-argument form only
#: when the configuration is not a column. See the module docstring for why Spanish.
TEXT_SEARCH_CONFIG = "spanish"

#: The generated column. `coalesce` on the heading because a NULL in a concatenation
#: would make the whole vector NULL, and a chunk without a heading is the ordinary
#: case rather than a broken one.
SEARCH_VECTOR_EXPRESSION = (
    f"to_tsvector('{TEXT_SEARCH_CONFIG}', "
    "coalesce(heading_path, '') || ' ' || content)"
)


def upgrade() -> None:
    op.add_column(
        "document_chunks",
        sa.Column("embedding_model", sa.String(length=32), nullable=True),
    )
    op.execute(
        "ALTER TABLE document_chunks ADD COLUMN search_vector tsvector "
        f"GENERATED ALWAYS AS ({SEARCH_VECTOR_EXPRESSION}) STORED"
    )
    # GIN rather than GiST: this index answers "which chunks contain these lexemes",
    # which is a lookup rather than a nearest-neighbour scan, and GIN is the faster of
    # the two for that and the slower to write — a trade this table takes on the read
    # side, where ticket 33's hybrid retrieval lives.
    op.execute(
        "CREATE INDEX ix_document_chunks_search ON document_chunks "
        f"USING gin (search_vector)"
    )
    # The re-embed worklist. Partial, so it holds the rows that still need a vector and
    # not the corpus — and keyed on children, because a parent is never embedded.
    op.execute(
        "CREATE INDEX ix_document_chunks_unembedded ON document_chunks (document_id) "
        "WHERE parent_chunk_id IS NOT NULL AND embedding IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_document_chunks_unembedded")
    op.execute("DROP INDEX IF EXISTS ix_document_chunks_search")
    op.execute("ALTER TABLE document_chunks DROP COLUMN IF EXISTS search_vector")
    op.drop_column("document_chunks", "embedding_model")
