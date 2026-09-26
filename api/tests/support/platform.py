"""A running-platform fixture for API tests.

The organisation and employee test modules drive real HTTP endpoints, and those
endpoints now read committed rows through their own sessions. A per-test
transaction that never commits is invisible to them, so those modules commit and
clean up instead.

This is the right shape for them regardless of authentication: they assert what
the API returns, not what a session holds, and a committed row is what the API
actually sees in production.
"""

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.security import hash_password
from app.db import build_engine
from app.main import app

#: Deleted in this order: every foreign key in the organisation schema is
#: RESTRICT, so referencing rows go first.
CLEANUP_ORDER = (
    "DELETE FROM audit_log",
    # A personnel change references its employee with ON DELETE RESTRICT, so it
    # has to go before them. Its approval request is a plain column, not a foreign
    # key, so the order against the engine's tables does not matter.
    "DELETE FROM personnel_changes",
    # Decisions first: they reference their request with ON DELETE RESTRICT, so
    # the request cannot go until they have.
    "DELETE FROM approval_decisions",
    "DELETE FROM approval_steps",
    "DELETE FROM approval_requests",
    # Deliveries reference their notification, which references nothing else.
    "DELETE FROM notification_deliveries",
    "DELETE FROM notifications",
    "DELETE FROM users",
    # After users, before the rest: published catalogue rows reference each other.
    "DELETE FROM role_permissions",
    "DELETE FROM roles",
    "DELETE FROM employee_assignments",
    "DELETE FROM employee_private",
    "DELETE FROM employees",
    "DELETE FROM job_positions",
    "UPDATE departments SET manager_employee_id = NULL",
    "DELETE FROM departments",
)


@dataclass
class Platform:
    """A committing database, an HTTP client, and the accounts signed in as."""

    settings: object
    factory: async_sessionmaker
    client: AsyncClient
    accounts: dict = field(default_factory=dict)

    async def wipe(self) -> None:
        async with self.factory() as session:
            for statement in CLEANUP_ORDER:
                await session.execute(text(statement))
            await session.commit()

    # --- data --------------------------------------------------------------

    async def sql(self, statement: str, params: dict | None = None):
        async with self.factory() as session:
            result = await session.execute(text(statement), params or {})
            rows = result.fetchall() if result.returns_rows else []
            await session.commit()
            return rows

    async def scalar(self, statement: str, params: dict | None = None):
        rows = await self.sql(statement, params)
        return rows[0][0] if rows else None

    async def department(self, code: str, **overrides: object) -> str:
        """Create a department through the API, as an administrator."""
        actor = await self.account(roles=("admin",))
        response = await actor.call(
            "POST",
            "/api/v1/departments",
            json={"code": code, "name_es": f"{code} es", "name_en": f"{code} en", **overrides},
        )
        assert response.status_code == 201, response.text
        return response.json()["id"]

    async def position(self, department_id: str, code: str, **overrides: object) -> str:
        actor = await self.account(roles=("admin",))
        response = await actor.call(
            "POST",
            "/api/v1/positions",
            json={
                "code": code,
                "title_es": f"{code} es",
                "title_en": f"{code} en",
                "department_id": department_id,
                **overrides,
            },
        )
        assert response.status_code == 201, response.text
        return response.json()["id"]

    async def employee(self, email: str | None = None, **overrides: object) -> str:
        actor = await self.account(roles=("admin",))
        address = email or f"emp{uuid.uuid4().hex[:8]}@empresa.es"
        response = await actor.call(
            "POST",
            "/api/v1/employees",
            json={
                "first_name": "Ana",
                "last_name": "Martín",
                "email": address,
                "hire_date": "2024-01-15",
                **overrides,
            },
        )
        assert response.status_code == 201, response.text
        return response.json()["id"]

    async def assign(self, employee_id: str, department_id: str, position_id: str, **extra):
        actor = await self.account(roles=("admin",))
        response = await actor.call(
            "POST",
            f"/api/v1/employees/{employee_id}/assignments",
            json={
                "department_id": department_id,
                "job_position_id": position_id,
                "start_date": "2024-01-15",
                **extra,
            },
        )
        assert response.status_code == 201, response.text
        return response.json()

    # --- identities --------------------------------------------------------

    async def grant_account(
        self,
        *,
        roles: tuple[str, ...] = ("employee",),
        email: str | None = None,
        must_change_password: bool = False,
        is_active: bool = True,
        sign_in: bool = True,
    ) -> "Actor":
        """An employee with an account holding exactly `roles`, then signed in.

        Roles are written straight to the database because there is no endpoint
        yet to grant them (that is ticket 08b). Everything else about the caller
        is real: a committed row, a real login, a real session cookie. A test that
        needs an administrator therefore gets one the same way production would,
        rather than through a header that bypasses the mechanism under test.
        """
        address = email or f"user{uuid.uuid4().hex[:8]}@empresa.es"
        employee_id = uuid.uuid4()
        user_id = uuid.uuid4()
        username = address.split("@")[0]

        async with self.factory() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO employees (id, first_name, last_name, email, hire_date, status)
                    VALUES (:id, 'Ada', 'Lovelace', :email, '2024-01-15', 'active')
                    """
                ),
                {"id": employee_id, "email": address},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO users (id, employee_id, username, password_hash,
                                       must_change_password, is_active, session_epoch, roles)
                    VALUES (:id, :employee_id, :username, :password_hash, :must_change,
                            :active, 1, CAST(:roles AS jsonb))
                    """
                ),
                {
                    "id": user_id,
                    "employee_id": employee_id,
                    "username": username,
                    "password_hash": hash_password("Str0ng!Password1"),
                    "must_change": must_change_password,
                    "active": is_active,
                    "roles": json.dumps(sorted(set(roles) | {"employee"})),
                },
            )
            await session.commit()

        actor = Actor(
            platform=self,
            user_id=str(user_id),
            employee_id=str(employee_id),
            username=username,
            email=address,
            password="Str0ng!Password1",
            declared_roles=roles,
        )
        if sign_in:
            await actor.sign_in()
        return actor

    async def admin(self, email: str | None = None) -> "Actor":
        """An administrator with a live session."""
        return await self.grant_account(roles=("admin",), email=email)

    async def account(
        self,
        *,
        roles: tuple[str, ...] = ("employee",),
        email: str | None = None,
        must_change_password: bool = False,
    ) -> "Actor":
        """An employee with an account, committed so requests can see it.

        `roles` are written to the account, which is where the permission snapshot
        reads them. A managerial *position* additionally derives the `manager`
        role, which is where the requirement says it comes from.
        """
        return await self.grant_account(
            roles=roles, email=email, must_change_password=must_change_password
        )


