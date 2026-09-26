"""Account domain: login identities attached to employees.

The temporary password exists in exactly one place — the return value of
`create` — and is stored only as an Argon2id hash. There is no "show it again"
path because there is nothing left to show.
"""

from app.domain.account.errors import AccountErrorCode
from app.domain.account.models import (
    AccountWithSecret,
    SessionRevoker,
    UserAccount,
    UserAccountInput,
    UserAccountPatch,
)
from app.domain.account.repository import AccountRepository
from app.domain.account.service import AccountService

__all__ = [
    "AccountErrorCode",
    "AccountRepository",
    "AccountService",
    "AccountWithSecret",
    "SessionRevoker",
    "UserAccount",
    "UserAccountInput",
    "UserAccountPatch",
]
