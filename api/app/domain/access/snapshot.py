"""Building and caching the permission snapshot.

Two sources feed a snapshot: the account (roles live in the users table in
ticket 08b; until then every account is an employee) and the employee's active
assignments. Both can change, and both must invalidate the cache *at the moment
they change*.

**Why a version rather than a delete.** A snapshot is keyed by the inputs that
produce it. The account's `session_epoch` already moves on deactivation and
password change, and the organisation module already publishes a structure
version for department edits. Combining them into the cache key means a stale
entry is not deleted, it is simply never looked up again — which cannot fail
half-way the way a delete-then-rebuild can.

TTL is the backstop, not the mechanism. If an invalidation is ever missed, the
entry expires; but nothing depends on that happening on time.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import ORG_TREE_VERSION_KEY, get_redis
from app.domain.access.principal import SYSTEM_ROLES, Principal
from app.logging import get_logger

logger = get_logger(__name__)

#: Backstop only. Correctness comes from the version in the key.
SNAPSHOT_TTL_SECONDS = 300

SNAPSHOT_KEY = "perm:user:{user_id}:{version}"

#: Clearance, most permissive first.
#:
#: This module reads department rows and turns a rank back into a name, so it
#: needs the ordering the kernel owns without importing the kernel — that would
#: make the dependency circular for no gain. `tests/test_access_kernel.py`
#: asserts the two agree, in both directions.
#:
#: Spelled out rather than derived, because an off-by-one here decides how much
#: a person can see: `0 or 2` is 2 in Python, so a lookup that passed the rank
#: through a truthiness test turned "high" into "low".
_CLEARANCE_BY_RANK = ("high", "medium", "low")
_CLEARANCE_RANK = {name: rank for rank, name in enumerate(reversed(_CLEARANCE_BY_RANK))}


def highest_clearance(*levels: str) -> str:
    """The most permissive of the levels given (D12: clearances take the highest).

    Unknown values are ignored rather than treated as "low": a typo in a
    department row must not silently clear somebody for everything, and it must
    not silently revoke them either — the column is a CHECK constraint, so an
    unknown value reaching here means the schema was bypassed.
    """
    known = [level for level in levels if level in _CLEARANCE_RANK]
    if not known:
        return "low"
    return max(known, key=_CLEARANCE_RANK.__getitem__)


@dataclass(slots=True, frozen=True)
class AccountFacts:
    """The account-side inputs to a snapshot."""

    user_id: UUID
    employee_id: UUID
    username: str
    is_active: bool
    must_change_password: bool
    session_epoch: int
    status: str
    roles: frozenset[str] = frozenset({"employee"})
    #: The stored value from `users.clearance_level` — set when the person was
    #: hired and raisable afterwards. The effective clearance takes the higher of
    #: this and what the departments grant.
    clearance_level: str = "low"


class PrincipalBuilder:
    """Assembles a `Principal` from the database.

    Deliberately the only place that knows how a snapshot is derived. Everything
    downstream receives a `Principal` and never asks where it came from.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load_account(self, user_id: UUID) -> AccountFacts | None:
        row = (
            await self._session.execute(
                text(
                    """
                    SELECT u.id, u.employee_id, u.username, u.is_active,
                           u.must_change_password, u.session_epoch, e.status, u.roles,
                           u.clearance_level
                    FROM users u
                    JOIN employees e ON e.id = u.employee_id
                    WHERE u.id = :user_id
                    """
                ),
                {"user_id": user_id},
            )
        ).first()
        if row is None:
            return None
        return AccountFacts(
            user_id=row[0],
            employee_id=row[1],
            username=row[2],
            is_active=row[3],
            must_change_password=row[4],
            session_epoch=row[5],
            status=row[6],
            roles=frozenset(row[7] or ["employee"]),
            clearance_level=row[8] or "low",
        )

    async def assignment_facts(
        self, employee_id: UUID
    ) -> tuple[frozenset[UUID], UUID | None, bool, frozenset[UUID]]:
        """Departments (descendants included), primary department, managerial
        flag, and the employees who report to this person.

        **The reports direction is one-way, and that is a security property rather
        than a detail.** A person's `manager_employee_id` names their approver; the
        reports set is built from the *other* side — the assignments that name this
        person. Folding a caller's own approver in, which this method used to do,
        put their manager into the set the kernel's `MANAGER_OF_SUBJECT` clause
        tests: it let a manager read the hours, leave and overtime of their own
        boss, and it put the boss into the daily digest's audience. `test_permission_
        snapshot.py` pins both directions.
        """
        rows = (
            await self._session.execute(
                text(
                    """
                    WITH mine AS (
                        SELECT a.department_id, a.is_primary, d.path
                        FROM employee_assignments a
                        JOIN departments d ON d.id = a.department_id
                        WHERE a.employee_id = :employee_id
                          AND a.end_date IS NULL
                    )
                    SELECT m.department_id, m.is_primary, d.id AS descendant_id
                    FROM mine m
                    LEFT JOIN departments d ON d.path <@ m.path
                    """
                ),
                {"employee_id": employee_id},
            )
        ).all()

        departments: set[UUID] = set()
        primary: UUID | None = None

        for row in rows:
            departments.add(row[0])
            if row[2] is not None:
                departments.add(row[2])
            if row[1]:
                primary = row[0]

        # Managerial positions are a property of the position, not of the person.
        managerial = await self._session.scalar(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM employee_assignments a
                    JOIN job_positions p ON p.id = a.job_position_id
                    WHERE a.employee_id = :employee_id
                      AND a.end_date IS NULL
                      AND p.is_managerial
                )
                """
            ),
            {"employee_id": employee_id},
        )
        is_manager = bool(managerial)

        # Reports are discovered from the other side: whoever names this person
        # as their approver.
        reports = (
            await self._session.execute(
                text(
                    """
                    SELECT DISTINCT a.employee_id
                    FROM employee_assignments a
                    WHERE a.end_date IS NULL
                      AND a.manager_employee_id = :employee_id
                    """
                ),
                {"employee_id": employee_id},
            )
        ).scalars()
        reports_set = {row for row in reports}

        return frozenset(departments), primary, is_manager, frozenset(reports_set)

    async def build(self, account: AccountFacts) -> Principal:
        departments, primary, is_manager, reports = await self.assignment_facts(
            account.employee_id
        )
        return Principal(
            user_id=account.user_id,
            employee_id=account.employee_id,
            username=account.username,
            # Roles come from the account, with `manager` added when a held
            # position is marked managerial — which is where the requirement says
            # the manager role is derived from.
            roles=self._effective_roles(account.roles, is_manager),
            # Two sources, and the higher wins: the departments someone works in
            # decide what their work may contain, and the stored value is how one
            # person is raised above that for a specific reason. Taking the lower
            # would make the stored value unable to raise anybody, and taking the
            # stored value alone would make a department-wide change need a data
            # migration to reach the people in it.
            clearance_level=highest_clearance(
                account.clearance_level,
                await self._clearance_for(account.employee_id, departments),
            ),
            department_ids=departments,
            primary_department_id=primary,
            is_manager=is_manager,
            reports_employee_ids=reports,
            version=account.session_epoch,
        )

    @staticmethod
    def _effective_roles(roles: frozenset[str], is_manager: bool) -> frozenset[str]:
        effective = set(roles) | {"employee"}
        if is_manager:
            effective.add("manager")
        return frozenset(effective & SYSTEM_ROLES)

    async def _clearance_for(
        self, employee_id: UUID, departments: frozenset[UUID]
    ) -> str:
        """The highest clearance among the departments the person sits in.

        Clearance is a property of where someone works, so it is derived rather
        than stored per person: a stored copy would have to be kept in step with
        every department edit, and would eventually disagree.
        """
        if not departments:
            return "low"
        level = await self._session.scalar(
            text(
                """
                SELECT COALESCE(
                    MIN(
                        CASE clearance_level
                            WHEN 'high' THEN 0
                            WHEN 'medium' THEN 1
                            ELSE 2
                        END
                    ),
                    2
                )
                FROM departments
                WHERE id = ANY(:department_ids)
                """
            ),
            {"department_ids": list(departments)},
        )
        # MIN picks the *most permissive* department, which is rank 0: the
        # lowest number is the highest clearance. `level or 2` would read that
        # 0 as "unset" and hand back "low" to exactly the people who should see
        # the most.
        rank = 2 if level is None else int(level)
        return _CLEARANCE_BY_RANK[rank] if 0 <= rank < len(_CLEARANCE_BY_RANK) else "low"

    async def structure_version(self) -> int:
        """The organisation module's stamp, part of every cache key."""
        try:
            value = await get_redis().get(ORG_TREE_VERSION_KEY)
        except Exception:
            return 0
        return int(value or 0)

    async def assignment_version(self, employee_id: UUID) -> int:
        """A stamp that changes whenever the snapshot's inputs change.

        Derived from the assignment rows rather than maintained by a counter,
        because a counter has to be incremented by every writer — and the one
        that forgets is the bug. Anything that alters departments, the primary
        position, the managerial flag or reports changes this value, so a stale
        snapshot is never looked up again.

        Department rows themselves are covered by `structure_version`, which the
        organisation module bumps on every write — including a clearance edit,
        which is what a person's clearance is derived from. The clearance of a
        *descendant* department is deliberately not re-read here: it arrives with
        the same stamp, and re-reading every subtree on every request would put a
        correlated aggregate on the hot path to guard against a write that does
        not exist yet.

        `hashtext` over the concatenated facts is cheap and entirely adequate: it
        only has to differ, not be collision-free in a security sense, because a
        collision would mean serving a snapshot built from identical inputs.

        `d.path::text` is cast explicitly. Without the cast, `||` resolves to
        ltree's own concatenation operator and PostgreSQL tries to read `':'` as
        a label, failing with "ltree syntax error at character 1" — a message
        that points at the data rather than at the operator that caused it.
        """
        value = await self._session.scalar(
            text(
                """
                SELECT COALESCE(
                    hashtext(
                        string_agg(
                            a.id::text || ':' || a.department_id::text
                            || ':' || a.is_primary::text
                            || ':' || COALESCE(a.manager_employee_id::text, '-')
                            || ':' || d.path::text
                            || ':' || d.clearance_level
                            || ':' || p.is_managerial::text,
                            '|' ORDER BY a.id
                        )
                    ),
                    0
                )
                FROM employee_assignments a
                JOIN departments d ON d.id = a.department_id
                JOIN job_positions p ON p.id = a.job_position_id
                WHERE a.employee_id = :employee_id
                  AND a.end_date IS NULL
                """
            ),
            {"employee_id": employee_id},
        )
        return int(value or 0)


