"""Account endpoints.

Admin-only. The temporary password appears in exactly one response — the one
that creates the account or resets it — and there is no endpoint that can show it
again, because nothing stored it.

Every response is the same shape: an account, optionally carrying the one-time
password that was just issued.

**There is no change-password endpoint here.** Changing your own password lives
in `api/v1/auth.py`, because it is a session operation: it replaces the cookie,
bumps the epoch and is the single endpoint an account in the forced-change state
may reach. A second path to the same write would be guarded differently and would
drift. Administration of *other* people's passwords stays here, as a reset.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import current_principal, db_session, require
from app.api.v1.schemas.account import (
    AccountCreate,
    AccountPasswordReset,
    AccountRead,
    AccountStateChange,
    PasswordPolicyRead,
    RoleAssignment,
)
from app.cache import RedisSessionRevoker
from app.core.security import MINIMUM_PASSWORD_LENGTH, REQUIRED_CHARACTER_CLASSES
from app.domain.access import Action, Principal, ResourceKind
from app.domain.account.models import UserAccount, UserAccountInput
from app.domain.account.service import AccountService
from app.repositories.account import PostgresAccountRepository

router = APIRouter(prefix="/accounts", tags=["accounts"])

# Only an administrator manages accounts; both guards read the session's
# permission snapshot through the kernel.
manage_accounts = require(Action.ACCOUNT_MANAGE, ResourceKind.ACCOUNT)
#: A different authority from managing an account: creating a login and deciding
#: what that login may do are not the same act, and the requirement says so.
manage_roles = require(Action.ROLE_MANAGE, ResourceKind.ACCOUNT)
read_accounts = require(Action.ACCOUNT_LIST, ResourceKind.ACCOUNT)


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
    dependencies=[Depends(read_accounts)],
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
    """**Deliberately public**, marked as such rather than left unguarded.

    The sign-in screen states the rule before anyone has a session. The response
    is a fixed description of the policy and reveals nothing about any account, so
    there is nothing here to protect and no action to name.
    """
    return PasswordPolicyRead(
        minimum_length=MINIMUM_PASSWORD_LENGTH,
        required_classes=list(REQUIRED_CHARACTER_CLASSES),
    )


@router.post(
    "",
    response_model=AccountRead,
    status_code=201,
    summary="Create an account and reveal its one-time password",
    dependencies=[Depends(manage_accounts)],
)
async def create_account(
    payload: AccountCreate,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> AccountRead:
    result = await _service(session).create(
        UserAccountInput(employee_id=payload.employee_id, username=payload.username),
        actor_user_id=principal.user_id,
        actor_roles=principal.roles,
    )
    return _read(result.account, temporary_password=result.temporary_password)


@router.get(
    "/{account_id}",
    response_model=AccountRead,
    summary="Read an account",
    dependencies=[Depends(manage_accounts)],
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
    dependencies=[Depends(manage_accounts)],
)
async def deactivate_account(
    account_id: UUID,
    payload: AccountStateChange | None = None,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> AccountRead:
    """Disabling is the supported removal: it keeps history readable and stops
    access at once, without deleting the audit trail that points at the account."""
    account = await _service(session).set_active(
        account_id,
        is_active=False,
        reason=payload.reason if payload else None,
        actor_user_id=principal.user_id,
        actor_roles=principal.roles,
    )
    return _read(account)


@router.post(
    "/{account_id}/reactivate",
    response_model=AccountRead,
    summary="Re-enable a disabled account",
    dependencies=[Depends(manage_accounts)],
)
async def reactivate_account(
    account_id: UUID,
    payload: AccountStateChange | None = None,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> AccountRead:
    account = await _service(session).set_active(
        account_id,
        is_active=True,
        reason=payload.reason if payload else None,
        actor_user_id=principal.user_id,
        actor_roles=principal.roles,
    )
    return _read(account)


@router.post(
    "/{account_id}/reset-password",
    response_model=AccountRead,
    summary="Issue a new one-time password",
    dependencies=[Depends(manage_accounts)],
)
async def reset_password(
    account_id: UUID,
    payload: AccountPasswordReset | None = None,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> AccountRead:
    result = await _service(session).reset_password(
        account_id,
        reason=payload.reason if payload else None,
        actor_user_id=principal.user_id,
        actor_roles=principal.roles,
    )
    return _read(result.account, temporary_password=result.temporary_password)


@router.put(
    "/{account_id}/roles",
    response_model=AccountRead,
    summary="Replace the roles an account holds",
    dependencies=[Depends(manage_roles)],
)
async def set_roles(
    account_id: UUID,
    payload: RoleAssignment,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> AccountRead:
    """Administrator only, and immediate.

    A replacement rather than grant/revoke pairs: the caller states the outcome it
    wants, the audit record carries both sides, and two administrators doing the
    same thing twice end up in the same place.
    """
    account = await _service(session).set_roles(
        account_id,
        roles=frozenset(payload.roles),
        actor_user_id=principal.user_id,
        actor_roles=principal.roles,
    )
    return _read(account)
