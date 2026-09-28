"""The `langgraph` schema: where an interrupted agent run waits (ticket 38).

Revision ID: 0026
Revises: 0025
Created: 2026-10-08

**Chain position:** 0025 (`20261007_1000_personal_documents.py`, ticket 36) → 0026 (this,
ticket 38). The directory *and* `alembic heads` were read immediately before this file was
written — `heads` reported `0025` and nothing else — because the ids have collided
repeatedly in this project.

**What this migration is, and what it deliberately is not.** DESIGN §10.2 decides that the
agent's interrupt state lives in *this* Postgres, in its own `langgraph` schema, and never
in Redis. So the schema is ours: this migration creates it and names who may use it. The
**tables inside it are not ours** — `checkpoints`, `checkpoint_blobs`, `checkpoint_writes`
and `checkpoint_migrations` are written and migrated by
`langgraph-checkpoint-postgres`, whose `setup()` reads `checkpoint_migrations` and applies
anything newer than the version it finds there.

Writing those four `CREATE TABLE`s out here would look more "migration-shaped" and would be
worse in two ways. It would freeze version 3.1.2's DDL into a revision that can never
follow the library, so the next minor bump would need a migration that hand-diffs a schema
we do not own; and it would put a second copy of the checkpoint format in this repository,
where the only reader who could keep it correct is the one who already has the real one.
`setup()` is idempotent (the library's documentation says it must be called once, by
whoever uses the checkpointer first) and `app/ai/agents/checkpoint.py` calls it, so the
schema here plus `setup()` at the seam is the whole arrangement.

**`CREATE` on the schema is granted, and that is not an oversight.** `setup()` runs as the
*request* role (`eam_app`), not as the table owner, because the checkpointer opens its own
connection with the same credentials the rest of the request path uses — a deployment that
migrated as `eam` and checked points as `eam` would be the one place where the restricted
role's absence went unnoticed. The role therefore needs to be able to create those four
tables the first time. After `setup()` has run they are owned by `eam_app` and the grant is
only what keeps a fresh database able to bootstrap.

**No default privileges here.** Migration 0007's `ALTER DEFAULT PRIVILEGES` is scoped to
`public`, and it should stay that way: the checkpoint tables are not business tables, and a
blanket grant that reached them would be a grant nobody decided to make. `eam` owns the
schema and can drop it; `eam_app` can use it; no third role has anything.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Spelled here as migration 0007 spells it: a migration describes the schema it applied,
#: and the application's constant for the same name may move afterwards.
APP_ROLE = "eam_app"

SCHEMA = "langgraph"

SCHEMA_COMMENT = (
    "LangGraph checkpoint state (DESIGN 10.2): an interrupted agent run survives a "
    "restart. Tables are created by langgraph-checkpoint-postgres setup(), not by "
    "an Alembic revision. Never a Redis database: this state must not be lost."
)


def upgrade() -> None:
    # A plain `CREATE SCHEMA`: the schema is the one thing this revision owns, and it must
    # exist before any connection can put `langgraph` on its `search_path`. `IF NOT EXISTS`
    # rather than a bare create because a database that already has the schema — one where
    # a developer ran the checkpointer's `setup()` against `public` and then moved it, or a
    # restored dump — should be brought to the same state rather than fail the deploy.
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")

    # The request role: USAGE to see the schema, CREATE so the library's first `setup()`
    # can create its four tables. DML on those tables needs no statement here — `eam_app`
    # creates them and therefore owns them.
    op.execute(f"GRANT USAGE, CREATE ON SCHEMA {SCHEMA} TO {APP_ROLE}")

    # The comment is the part a reader finds from `\\dn+`: without it the schema's name
    # says nothing about why it is separate from `public`, and "it is where LangGraph
    # lives" is exactly the kind of fact that gets moved into `public` by someone tidying
    # up a year later.
    op.execute(f"COMMENT ON SCHEMA {SCHEMA} IS '{SCHEMA_COMMENT}'")


def downgrade() -> None:
    """Drop the schema and everything in it.

    **`CASCADE`, and the data loss is the point.** An interrupted run's state has no
    meaning without the feature that reads it, and leaving four orphan tables in a schema
    no revision creates would make `upgrade` after `downgrade` a different operation than
    `upgrade` on a fresh database. A downgrade of ticket 38 is a decision to discard every
    paused confirmation in the installation, and this statement is the honest way to say
    that. `IF EXISTS` so the down migration is safe on a database where `setup()` never ran.
    """
    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