async def resolve_principal(session: AsyncSession, user_id: UUID) -> Principal | None:
    """Load a principal, preferring a cached snapshot.

    The cache key carries every input the snapshot was built from, so a changed
    account, a granted role, a changed assignment or a changed organisation
    misses and rebuilds. The earlier version keyed only on the account and the
    structure version, which meant moving an employee between departments kept
    serving the old snapshot — caught by
    `test_moving_an_employee_between_departments_takes_effect_at_once`.
    """
    builder = PrincipalBuilder(session)
    account = await builder.load_account(user_id)
    if account is None or not account.is_active:
        return None

    # Roles and the stored clearance are read from the row that was just loaded,
    # so they are part of the key for free. Without them, granting a role or
    # raising somebody's clearance would keep serving the old answer until the TTL
    # expired — the failure this key exists to prevent.
    roles_stamp = ".".join(sorted(account.roles))
    version = (
        f"{account.session_epoch}.{roles_stamp}.{account.clearance_level}."
        f"{await builder.assignment_version(account.employee_id)}."
        f"{await builder.structure_version()}"
    )
    key = SNAPSHOT_KEY.format(user_id=user_id, version=version)

    cached = await _read_cached(key)
    if cached is not None:
        return cached

    principal = await builder.build(account)
    await _write_cached(key, principal, user_id)
    return principal


