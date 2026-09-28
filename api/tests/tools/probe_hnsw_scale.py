"""Measure the vector index at corpus scale, on a table shaped like the real one.

`docs/DESIGN.md` §10.3 chose `vector(1536)` + HNSW + `vector_cosine_ops` from a measurement
taken on a *smoke table* — `probe_vector_dimensions.py` compares dimension choices and owns
that decision. This probe answers the question that decision leaves open: **what does that
index cost and return on `document_chunks`, with the row-level policy attached and the
parent/child rows the split really writes?**

    docker compose exec -T -e TEST_DATABASE_NAME=eam_emb_probe \
        api python /app/tests/tools/probe_hnsw_scale.py

**It runs against a scratch database and owns one schema in it.** `probe_hnsw` is created at
the start and dropped at the end, so re-running it is safe and leaves nothing behind — the
other probes in this directory wipe rows they do not own, and this one has no business doing
that to anybody's development corpus. `PROBE_ROWS` changes the corpus size and the report
prints what was actually measured.

What it reports, and what each number means:

* **rows / dimensions / build s / table MB** — the corpus, and what indexing it costs. A
  migration builds the index once, so "13.6 s at 20 000 rows" is the answer to "minutes or
  hours", and the size is the same figure §10.3's table compares across dimension choices.
* **query, index forced** — the HNSW scan with sequential scans disabled, so this is the
  index's own cost. Without the setting the planner may reasonably prefer a sequential scan
  at this row count, which would make the number a measurement of the table.
* **query, exact scan** — the same queries with the index disabled: the baseline the index
  has to beat.
* **query, runtime role + policy** — the same queries as `eam_app` with a caller context
  published, which is what a request pays. Reported with the policy's plan, because the
  interesting fact about ticket 31's predicate is not its milliseconds at this size but
  *how the planner treats it*: with the search path pinned to the index the predicate is
  costed as though the scan returned the whole table.
* **recall@5** — the fraction of the true five nearest neighbours the index returned,
  against an exact scan of the same queries. HNSW is approximate, and a query time without a
  recall figure is a number that can be improved by making the answer worse.

Synthetic random vectors, so this measures *index* behaviour and not retrieval quality. What
it can rule out is "the index does not fit" and "an indexed query drops to seconds at this
size", which are the two ways §10.3's decision could be wrong at scale.
"""

import os
import statistics
import time

import psycopg

DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql://eam:eam_dev_password@postgres:5432/eam_emb_probe",
).replace("postgresql+psycopg://", "postgresql://")

#: The corpus size. Ten thousand documents is the ticket's figure; at the ~2 children per
#: document this split produces, 20 000 chunks is that corpus.
ROWS = int(os.environ.get("PROBE_ROWS", "20000"))

#: The locked dimension (§10.3). A literal, like the migration's.
DIMENSIONS = 1536

QUERIES = 30
TOP_K = 5

#: The schema this probe owns, dropped at the end and recreated at the start.
SCHEMA = "probe_hnsw"

#: The runtime role, which is who the policy has to mean something for: a table's owner is
#: exempt from its own policies, so the policy measurement has to run as somebody else.
RUNTIME_ROLE = "eam_app"

CREATE_SCHEMA = f"""
CREATE TABLE {SCHEMA}.documents (
    id uuid PRIMARY KEY,
    owner_employee_id uuid,
    is_company_kb boolean NOT NULL DEFAULT false
);

CREATE TABLE {SCHEMA}.chunks (
    id uuid PRIMARY KEY,
    document_id uuid NOT NULL REFERENCES {SCHEMA}.documents (id) ON DELETE CASCADE,
    parent_chunk_id uuid REFERENCES {SCHEMA}.chunks (id) ON DELETE CASCADE,
    chunk_index integer NOT NULL,
    content text NOT NULL,
    token_count integer NOT NULL,
    embedding vector({DIMENSIONS}),
    embedding_model text,
    chunking_version text NOT NULL,
    search_vector tsvector GENERATED ALWAYS AS (to_tsvector('spanish', content)) STORED
);

CREATE INDEX ix_probe_chunks_document ON {SCHEMA}.chunks (document_id, chunk_index);
"""

