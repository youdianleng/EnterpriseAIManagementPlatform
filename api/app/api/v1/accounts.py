"""Account endpoints.

Admin-only. The temporary password appears in exactly one response — the one
that creates the account or resets it — and there is no endpoint that can show it
again, because nothing stored it.

Every response is the same shape: an account, optionally carrying the one-time
password that was just issued.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import (
    actor_employee_id,
    actor_roles,
    db_session,
    require_roles,
)
from app.api.v1.schemas.account import (
    AccountCreate,
    AccountPasswordReset,
    AccountRead,
    AccountStateChange,
    PasswordChange,
    PasswordPolicyRead,
)
from app.cache import RedisSessionRevoker
from app.core.security import MINIMUM_PASSWORD_LENGTH, REQUIRED_CHARACTER_CLASSES
from app.domain.account.models import UserAccount, UserAccountInput
from app.domain.account.service import AccountService
from app.repositories.account import PostgresAccountRepository

router = APIRouter(prefix="/accounts", tags=["accounts"])

# Only an administrator manages accounts. Deny by default until ticket 11 wires
# the real session.
require_admin = require_roles("admin")


def _service(session: AsyncSession) -> AccountService:
    return AccountService(
        repository=PostgresAccountRepository(session),
        session=session,
        revoker=RedisSessionRevoker(),
    )


def _read(account: UserAccount, *, temporary_password: str | None = None) -> AccountRead:
    payload = AccountRead.model_validate(account, from_attributes=True)
    if temporary_password is not None:
        payload.temporary_password = temporary_password
    return payload


@router.get(
    "",
    response_model=list[AccountRead],
    summary="List accounts",
    dependencies=[Depends(require_admin)],
)
async def list_accounts(
    include_inactive: bool = Query(default=True),
    employee_id: UUID | None = Query(default=None),
    session: AsyncSession = Depends(db_session),
) -> list[AccountRead]:
    accounts = await _service(session).list_accounts(
        include_inactive=include_inactive, employee_id=employee_id
    )
    return [_read(account) for account in accounts]


@router.get(
    "/password-policy",
    response_model=PasswordPolicyRead,
    summary="The password rule, for the UI to display",
)
async def read_password_policy() -> PasswordPolicyRead:
    """Published rather than duplicated in the frontend, so the rule has one home."""
    return PasswordPolicyRead(
        minimum_length=MINIMUM_PASSWORD_LENGTH,
        required_classes=list(REQUIRED_CHARACTER_CLASSES),
    )


@router.post(
    "",
    response_model=AccountRead,
    status_code=201,
    summary="Create an account and reveal its one-time password",
    dependencies=[Depends(require_admin)],
)
async def create_account(
    payload: AccountCreate,
    roles: frozenset[str] = Depends(actor_roles),
    actor: UUID | None = Depends(actor_employee_id),
    session: AsyncSession = Depends(db_session),
) -> AccountRead:
    result = await _service(session).create(
        UserAccountInput(employee_id=payload.employee_id, username=payload.username),
        actor_user_id=actor,
        actor_roles=roles,
    )
    return _read(result.account, temporary_password=result.temporary_password)


@router.get(
    "/{account_id}",
    response_model=AccountRead,
    summary="Read an account",
    dependencies=[Depends(require_admin)],
)
async def read_account(
    account_id: UUID,
    session: AsyncSession = Depends(db_session),
) -> AccountRead:
    return _read(await _service(session).get(account_id))


@router.post(
    "/{account_id}/deactivate",
    response_model=AccountRead,
    summary="Disable an account and end its sessions",
    dependencies=[Depends(require_admin)],
)
async def deactivate_account(
    account_id: UUID,
    payload: AccountStateChange | None = None,
    roles: frozenset[str] = Depends(actor_roles),
    actor: UUID | None = Depends(actor_employee_id),
    session: AsyncSession = Depends(db_session),
) -> AccountRead:
    """Disabling is the supported removal: it keeps history readable and stops
    access at once, without deleting the audit trail that points at the account."""
    account = await _service(session).set_active(
        account_id,
        is_active=False,
        reason=payload.reason if payload else None,
        actor_user_id=actor,
        actor_roles=roles,
    )
    return _read(account)


@router.post(
    "/{account_id}/reactivate",
    response_model=AccountRead,
    summary="Re-enable a disabled account",
    dependencies=[Depends(require_admin)],
)
async def reactivate_account(
    account_id: UUID,
    payload: AccountStateChange | None = None,
    roles: frozenset[str] = Depends(actor_roles),
    actor: UUID | None = Depends(actor_employee_id),
    session: AsyncSession = Depends(db_session),
) -> AccountRead:
    account = await _service(session).set_active(
        account_id,
        is_active=True,
        reason=payload.reason if payload else None,
        actor_user_id=actor,
        actor_roles=roles,
    )
    return _read(account)


@router.post(
    "/{account_id}/reset-password",
    response_model=AccountRead,
    summary="Issue a new one-time password",
    dependencies=[Depends(require_admin)],
)
async def reset_password(
    account_id: UUID,
    payload: AccountPasswordReset | None = None,
    roles: frozenset[str] = Depends(actor_roles),
    actor: UUID | None = Depends(actor_employee_id),
    session: AsyncSession = Depends(db_session),
) -> AccountRead:
    result = await _service(session).reset_password(
        account_id,
        reason=payload.reason if payload else None,
        actor_user_id=actor,
        actor_roles=roles,
    )
    return _read(result.account, temporary_password=result.temporary_password)


@router.post(
    "/{account_id}/change-password",
    response_model=AccountRead,
    summary="Change your own password",
)
async def change_password(
    account_id: UUID,
    payload: PasswordChange,
    session: AsyncSession = Depends(db_session),
) -> AccountRead:
    """Requires the current password even though the caller holds a session.

    That requirement is what separates the account holder from someone who found
    an unlocked screen. Ticket 10 restricts this to the caller's own account.
    """
    account = await _service(session).change_own_password(
        account_id,
        current_password=payload.current_password,
        new_password=payload.new_password,
    )
    return _read(account)
