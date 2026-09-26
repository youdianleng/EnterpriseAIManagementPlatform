"""Check the role constraint before trusting it.

A CHECK expression that is syntactically valid but semantically wrong is worse
than none: it looks like a guarantee. This drives it against real values,
including the ones it must refuse.

    docker compose exec -T api python /app/tests/tools/probe_role_constraint.py
"""

import asyncio
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db import build_engine  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, observed: object = "") -> None:
    # Truncated for display only. An earlier version truncated before matching and
    # so reported a failure for a message whose constraint name fell past the cut.
    print(f"[{'ok  ' if condition else 'FAIL'}] {label}: {str(observed)[:160]}")
    if not condition:
        failures.append(label)


async def main() -> None:
    settings = get_settings()
    # The application's own database, not the test one: probes inspect the stack
    # that is running, and migrations are applied to that database. Reading
    # `test_database_url` here would inspect a schema pytest built separately.
    engine = build_engine(settings)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def insert(roles: str) -> tuple[bool, str]:
        employee_id = uuid4()
        user_id = uuid4()
        try:
            async with factory() as session:
                await session.execute(
                    text(
                        """
                        INSERT INTO employees (id, first_name, last_name, email,
                                               hire_date, status)
                        VALUES (:id, 'A', 'B', :email, '2024-01-15', 'active')
                        """
                    ),
                    {"id": employee_id, "email": f"roles{uuid4().hex[:8]}@empresa.es"},
                )
                await session.execute(
                    text(
                        """
                        INSERT INTO users (id, employee_id, username, password_hash,
                                           must_change_password, is_active,
                                           session_epoch, roles)
                        VALUES (:id, :employee_id, :username, :hash, false, true, 1,
                                CAST(:roles AS jsonb))
                        """
                    ),
                    {
                        "id": user_id,
                        "employee_id": employee_id,
                        "username": f"role{uuid4().hex[:8]}",
                        "hash": hash_password("Str0ng!Password1"),
                        "roles": roles,
                    },
                )
                await session.commit()
            return True, ""
        except Exception as exc:
            return False, str(exc).splitlines()[0]

    allowed, detail = await insert('["employee", "hr"]')
    check("a set of known roles is accepted", allowed, detail)
    if not allowed:
        print("\nthe schema under test is not ready;")
        print("run `alembic upgrade head` against this database first.")

    # Each refusal must be *about roles*, not an unrelated error. Asserting only
    # "it failed" would pass for a missing column — which is how this probe first
    # reported several false successes.
    #
    # The trigger raises for unknown values and names them; the CHECK catches the
    # empty array, which is a shape problem rather than a value problem.
    for roles, expected in (
        ('["wizard"]', "unknown role"),
        ('["employee", "wizard"]', "unknown role"),
        ('["EMPLOYEE"]', "unknown role"),
        ("[]", "ck_users_roles_not_empty"),
    ):
        refused, detail = await insert(roles)
        check(
            f"{roles} is refused mentioning {expected!r}",
            (not refused) and expected in detail,
            detail,
        )

    async with factory() as session:
        await session.execute(text("DELETE FROM users"))
        await session.execute(text("DELETE FROM employees"))
        await session.commit()
    await engine.dispose()

    print()
    print("FAIL" if failures else "ALL CHECKS PASSED")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())
