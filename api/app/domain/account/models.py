"""Account value objects."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass(slots=True, frozen=True)
class UserAccount:
    """An account without its secret. This is what every read returns."""

    id: UUID
    employee_id: UUID
    username: str
    is_active: bool
    must_change_password: bool
    session_epoch: int
    last_login_at: datetime | None
    created_at: datetime
    #: Denormalised for display so a list does not need a second query per row.
    employee_full_name: str = ""
    employee_email: str = ""
    #: The stored clearance, inherited from the primary position's department when
    #: the account was created. The *effective* clearance the kernel uses is the
    #: higher of this and what the person's departments grant.
    clearance_level: str = "low"


@dataclass(slots=True, frozen=True)
class AccountWithSecret:
    """Returned once, by `create` and `reset_password`.

    The plaintext exists only in this value; it is never stored and cannot be
    retrieved again.
    """

    account: UserAccount
    temporary_password: str


@dataclass(slots=True)
class UserAccountInput:
    employee_id: UUID
    username: str


@dataclass(slots=True)
class UserAccountPatch:
    is_active: bool | None = None

    def changes(self) -> dict[str, object]:
        return {
            name: value for name in self.__slots__ if (value := getattr(self, name)) is not None
        }


class SessionRevoker(Protocol):
    """Invalidate every session issued to a user before now.

    A separate seam from the repository because sessions live in Redis, not in
    the database, and because "no sessions may survive" has to hold even when the
    database write succeeded but the cache call did not.
    """

    async def revoke_all(self, user_id: UUID, *, epoch: int) -> None: ...
