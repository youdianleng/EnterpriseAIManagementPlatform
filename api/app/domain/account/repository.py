"""Persistence contract for accounts.

Implementations never commit: an account change and its audit record belong to
the same transaction.
"""

from typing import Protocol
from uuid import UUID

from app.domain.account.models import UserAccount, UserAccountInput


class AccountRepository(Protocol):
    async def get(self, user_id: UUID) -> UserAccount | None: ...

    async def get_by_username(self, username: str) -> UserAccount | None: ...

    async def get_by_employee(self, employee_id: UUID) -> UserAccount | None: ...

    async def get_password_hash(self, user_id: UUID) -> str | None: ...

    async def list_accounts(
        self, *, include_inactive: bool = True, employee_id: UUID | None = None
    ) -> list[UserAccount]: ...

    async def save(self, data: UserAccountInput, *, password_hash: str) -> UserAccount: ...

    async def set_active(self, user_id: UUID, *, is_active: bool) -> UserAccount: ...

    async def set_password(
        self, user_id: UUID, *, password_hash: str, must_change: bool
    ) -> UserAccount: ...

    async def bump_session_epoch(self, user_id: UUID) -> int: ...

    async def employee_status(self, employee_id: UUID) -> str | None: ...

    async def employee_exists(self, employee_id: UUID) -> bool: ...

    async def commit(self) -> None: ...