#: Ticket 31's policy on the synthetic tables: a chunk is readable exactly when its document
#: is, through the same join the real policy makes. Written out here rather than copied,
#: because this probe must run in a database that has never been migrated.
CREATE_POLICY = f"""
ALTER TABLE {SCHEMA}.chunks ENABLE ROW LEVEL SECURITY;
CREATE POLICY probe_chunks_access ON {SCHEMA}.chunks
    FOR ALL
    USING (
        EXISTS (
            SELECT 1 FROM {SCHEMA}.documents d
             WHERE d.id = chunks.document_id
               AND (d.owner_employee_id
                        = nullif(current_setting('app.current_employee_id', true), '')::uuid
                    OR d.is_company_kb)
        )
    )
    WITH CHECK (true);
GRANT USAGE ON SCHEMA {SCHEMA} TO {RUNTIME_ROLE};
GRANT SELECT ON ALL TABLES IN SCHEMA {SCHEMA} TO {RUNTIME_ROLE};
"""

#: One synthetic document per 2 000 rows, and a random vector per chunk. `random()` is
#: volatile, so the aggregate is evaluated per row rather than folded once — which is what
#: makes the corpus 20 000 *different* vectors instead of one repeated.
LOAD = f"""
INSERT INTO {SCHEMA}.documents (id, owner_employee_id, is_company_kb)
SELECT gen_random_uuid(), gen_random_uuid(), false FROM generate_series(1, 10);

INSERT INTO {SCHEMA}.chunks (id, document_id, parent_chunk_id, chunk_index, content,
                             token_count, embedding, embedding_model, chunking_version)
SELECT gen_random_uuid(),
       (SELECT id FROM {SCHEMA}.documents OFFSET ((g - 1) / 2000) LIMIT 1),
       NULL,
       g,
       'Clausula sintetica numero ' || g || ' sobre vacaciones y permisos.',
       120,
       ('[' || (SELECT string_agg(random()::text, ',' ORDER BY d)
                  FROM generate_series(1, {DIMENSIONS}) AS d) || ']')::vector,
       'probe-synthetic',
       'parent-child-v1'
FROM generate_series(1, {ROWS}) AS g;
"""


def scalar(connection: psycopg.Connection, sql: str, parameters: tuple = ()):  # noqa: ANN201
    row = connection.execute(sql, parameters).fetchone()
    return row[0] if row else None


def build(connection: psycopg.Connection) -> None:
    connection.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    connection.execute(f"CREATE SCHEMA {SCHEMA}")
    connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
    connection.execute(f"SET search_path TO {SCHEMA}, public")
    for statement in CREATE_SCHEMA.strip().split(";"):
        if statement.strip():
            connection.execute(statement)
    started = time.perf_counter()
    connection.execute(LOAD)
    connection.execute(CREATE_POLICY)
    connection.commit()
    print(f"loaded {ROWS} rows in {time.perf_counter() - started:.1f} s", flush=True)


def index(connection: psycopg.Connection) -> float:
    started = time.perf_counter()
    connection.execute(
        f"CREATE INDEX ix_probe_chunks_embedding ON {SCHEMA}.chunks "
        "USING hnsw (embedding vector_cosine_ops)"
    )
    connection.execute(
        f"CREATE INDEX ix_probe_chunks_search ON {SCHEMA}.chunks USING gin (search_vector)"
    )
    connection.execute(
        f"CREATE INDEX ix_probe_chunks_unembedded ON {SCHEMA}.chunks (document_id) "
        "WHERE parent_chunk_id IS NOT NULL AND embedding IS NULL"
    )
    # Statistics after the load, not before it: the planner chooses between the index and a
    # sequential scan on the estimated selectivity, and a table whose statistics say "empty"
    # gets the scan.
    connection.execute(f"ANALYZE {SCHEMA}.chunks")
    connection.commit()
    return time.perf_counter() - started


def plan_for(connection: psycopg.Connection, probe: str) -> str:
    """The plan the planner picks, with the 12 000-character probe cut out of it."""
    rows = connection.execute(
        f"EXPLAIN (COSTS OFF) SELECT id FROM {SCHEMA}.chunks "
        f"ORDER BY embedding <=> %s::vector LIMIT {TOP_K}",
        (probe,),
    ).fetchall()
    plan = " -> ".join(row[0].strip().splitlines()[0].strip() for row in rows)
    return " ".join(plan.split())[:160]


