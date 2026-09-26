"""PostgreSQL implementation of the account repository.

The password hash is fetched by a dedicated method rather than carried on
`UserAccount`. Keeping it off the read model means a `UserAccount` can be logged,
serialised or compared in a test without a hash travelling with it by accident.
"""

from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.account.models import (
    UserAccount,
    UserAccountInput,
)
from app.models.account import User as UserRow
from app.models.employee import Employee as EmployeeRow


def _to_domain(row: UserRow, employee: EmployeeRow | None = None) -> UserAccount:
    full_name = ""
    email = ""
    if employee is not None:
        full_name = f"{employee.first_name} {employee.last_name}"
        email = employee.email
    return UserAccount(
        id=row.id,
        employee_id=row.employee_id,
        username=row.username,
        is_active=row.is_active,
        must_change_password=row.must_change_password,
        session_epoch=row.session_epoch,
        last_login_at=row.last_login_at,
        created_at=row.created_at,
        employee_full_name=full_name,
        employee_email=email,
        clearance_level=row.clearance_level,
        roles=frozenset(row.roles or ["employee"]),
    )


class PostgresAccountRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _with_employee(self):
        return select(UserRow, EmployeeRow).outerjoin(
            EmployeeRow, EmployeeRow.id == UserRow.employee_id
        )

    async def get(self, user_id: UUID) -> UserAccount | None:
        row = (
            await self._session.execute(self._with_employee().where(UserRow.id == user_id))
        ).first()
        return _to_domain(*row) if row else None

    async def get_by_username(self, username: str) -> UserAccount | None:
        row = (
            await self._session.execute(
                self._with_employee().where(func.lower(UserRow.username) == username.lower())
            )
        ).first()
        return _to_domain(*row) if row else None

    async def get_by_employee(self, employee_id: UUID) -> UserAccount | None:
        row = (
            await self._session.execute(
                self._with_employee().where(UserRow.employee_id == employee_id)
            )
        ).first()
        return _to_domain(*row) if row else None

    async def get_password_hash(self, user_id: UUID) -> str | None:
        return await self._session.scalar(
            select(UserRow.password_hash).where(UserRow.id == user_id)
        )

    async def list_accounts(
        self, *, include_inactive: bool = True, employee_id: UUID | None = None
    ) -> list[UserAccount]:
        statement = self._with_employee()
        if not include_inactive:
            statement = statement.where(UserRow.is_active.is_(True))
        if employee_id is not None:
            statement = statement.where(UserRow.employee_id == employee_id)
        rows = (await self._session.execute(statement.order_by(UserRow.username))).all()
        return [_to_domain(*row) for row in rows]

    async def save(
        self, data: UserAccountInput, *, password_hash: str, clearance_level: str = "low"
    ) -> UserAccount:
        row = UserRow(
            employee_id=data.employee_id,
            username=data.username,
            password_hash=password_hash,
            must_change_password=True,
            is_active=True,
            clearance_level=clearance_level,
        )
        self._session.add(row)
        await self._session.flush()
        employee = await self._session.get(EmployeeRow, data.employee_id)
        return _to_domain(row, employee)

    async def primary_department_clearance(self, employee_id: UUID) -> str | None:
        """The clearance of the department the primary assignment sits in.

        `docs/DESIGN.md` §10.5: a new account starts from the primary position's
        department, so HR configures clearance once per department instead of
        once per person. The primary assignment wins; with none flagged, the
        earliest active one is used, because "no primary" is a data-entry state
        rather than a reason to leave somebody at the floor.
        """
        value = await self._session.scalar(
            text(
                """
                SELECT d.clearance_level
                FROM employee_assignments a
                JOIN departments d ON d.id = a.department_id
                WHERE a.employee_id = :employee_id
                  AND a.end_date IS NULL
                ORDER BY a.is_primary DESC, a.start_date
                LIMIT 1
                """
            ),
            {"employee_id": employee_id},
        )
        return value

    async def set_roles(self, user_id: UUID, *, roles: frozenset[str]) -> UserAccount:
        """Write the role set. The database validates the values as well."""
        await self._session.execute(
            update(UserRow)
            .where(UserRow.id == user_id)
            .values(roles=sorted(roles))
            .execution_options(synchronize_session="fetch")
        )
        await self._session.flush()
        account = await self.get(user_id)
        assert account is not None
        return account

    async def count_active_with_role(self, role: str) -> int:
        """How many enabled accounts hold this role.

        Counted in SQL rather than by loading accounts, because the caller is
        deciding whether removing the role would leave nobody able to administer
        the system, and that decision must not depend on what a page happened to
        contain.
        """
        value = await self._session.scalar(
            select(func.count())
            .select_from(UserRow)
            .where(UserRow.is_active.is_(True), UserRow.roles.contains([role]))
        )
        return int(value or 0)

    async def set_active(self, user_id: UUID, *, is_active: bool) -> UserAccount:
        # synchronize_session="fetch": a bulk UPDATE does not refresh objects
        # already in the session's identity map, so without this the row is
        # correct in the database and stale in memory — the account would come
        # back reading is_active=True right after being disabled.
        await self._session.execute(
            update(UserRow)
            .where(UserRow.id == user_id)
            .values(is_active=is_active)
            .execution_options(synchronize_session="fetch")
        )
        await self._session.flush()
        account = await self.get(user_id)
        assert account is not None
        return account

    async def set_password(
        self, user_id: UUID, *, password_hash: str, must_change: bool
    ) -> UserAccount:
        await self._session.execute(
            update(UserRow)
            .where(UserRow.id == user_id)
            .values(
                password_hash=password_hash,
                must_change_password=must_change,
                password_changed_at=func.now(),
            )
            .execution_options(synchronize_session="fetch")
        )
        await self._session.flush()
        account = await self.get(user_id)
        assert account is not None
        return account

    async def bump_session_epoch(self, user_id: UUID) -> int:
        epoch = await self._session.scalar(
            update(UserRow)
            .where(UserRow.id == user_id)
            .values(session_epoch=UserRow.session_epoch + 1)
            .returning(UserRow.session_epoch)
            .execution_options(synchronize_session="fetch")
        )
        await self._session.flush()
        assert epoch is not None
        return int(epoch)

    async def employee_status(self, employee_id: UUID) -> str | None:
        return await self._session.scalar(
            select(EmployeeRow.status).where(EmployeeRow.id == employee_id)
        )

    async def employee_exists(self, employee_id: UUID) -> bool:
        return bool(
            await self._session.scalar(
                select(func.count()).select_from(EmployeeRow).where(EmployeeRow.id == employee_id)
            )
        )

    async def touch_last_login(self, user_id: UUID) -> None:
        await self._session.execute(
            text("UPDATE users SET last_login_at = now() WHERE id = :id"), {"id": user_id}
        )

    async def commit(self) -> None:
        await self._session.commit()


__all__ = ["PostgresAccountRepository"]
