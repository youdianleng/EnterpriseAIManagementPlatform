"""The role-to-permission mapping, published as data.

`permissions.py` holds the rules, and it holds them in one readable table rather
than in branches. Ticket 08b asks for that mapping to be *queryable* — by an
auditor, by the interface that explains what a role may do, and by whoever joins
the project next — and the answer is these two tables.

**They are a projection of the code, not a second source of truth.** The sync below
runs at application start and rewrites them from `RULES`, so they cannot drift and
a decision cannot be changed by editing a row. That ordering is deliberate: a
permission decided by querying the database would be slower on every request, and
a permission change that is a row edit alone is a change nobody reviewed.

The runtime role has SELECT and nothing else on both tables (migration 0008), so
the published form is genuinely read-only to the application. The sync runs on the
owner connection, like migrations and the seed: publishing the catalogue is an
administrative act, not a request.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.access.principal import SYSTEM_ROLES
from app.logging import get_logger

logger = get_logger(__name__)

#: One line per role, for the interface that lists them. Kept next to the sync so a
#: new role cannot be published without somebody writing down what it is for.
ROLE_DESCRIPTIONS: dict[str, str] = {
    "admin": "System administration: accounts, structure and clearance configuration.",
    "hr": "Personnel files, attendance, leave and timesheets; the second approver.",
    "finance": "Payroll records, payslip batches and the overtime export.",
    "it": "Account recovery and forced sign-out. No business data.",
    "compliance": "The read-only audit trail and the data-protection register.",
    "manager": "Derived from a managerial position: their reports, the first approver.",
    "employee": (
        "Their own data, their department's contact list, and clearance-bounded search."
    ),
}

#: Which roles the system derives rather than grants. A managerial position adds
#: `manager` to the permission snapshot; an administrator may also grant it.
DERIVED_ROLES: frozenset[str] = frozenset({"manager"})


async def sync_role_catalogue(session: AsyncSession) -> int:
    """Rewrite `roles` and `role_permissions` from the catalogue in code.

    Returns the number of permission rows published. Rewriting rather than
    merging, so a permission *removed* from the code disappears from the table as
    well — the failure mode of a merge is a table that only ever grows, which is
    exactly the drift this exists to prevent.
    """
    from app.domain.access.permissions import RULES

    known = {role: ROLE_DESCRIPTIONS.get(role, role) for role in sorted(SYSTEM_ROLES)}
    for role in RULES.values():
        for name in role.roles:
            known.setdefault(name, name)

    await session.execute(text("DELETE FROM role_permissions"))
    await session.execute(text("DELETE FROM roles"))
    for name, description in known.items():
        await session.execute(
            text(
                """
                INSERT INTO roles (name, description, is_derived)
                VALUES (:name, :description, :derived)
                """
            ),
            {
                "name": name,
                "description": description,
                "derived": name in DERIVED_ROLES,
            },
        )

    published = 0
    for action, rule in RULES.items():
        if rule.public:
            # A public action belongs to nobody's role; publishing it against a
            # role would state the opposite of what the catalogue says.
            continue
        for role in sorted(rule.roles):
            await session.execute(
                text(
                    """
                    INSERT INTO role_permissions (role, action)
                    VALUES (:role, :action)
                    ON CONFLICT DO NOTHING
                    """
                ),
                {"role": role, "action": str(action)},
            )
            published += 1

    await session.commit()
    logger.info("role_catalogue_published", roles=len(known), permissions=published)
    return published


async def published_catalogue(session: AsyncSession) -> dict[str, dict]:
    """What the database currently says, for the read surface and for the test."""
    roles = (
        await session.execute(
            text("SELECT name, description, is_derived FROM roles ORDER BY name")
        )
    ).all()
    permissions = (
        await session.execute(
            text("SELECT role, action FROM role_permissions ORDER BY role, action")
        )
    ).all()

    by_role: dict[str, list[str]] = {row[0]: [] for row in roles}
    for role, action in permissions:
        by_role.setdefault(role, []).append(action)

    return {
        row[0]: {
            "name": row[0],
            "description": row[1],
            "is_derived": row[2],
            "actions": by_role.get(row[0], []),
        }
        for row in roles
    }


__all__ = [
    "DERIVED_ROLES",
    "ROLE_DESCRIPTIONS",
    "published_catalogue",
    "sync_role_catalogue",
]