def probe_vectors(connection: psycopg.Connection) -> list[str]:
    rows = connection.execute(
        f"SELECT embedding::text FROM {SCHEMA}.chunks ORDER BY chunk_index LIMIT {QUERIES}"
    ).fetchall()
    return [row[0] for row in rows]


def neighbours(connection: psycopg.Connection, probes: list[str]) -> list[set[str]]:
    found: list[set[str]] = []
    for vector in probes:
        rows = connection.execute(
            f"SELECT id::text FROM {SCHEMA}.chunks ORDER BY embedding <=> %s::vector LIMIT {TOP_K}",
            (vector,),
        ).fetchall()
        found.append({row[0] for row in rows})
    return found


def time_queries(connection: psycopg.Connection, probes: list[str]) -> list[float]:
    """Per-query milliseconds, with one warm-up discarded, as the session is configured."""
    connection.execute(
        f"SELECT id FROM {SCHEMA}.chunks ORDER BY embedding <=> %s::vector LIMIT {TOP_K}",
        (probes[0],),
    ).fetchall()
    timings: list[float] = []
    for vector in probes:
        started = time.perf_counter()
        connection.execute(
            f"SELECT id FROM {SCHEMA}.chunks ORDER BY embedding <=> %s::vector LIMIT {TOP_K}",
            (vector,),
        ).fetchall()
        timings.append((time.perf_counter() - started) * 1000)
    connection.commit()
    return timings


def report(timings: list[float], label: str) -> None:
    ordered = sorted(timings)
    p50 = statistics.median(ordered)
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    print(f"  {label}: p50 {p50:7.2f} ms   p95 {p95:7.2f} ms   (n={len(ordered)})", flush=True)


def main() -> None:
    print(f"rows={ROWS} dimensions={DIMENSIONS} queries={QUERIES} top_k={TOP_K}", flush=True)
    with psycopg.connect(DSN, autocommit=False) as connection:
        build(connection)
        seconds = index(connection)
        size = float(
            scalar(connection, f"SELECT pg_total_relation_size('{SCHEMA}.chunks') / 1048576.0")
        )
        print(f"\nindex build: {seconds:.1f} s    table + indexes: {size:.1f} MB\n", flush=True)

        probes = probe_vectors(connection)
        print(f"  plan, no forcing        : {plan_for(connection, probes[0])}", flush=True)

        # The index's own cost: sequential scans off, so the HNSW scan is what runs.
        connection.execute("SET enable_seqscan = off")
        report(time_queries(connection, probes), "query, index forced     ")
        print(f"  plan, index forced      : {plan_for(connection, probes[0])}", flush=True)

        # Recall against the same exact scan, while the index is still on.
        found = neighbours(connection, probes)
        connection.execute("SET enable_indexscan = off")
        connection.execute("SET enable_indexonlyscan = off")
        truth = neighbours(connection, probes)
        report(time_queries(connection, probes), "query, exact scan       ")
        recall = sum(len(t & f) for t, f in zip(truth, found, strict=True)) / (TOP_K * len(probes))
        print(f"  recall@{TOP_K}: {recall:.3f}   (vs the exact scan, same queries)", flush=True)

        connection.execute("RESET enable_indexscan")
        connection.execute("RESET enable_indexonlyscan")

        # The same queries as the runtime role with a caller context published: a request's
        # cost, policy included. `SET enable_seqscan = off` stays on, because a retrieval
        # query that the planner answers with a scan is exactly the case being examined.
        connection.commit()
        connection.execute(f"SET ROLE {RUNTIME_ROLE}")
        connection.execute(f"SET search_path TO {SCHEMA}, public")
        connection.execute(
            "SELECT set_config('app.current_employee_id', %s, false)",
            (str(scalar(connection, f"SELECT owner_employee_id FROM {SCHEMA}.documents LIMIT 1")),),
        )
        connection.commit()
        print(f"  plan, runtime role      : {plan_for(connection, probes[0])}", flush=True)
        report(time_queries(connection, probes), "query, runtime + policy ")
        # And with the planner free to choose, which is what a request actually gets.
        connection.execute("RESET enable_seqscan")
        report(time_queries(connection, probes), "query, runtime, planner ")
        connection.execute("RESET ROLE")

        connection.execute("DROP SCHEMA " + SCHEMA + " CASCADE")
        connection.commit()
    print("\nschema dropped; nothing left behind", flush=True)


if __name__ == "__main__":
    main()
