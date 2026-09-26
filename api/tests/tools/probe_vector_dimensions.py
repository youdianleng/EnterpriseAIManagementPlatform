"""Measure the real cost of vector dimension choice before locking the schema.

Ticket 04 forbids changing the column dimension later, so this runs the
comparison now: build an HNSW index and run queries at 1536 and at 3072
dimensions, at the document scale the design doc targets (10k chunks).

Run inside the compose network:
    docker compose exec -T api python /app/tests/tools/probe_vector_dimensions.py
"""

import os
import time

import psycopg

DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql://eam:eam_dev_password@postgres:5432/eam",
).replace("postgresql+psycopg://", "postgresql://")

ROWS = 10_000
QUERIES = 30
TOP_K = 5


def vector_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{value:.5f}" for value in values) + "]"


def make_ready(
    connection: psycopg.Connection, table: str, dimension: int, column_type: str
) -> None:
    connection.execute(f"DROP TABLE IF EXISTS {table}")
    connection.execute(
        f"CREATE TABLE {table} (id integer PRIMARY KEY, embedding {column_type}({dimension}))"
    )
    connection.execute(
        f"""
        INSERT INTO {table} (id, embedding)
        SELECT g, (
            SELECT ('[' || string_agg(random()::text, ',') || ']')::{column_type}
            FROM generate_series(1, {dimension}) AS d
        )
        FROM generate_series(1, {ROWS}) AS g
        """
    )
    connection.commit()


def time_index(connection: psycopg.Connection, table: str, ops: str) -> float:
    started = time.perf_counter()
    connection.execute(f"CREATE INDEX ON {table} USING hnsw (embedding {ops})")
    connection.commit()
    return time.perf_counter() - started


def time_queries(
    connection: psycopg.Connection, table: str, dimension: int, column_type: str, ops: str
) -> float:
    probe = vector_literal([0.5] * dimension)
    # Warm the index so the measurement is query cost, not first-touch cost.
    connection.execute(
        f"SELECT id FROM {table} ORDER BY embedding <=> '{probe}'::{column_type} LIMIT {TOP_K}"
    ).fetchall()
    started = time.perf_counter()
    for _ in range(QUERIES):
        connection.execute(
            f"SELECT id FROM {table} ORDER BY embedding <=> '{probe}'::{column_type} LIMIT {TOP_K}"
        ).fetchall()
    elapsed = time.perf_counter() - started
    connection.commit()
    return elapsed / QUERIES * 1000


def table_size_mb(connection: psycopg.Connection, table: str) -> float:
    row = connection.execute(
        "SELECT pg_total_relation_size(%s) / 1024.0 / 1024.0", (table,)
    ).fetchone()
    return float(row[0])


def main() -> None:
    print(f"rows={ROWS} queries={QUERIES} top_k={TOP_K}\n")
    print(f"{'variant':<26} {'size MB':>9} {'index s':>9} {'query ms':>10}")
    print("-" * 58)

    with psycopg.connect(DSN, autocommit=False) as connection:
        connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
        connection.commit()

        variants = [
            ("vector(1536) + HNSW", "dim1536", 1536, "vector", "vector_cosine_ops"),
            ("halfvec(1536) + HNSW", "dim1536h", 1536, "halfvec", "halfvec_cosine_ops"),
            (
                "halfvec(3072) + HNSW",
                "dim3072h",
                3072,
                "halfvec",
                "halfvec_cosine_ops",
            ),
            ("vector(3072) + HNSW", "dim3072", 3072, "vector", "vector_cosine_ops"),
        ]
        for label, table, dimension, column_type, ops in variants:
            try:
                make_ready(connection, table, dimension, column_type)
                index_seconds = time_index(connection, table, ops)
                query_ms = time_queries(connection, table, dimension, column_type, ops)
                size = table_size_mb(connection, table)
                print(
                    f"{label:<26} {size:>9.1f} {index_seconds:>9.1f} {query_ms:>10.2f}",
                    flush=True,
                )
            except Exception as exc:
                print(f"{label:<26} FAILED: {str(exc).splitlines()[0][:60]}", flush=True)
            finally:
                connection.rollback()

        # Leave the database clean: this probe is exploratory, not a migration.
        for _, table, _, _, _ in variants:
            connection.execute(f"DROP TABLE IF EXISTS {table}")
        connection.commit()


if __name__ == "__main__":
    main()
