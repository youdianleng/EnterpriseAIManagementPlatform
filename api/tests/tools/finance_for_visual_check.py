"""Grant the demo `empleado` account the finance role, so the visual check can drive it.

Run inside the api container:

    docker compose exec -T api python tests/tools/finance_for_visual_check.py grant
    docker compose exec -T api python tests/tools/finance_for_visual_check.py revoke

**Not a migration and not part of the suite.** It exists so the browser check can sign in as
a finance officer on a development database whose seed does not create one (the seeded
logins are admin, hr and two employees — `app/seed.py` `DEMO_LOGINS`). It is reversible with
`revoke`, it touches one column of one row, and it says which account it touched on the way
out. Deleting it would be fine; it is kept because the next ticket that needs a finance
session in the browser will otherwise write it again.
"""

import asyncio
import json
import sys

from sqlalchemy import text

from app.config import get_settings
from app.core.security import hash_password
from app.db import build_engine

USERNAME = "empleado"
PASSWORD = "Finanzas!2026"
FINANCE = "finance"


async def main(action: str) -> None:
    settings = get_settings()
    # The **owner** connection, not the runtime one: changing an account's roles is not a
    # request, and the runtime role's policies would refuse it — which is the point of them.
    engine = build_engine(settings, settings.database_url)
    async with engine.begin() as connection:
        row = (
            await connection.execute(
                text("SELECT id, roles FROM users WHERE username = :username"),
                {"username": USERNAME},
            )
        ).first()
        if row is None:
            raise SystemExit(f"no account {USERNAME!r} on this database")
        user_id, roles = row
        current = set(roles or [])
        if action == "grant":
            current.add(FINANCE)
        elif action == "revoke":
            current.discard(FINANCE)
        else:
            raise SystemExit(f"usage: {sys.argv[0]} grant|revoke")
        await connection.execute(
            text(
                "UPDATE users SET roles = CAST(:roles AS jsonb), password_hash = :hash, "
                "must_change_password = false WHERE id = :id"
            ),
            {
                "roles": json.dumps(sorted(current)),
                "hash": hash_password(PASSWORD),
                "id": user_id,
            },
        )
        if action == "grant":
            seeded = await _seed_salaries(connection)
            print(f"      {seeded} salary record(s) ensured for the demo staff")
    await engine.dispose()
    print(f"{action}: {USERNAME} now holds {sorted(current)}; password set to {PASSWORD!r}")


async def _seed_salaries(connection) -> int:
    """Give the seeded staff a salary record in force, so the missing list has content.

    Ticket 43's archive is what the missing list is derived from, and a development database
    seeded before that ticket has none — so the screen would honestly say "nobody had a
    salary in force", and a browser check would be looking at an empty state it did not mean
    to test. An opening record per employee, in force from 2020 and with no end date; the
    archive's own exclusion constraint means running this twice adds nothing.
    """
    rows = (
        await connection.execute(
            text(
                """
                INSERT INTO salary_records (id, employee_id, effective_from, base_salary,
                                            currency, pay_period, change_reason_type,
                                            change_reason)
                SELECT gen_random_uuid(), e.id, DATE '2020-01-01', NUMERIC '30000.00',
                       'EUR', 'monthly', 'initial', 'Alta en la empresa (datos de demo)'
                  FROM employees e
                 WHERE e.termination_date IS NULL
                   AND NOT EXISTS (
                       SELECT 1 FROM salary_records s WHERE s.employee_id = e.id
                   )
                RETURNING id
                """
            )
        )
    ).all()
    return len(rows)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "grant"))
