"""Account rules.

Three rules matter and all three are enforced here rather than at the edge:

* One account per employee. The database has a unique constraint on
  `employee_id`; the service checks first so the caller gets a readable conflict
  instead of an integrity error.
* No account for someone who has left. Created employees only, and an account is
  deactivated when its employee is not active.
* Disabling an account ends its sessions immediately. Setting `is_active` alone
  would leave a session that was issued a minute earlier working until it
  expired, which is not what "disabled" means.

Every operation that changes state also writes an audit record, in the same
transaction.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditAction, record
from app.core.security import (
    generate_temporary_password,
    hash_password,
)
from app.domain.account.errors import AccountErrorCode
from app.domain.account.models import (
    AccountWithSecret,
    SessionRevoker,
    UserAccount,
    UserAccountInput,
)
from app.domain.account.repository import AccountRepository
from app.domain.errors import DomainError

#: An employee in this state may not hold an active account.
INACTIVE_EMPLOYEE_STATUSES = {"terminated"}


class AccountService:
    def __init__(
        self,
        repository: AccountRepository,
        session: AsyncSession,
        revoker: SessionRevoker,
    ) -> None:
        self._repository = repository
        self._session = session
        self._revoker = revoker

    # --- reads -------------------------------------------------------------

    async def get(self, user_id: UUID) -> UserAccount:
        account = await self._repository.get(user_id)
        if account is None:
            raise DomainError(
                AccountErrorCode.ACCOUNT_NOT_FOUND, detail=f"unknown account {user_id}"
            )
        return account

    async def list_accounts(
        self, *, include_inactive: bool = True, employee_id: UUID | None = None
    ) -> list[UserAccount]:
        return await self._repository.list_accounts(
            include_inactive=include_inactive, employee_id=employee_id
        )

    # --- writes ------------------------------------------------------------

    async def create(
        self,
        data: UserAccountInput,
        *,
        actor_user_id: UUID | None = None,
        actor_roles: frozenset[str] = frozenset(),
    ) -> AccountWithSecret:
        """Create an account and hand back its one-time password.

        The plaintext is generated here, hashed, and returned. It is never
        written anywhere, so the response is genuinely the only chance to read
        it — not a policy the code is merely supposed to follow.
        """
        employee_status = await self._repository.employee_status(data.employee_id)
        if employee_status is None:
            raise DomainError(
                AccountErrorCode.ACCOUNT_EMPLOYEE_NOT_ACTIVE,
                detail=f"employee {data.employee_id} does not exist",
            )
        if employee_status in INACTIVE_EMPLOYEE_STATUSES:
            raise DomainError(
                AccountErrorCode.ACCOUNT_EMPLOYEE_NOT_ACTIVE,
                detail=f"employee status is {employee_status}",
            )

        if await self._repository.get_by_employee(data.employee_id) is not None:
            raise DomainError(
                AccountErrorCode.ACCOUNT_EMPLOYEE_HAS_ACCOUNT,
                detail=f"employee {data.employee_id} already has an account",
            )
        if await self._repository.get_by_username(data.username) is not None:
            raise DomainError(
                AccountErrorCode.ACCOUNT_USERNAME_TAKEN,
                detail=f"username {data.username} is in use",
            )

        temporary_password = generate_temporary_password()
        # Inherited from the primary position's department (DESIGN §10.5), so a new
        # account starts at the level its department works at instead of at the
        # floor. It is a starting value, not a ceiling: the kernel takes the higher
        # of this and what the departments grant.
        inherited_clearance = (
            await self._repository.primary_department_clearance(data.employee_id) or "low"
        )
        account = await self._repository.save(
            data,
            password_hash=hash_password(temporary_password),
            clearance_level=inherited_clearance,
        )

        await record(
            self._session,
            action=AuditAction.ACCOUNT_CREATED,
            entity_type="user",
            entity_id=account.id,
            actor_user_id=actor_user_id,
            actor_roles=actor_roles,
            after={
                "username": account.username,
                "employee_id": str(account.employee_id),
                "must_change_password": True,
                "clearance_level": inherited_clearance,
            },
        )
        await self._repository.commit()
        return AccountWithSecret(account=account, temporary_password=temporary_password)

    async def set_active(
        self,
        user_id: UUID,
        *,
        is_active: bool,
        reason: str | None = None,
        actor_user_id: UUID | None = None,
        actor_roles: frozenset[str] = frozenset(),
    ) -> UserAccount:
        account = await self.get(user_id)
        if account.is_active == is_active:
            raise DomainError(
                AccountErrorCode.ACCOUNT_ALREADY_IN_STATE,
                detail=f"account is already {'active' if is_active else 'disabled'}",
            )

        await self._repository.set_active(user_id, is_active=is_active)

        if not is_active:
            # Bumping the epoch is what makes this immediate: a session carries
            # the epoch it was issued under, so every older session stops
            # matching without the server having to find them.
            epoch = await self._repository.bump_session_epoch(user_id)
            await self._revoker.revoke_all(user_id, epoch=epoch)

        # Re-read after both writes, so the response carries the epoch that
        # every future session check will be compared against.
        updated = await self.get(user_id)

        await record(
            self._session,
            action=(
                AuditAction.ACCOUNT_REACTIVATED if is_active else AuditAction.ACCOUNT_DEACTIVATED
            ),
            entity_type="user",
            entity_id=user_id,
            actor_user_id=actor_user_id,
            actor_roles=actor_roles,
            before={"is_active": account.is_active},
            after={"is_active": updated.is_active},
            reason=reason,
        )
        await self._repository.commit()
        return updated

    async def reset_password(
        self,
        user_id: UUID,
        *,
        reason: str | None = None,
        actor_user_id: UUID | None = None,
        actor_roles: frozenset[str] = frozenset(),
    ) -> AccountWithSecret:
        """Issue a new one-time password and end every existing session.

        A reset that left old sessions alive would let whoever forced the reset
        keep using the account, which defeats the purpose.
        """
        account = await self.get(user_id)

        temporary_password = generate_temporary_password()
        # The epoch is bumped before the account is re-read, so the value handed
        # back already reflects the invalidation. Bumping afterwards would return
        # a stale epoch while the database held the new one, and a caller caching
        # the response would keep a value that no session check agrees with.
        epoch = await self._repository.bump_session_epoch(user_id)
        updated = await self._repository.set_password(
            user_id,
            password_hash=hash_password(temporary_password),
            must_change=True,
        )
        await self._revoker.revoke_all(user_id, epoch=epoch)

        await record(
            self._session,
            action=AuditAction.ACCOUNT_PASSWORD_RESET,
            entity_type="user",
            entity_id=user_id,
            actor_user_id=actor_user_id,
            actor_roles=actor_roles,
            before={"must_change_password": account.must_change_password},
            after={"must_change_password": True, "session_epoch": updated.session_epoch},
            reason=reason,
        )
        await self._repository.commit()
        return AccountWithSecret(account=updated, temporary_password=temporary_password)