def _encode(principal: Principal) -> str:
    import json

    return json.dumps(
        {
            "user_id": str(principal.user_id),
            "employee_id": str(principal.employee_id),
            "username": principal.username,
            "roles": sorted(principal.roles),
            "clearance_level": principal.clearance_level,
            "department_ids": sorted(str(value) for value in principal.department_ids),
            "primary_department_id": (
                str(principal.primary_department_id) if principal.primary_department_id else None
            ),
            "is_manager": principal.is_manager,
            "reports_employee_ids": sorted(
                str(value) for value in principal.reports_employee_ids
            ),
            "version": principal.version,
        }
    )


def _decode(raw: str) -> Principal:
    import json

    payload = json.loads(raw)
    return Principal(
        user_id=UUID(payload["user_id"]),
        employee_id=UUID(payload["employee_id"]),
        username=payload["username"],
        roles=frozenset(payload["roles"]),
        clearance_level=payload["clearance_level"],
        department_ids=frozenset(UUID(value) for value in payload["department_ids"]),
        primary_department_id=(
            UUID(payload["primary_department_id"])
            if payload.get("primary_department_id")
            else None
        ),
        is_manager=payload["is_manager"],
        reports_employee_ids=frozenset(
            UUID(value) for value in payload["reports_employee_ids"]
        ),
        version=payload["version"],
    )