class Actor:
    """One signed-in caller with its own cookie jar."""

    def __init__(
        self,
        *,
        platform: Platform,
        user_id: str,
        employee_id: str,
        username: str,
        email: str,
        password: str,
        declared_roles: tuple[str, ...],
    ) -> None:
        self.platform = platform
        self.user_id = user_id
        self.employee_id = employee_id
        self.username = username
        #: The address the fixture generated, so a test can assert on it rather
        #: than hardcoding a value the fixture never produced.
        self.email = email
        self.password = password
        self.declared_roles = declared_roles

    async def sign_in(self) -> None:
        transport = ASGITransport(app=app)
        self.client = AsyncClient(transport=transport, base_url="http://test")
        response = await self.client.post(
            "/api/v1/auth/login",
            json={"username": self.username, "password": self.password},
        )
        assert response.status_code == 200, response.text

    async def call(self, method: str, path: str, **kwargs):
        return await self.client.request(method, path, **kwargs)

    async def get(self, path: str, **kwargs):
        return await self.call("GET", path, **kwargs)

    async def post(self, path: str, **kwargs):
        return await self.call("POST", path, **kwargs)

    async def patch(self, path: str, **kwargs):
        return await self.call("PATCH", path, **kwargs)

    async def put(self, path: str, **kwargs):
        return await self.call("PUT", path, **kwargs)

    async def delete(self, path: str, **kwargs):
        return await self.call("DELETE", path, **kwargs)

    async def close(self) -> None:
        await self.client.aclose()


@asynccontextmanager
async def running_platform(settings) -> AsyncIterator[Platform]:
    """A clean database and a client, torn down afterwards."""
    engine = build_engine(settings, settings.test_database_url)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        platform = Platform(settings=settings, factory=factory, client=client)
        await platform.wipe()
        try:
            yield platform
        finally:
            await platform.wipe()
            await engine.dispose()
