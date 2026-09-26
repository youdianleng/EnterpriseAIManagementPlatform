"""What row-level security costs, measured rather than guessed.

    docker compose exec -T api python /app/tests/tools/probe_rls_cost.py

Two measurements, because they answer different questions:

* **Per row.** A 50 000-row scratch table with the same predicate as a policy,
  scanned with and without it. This is the number that scales.
* **Per statement.** The real table, scanned 2000 times inside one PL/pgSQL loop.
  At this size the policy's fixed cost per statement dominates, and that is the
  number a request pays once per query.

Neither number is interesting on its own — an authorisation check that costs
microseconds is not why this design exists. It exists so that a forgotten filter
returns no rows instead of every row, and the cost of that is worth knowing rather
than assuming.
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import build_engine  # noqa: E402
from tests.tools.support import check, finish  # noqa: E402

ROWS = 50_000
SCANS = 2_000

#: The predicate the real policy uses, so the measurement is of this design and
#: not of a generic "policy overhead".
PREDICATE = """
    app_setting('app.is_privileged')::boolean
    OR owner_employee_id = app_setting('app.current_employee_id')::uuid
    OR app_setting_array('app.current_roles') && ARRAY['admin', 'hr']
"""


async def execution_time(session, statement: str) -> float:
    """The planner's own number, in milliseconds."""
    rows = (await session.execute(text(f"EXPLAIN (ANALYZE, TIMING OFF) {statement}"))).all()
    for row in rows:
        line = row[0].strip()
        if line.startswith("Execution Time:"):
            return float(line.split(":", 1)[1].strip().split()[0])
    raise AssertionError(f"no execution time in the plan for {statement}")


async def per_row(session) -> tuple[float, float]:
    await session.execute(
        text(
            """
            CREATE TABLE rls_bench (id serial PRIMARY KEY, owner_employee_id uuid,
                                    department_id uuid, clearance_level text, payload text)
            """
        )
    )
    await session.execute(
        text(
            """
            INSERT INTO rls_bench (owner_employee_id, department_id, clearance_level, payload)
            SELECT gen_random_uuid(), gen_random_uuid(),
                   (ARRAY['low','medium','high'])[1 + (i % 3)], repeat('x', 40)
            FROM generate_series(1, :rows) i
            """
        ),
        {"rows": ROWS},
    )
    await session.execute(text("ANALYZE rls_bench"))

    without = await execution_time(session, "SELECT count(*) FROM rls_bench")

    await session.execute(text("SELECT set_config('app.current_roles', '{admin}', true)"))
    await session.execute(
        text("SELECT set_config('app.current_employee_id', :me, true)"),
        {"me": "00000000-0000-0000-0000-000000000099"},
    )
    await session.execute(text("ALTER TABLE rls_bench ENABLE ROW LEVEL SECURITY"))
    # FORCE, because the owner is exempt from its own policies and this session is
    # the owner: without it there would be nothing to measure.
    await session.execute(text("ALTER TABLE rls_bench FORCE ROW LEVEL SECURITY"))
    await session.execute(
        text(f"CREATE POLICY rls_bench_read ON rls_bench FOR SELECT USING ({PREDICATE})")
    )
    with_policy = await execution_time(session, "SELECT count(*) FROM rls_bench")
    return without, with_policy


async def per_statement(session) -> tuple[float, float]:
    """2000 scans, timed around the loop, for each role."""
    loop = text(
        f"""
        DO $$
        DECLARE i int; n bigint;
        BEGIN
            FOR i IN 1..{SCANS} LOOP
                SELECT count(*) INTO n FROM employee_private;
            END LOOP;
        END $$
        """
    )
    started = time.perf_counter()
    await session.execute(loop)
    as_owner = time.perf_counter() - started

    await session.execute(text("SET ROLE eam_app"))
    try:
        await session.execute(text("SELECT set_config('app.current_roles', '{admin}', true)"))
        started = time.perf_counter()
        await session.execute(loop)
        as_app = time.perf_counter() - started
    finally:
        await session.execute(text("RESET ROLE"))
    return as_owner * 1000, as_app * 1000


async def main() -> None:
    settings = get_settings()
    engine = build_engine(settings, settings.database_url)
    try:
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as session:
            # Not asserted on: this probe measures the cost of the rule, not the
            # contents of the database, and the other probes leave it empty when
            # they clean up after themselves.
            rows = await session.scalar(text("SELECT count(*) FROM employee_private"))

            without, with_policy = await per_row(session)
            await session.rollback()

            owner_ms, app_ms = await per_statement(session)

        print()
        print(f"Per row, {ROWS} rows, same predicate as the policy")
        print(f"  no policy   {without:8.3f} ms")
        print(f"  with policy {with_policy:8.3f} ms")
        delta = with_policy - without
        print(f"  delta       {delta:8.3f} ms  ({delta / without * 100:.1f}%)")
        print()
        print(f"Per statement, {SCANS} scans of employee_private ({rows} rows present)")
        print(f"  as owner    {owner_ms:8.3f} ms total  ({owner_ms / SCANS * 1000:.1f} µs each)")
        print(f"  as eam_app  {app_ms:8.3f} ms total  ({app_ms / SCANS * 1000:.1f} µs each)")
        print(f"  delta       {app_ms - owner_ms:8.3f} ms total  "
              f"({(app_ms - owner_ms) / SCANS * 1000:.1f} µs each)")
        print()

        # The claims worth failing on: the policy is not free, and it is not
        # expensive either. A regression to "milliseconds per row" would matter;
        # noise at this scale would not.
        check("the policy costs less than a millisecond per 10 000 rows",
              delta < 1.0, f"{delta:.3f} ms for {ROWS} rows")
        check("and less than a millisecond per statement",
              (app_ms - owner_ms) / SCANS < 1.0,
              f"{(app_ms - owner_ms) / SCANS * 1000:.1f} µs each")
    finally:
        await engine.dispose()

    print()
    finish()


if __name__ == "__main__":
    asyncio.run(main())
