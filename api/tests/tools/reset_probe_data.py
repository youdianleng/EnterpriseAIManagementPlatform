"""Remove leftover probe rows.

Probe scripts create real rows on purpose (they exercise real HTTP), and an
early version of two of them did not clean up after itself. Every foreign key in
the organisation schema is RESTRICT, so deletes have to run outermost-first.

    docker compose exec -T api python /app/tests/tools/reset_probe_data.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import build_engine  # noqa: E402

# Order matters: each table is referenced by the one above it.
DELETES = [
    "DELETE FROM employee_assignments",
    "DELETE FROM employee_private",
    "DELETE FROM employees",
    "DELETE FROM job_positions",
    "UPDATE departments SET manager_employee_id = NULL",
    "DELETE FROM departments",
]


async def main() -> None:
    engine = build_engine(get_settings())
    try:
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as session:
            for statement in DELETES:
                result = await session.execute(text(statement))
                print(f"{statement:<52} removed {result.rowcount}")
            await session.commit()

            for table in ("departments", "employees", "job_positions", "employee_assignments"):
                count = await session.scalar(text(f"SELECT count(*) FROM {table}"))
                print(f"{table:<52} now {count}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