async def _read_cached(key: str) -> Principal | None:
    try:
        raw = await get_redis().get(key)
    except Exception:
        # A cache that cannot be read means a rebuild, never a refusal: the
        # database is authoritative and is about to be asked anyway.
        return None
    if not raw:
        return None
    try:
        return _decode(raw)
    except Exception:
        logger.warning("permission_snapshot_undecodable", key=key)
        return None


async def _write_cached(key: str, principal: Principal, user_id: UUID) -> None:
    try:
        await get_redis().set(key, _encode(principal), ex=SNAPSHOT_TTL_SECONDS)
    except Exception:
        return


async def invalidate_user(user_id: UUID) -> None:
    """Drop every cached snapshot for one user.

    Deleting by pattern needs a scan, which is why the version is part of the key
    instead: a changed input produces a different key and the old entry is simply
    never read again, expiring on its own. This function exists for the cases
    where the *inputs* are not visible to the key — a role change made directly
    in the database, for instance — and callers who want the old entry gone now.
    """
    try:
        client = get_redis()
        async for key in client.scan_iter(match=f"perm:user:{user_id}:*", count=100):
            await client.delete(key)
    except Exception:
        return


def to_viewer_context(principal: Principal):  # noqa: ANN201 - ViewerContext
    """Adapt a principal to the employee module's field-visibility input.

    The employee module predates the kernel and takes a `ViewerContext`. Rather
    than rewrite its rule — which is tested exhaustively and states the
    requirement directly — the principal is translated at the boundary. When the
    two shapes merge this function disappears, and its test with it.
    """
    from app.domain.employee.visibility import ViewerContext

    return ViewerContext(
        employee_id=principal.employee_id,
        roles=principal.roles,
        clearance_level=principal.clearance_level,
        department_ids=principal.department_ids,
    )


__all__ = [
    "SNAPSHOT_KEY",
    "SNAPSHOT_TTL_SECONDS",
    "AccountFacts",
    "PrincipalBuilder",
    "highest_clearance",
    "invalidate_user",
    "resolve_principal",
    "to_viewer_context",
]
