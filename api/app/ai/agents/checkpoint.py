"""Where an interrupted run waits: the `langgraph` schema of this same Postgres.

DESIGN §10.2 is the decision this module implements — **同库 Postgres，独立 schema，不使用
Redis** — and the reason is in §6.1: 「等待员工确认」 is a state that has to survive the employee
closing their browser and coming back the next day. Redis holds caches and sessions, which
may be lost; a half-confirmed draft may not. So the checkpointer is
`langgraph-checkpoint-postgres`, and it writes to the `langgraph` schema that Alembic
revision 0026 creates (`api/alembic/versions/20261008_1000_langgraph_checkpoints.py`).

**Two rails, and each owns half the fact.**

* The **schema** is ours. A migration creates it, comments it, and grants the request role
  `USAGE, CREATE` on it. A schema is a statement about this installation's layout, and the
  project already keeps every such statement in a migration.
* The **tables** are the library's. `checkpoints`, `checkpoint_blobs`, `checkpoint_writes`
  and `checkpoint_migrations` are created by `AsyncPostgresSaver.setup()`, which reads the
  version it finds in `checkpoint_migrations` and applies anything newer. Writing that DDL
  into a revision would freeze one release's format into a migration that can never follow
  the library. `open_checkpointer` calls `setup()` on the connection it opens — idempotent,
  and the library's own documented requirement for the first user.

**How the schema is selected.** The connection carries
`options=-csearch_path=langgraph`, i.e. the schema is chosen by the *connection* rather than
by the caller remembering to qualify table names. The library's SQL uses unqualified names
(`INSERT INTO checkpoints …`), so this is the only place the choice can be made, and making
it here means a future query cannot accidentally read `public.checkpoints` — there is no
such table, and a typo fails loudly instead of silently writing somewhere else.

**The role is the request role, not the table owner.** The checkpointer connects with
`settings.runtime_database_url` — `eam_app` — exactly as the rest of a request does. A
checkpointer that ran as the schema owner would be the one path in the application where the
restricted role's absence went unnoticed, and it is the *state of an employee's draft* that
would be sitting in tables no policy guards.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.config import Settings, to_libpq_dsn

#: The schema the checkpoint tables live in. Spelled here as migration 0026 spells it.
CHECKPOINT_SCHEMA = "langgraph"


def checkpoint_dsn(settings: Settings, *, test: bool = False) -> str:
    """The libpq DSN a checkpointer connects with, with the schema already selected.

    `test=True` points at the same server's test database, which is what a test run needs
    and what `Settings.runtime_test_database_url` already computes for the application's
    own role. The `options` parameter is percent-encoded because it is a libpq *conninfo*
    keyword whose value itself contains an `=`, and passing it unencoded turns the rest of
    the DSN into part of that value.
    """
    url = settings.runtime_test_database_url if test else settings.runtime_database_url
    base = to_libpq_dsn(url)
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}options=-csearch_path%3D{CHECKPOINT_SCHEMA}"


@asynccontextmanager
async def open_checkpointer(
    settings: Settings, *, test: bool = False, prepare: bool = True
) -> AsyncIterator[AsyncPostgresSaver]:
    """An open Postgres checkpointer, ready to hand to `graph.build_graph`.

    `prepare` runs the library's `setup()` — create the tables if they are not there, apply
    any migration newer than the version recorded in `checkpoint_migrations`. It is
    idempotent and costs a couple of statements once the tables exist, so the default is on:
    a caller who forgets it gets "relation langgraph.checkpoints does not exist" at the
    first run, which is a worse error than a redundant query. A deployment that prepares
    once (at boot) may pass `prepare=False` for every later connection.

    The connection is **autocommit**, as the library's own `from_conn_string` makes it: the
    checkpointer writes one row per step and must not hold a transaction open across the
    whole run, which is also why it is deliberately *not* the request path's session — an
    interrupted conversation is not a unit of work that a retry should roll back.
    """
    async with AsyncPostgresSaver.from_conn_string(checkpoint_dsn(settings, test=test)) as saver:
        if prepare:
            await saver.setup()
        yield saver


__all__ = [
    "CHECKPOINT_SCHEMA",
    "checkpoint_dsn",
    "open_checkpointer",
]
