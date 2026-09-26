"""Authentication: login, logout and the forced password change.

The rule that shapes this module is that an account flagged
`must_change_password` cannot reach anything but the change-password endpoint.
That is enforced in the request guard (`api/v1/auth.py`) rather than by the
client choosing to navigate somewhere, so a hand-written request cannot skip it.

Every outcome is audited, including the failures: an audit trail that only
records successes cannot answer "was someone trying to get in".
"""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditAction, record
from app.core.security import hash_password, password_policy_violations, verify_password
from app.domain.account.errors import AccountErrorCode
from app.domain.account.models import UserAccount
from app.domain.account.repository import AccountRepository
from app.domain.errors import DomainError
from app.sessions import RedisSessionStore, Session

#: Which sessions a password change leaves alive.
KEEP_CURRENT = True


@dataclass(slots=True, frozen=True)
class LoginResult:
    session: Session
    account: UserAccount


@dataclass(slots=True, frozen=True)
class PasswordChangeResult:
    """The account plus the replacement session for this device.

    A password change invalidates every session, including the one the request
    arrived on — the old session carries the old epoch, so leaving it in place
    would make the very next request fail. Rather than ask the person to sign in
    again, a fresh session is issued here and returned for the cookie.
    """

    account: UserAccount
    session: Session
    other_sessions_ended: int


@dataclass(slots=True, frozen=True)
class LockoutStatus:
    locked: bool
    seconds_remaining: int


class AuthService:
    def __init__(
        self,
        repository: AccountRepository,
        session: AsyncSession,
        session_store: RedisSessionStore,
        throttle,  # noqa: ANN001 - LoginThrottle or NullThrottle
    ) -> None:
        self._repository = repository
        self._db = session
        self._sessions = session_store
        self._throttle = throttle

    async def login(
        self,
        *,
        username: str,
        password: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> LoginResult:
        """Verify credentials and open a session.

        Order matters: the lockout is checked *before* the password, so a correct
        password during a lockout is still refused. Otherwise the lockout's only
        effect would be on an attacker who already guessed wrong.
        """
        if await self._throttle.is_locked(username):
            remaining = await self._throttle.seconds_until_unlock(username)
            await record(
                self._db,
                action=AuditAction.LOGIN_FAILED,
                entity_type="user",
                entity_id=None,
                after={"username": username, "reason": "locked_out"},
                ip_address=ip_address,
                user_agent=user_agent,
            )
            await self._db.commit()
            raise DomainError(
                AccountErrorCode.ACCOUNT_LOCKED,
                detail=f"locked out; {remaining} seconds remaining",
            )

        account_row = await self._repository.get_by_username(username)
        stored_hash = (
            await self._repository.get_password_hash(account_row.id) if account_row else None
        )

        # The same generic failure for an unknown username and a wrong password:
        # distinguishing them tells an attacker which usernames exist.
        if account_row is None or stored_hash is None:
            await self._register_failure(
                username, reason="unknown_username", ip_address=ip_address, user_agent=user_agent
            )
            raise DomainError(
                AccountErrorCode.ACCOUNT_INVALID_CREDENTIALS, detail="unknown username"
            )

        if not verify_password(stored_hash, password):
            await self._register_failure(
                username, reason="password_mismatch", ip_address=ip_address, user_agent=user_agent
            )
            raise DomainError(
                AccountErrorCode.ACCOUNT_INVALID_CREDENTIALS, detail="password mismatch"
            )

        if not account_row.is_active:
            await record(
                self._db,
                action=AuditAction.LOGIN_FAILED,
                entity_type="user",
                entity_id=account_row.id,
                after={"username": username, "reason": "account_disabled"},
                ip_address=ip_address,
                user_agent=user_agent,
            )
            await self._db.commit()
            raise DomainError(
                AccountErrorCode.ACCOUNT_DISABLED, detail="account is disabled"
            )

        await self._throttle.clear(username)
        session = await self._sessions.create(
            user_id=account_row.id,
            epoch=account_row.session_epoch,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        await self._repository.touch_last_login(account_row.id)

        await record(
            self._db,
            action=AuditAction.LOGIN_SUCCEEDED,
            entity_type="user",
            entity_id=account_row.id,
            actor_user_id=account_row.id,
            after={"username": account_row.username},
            ip_address=ip_address,
            user_agent=user_agent,
        )
        await self._repository.commit()
        return LoginResult(session=session, account=account_row)

    async def logout(
        self,
        session: Session,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        await self._sessions.destroy(session.id)
        await record(
            self._db,
            action=AuditAction.LOGOUT,
            entity_type="user",
            entity_id=session.user_id,
            actor_user_id=session.user_id,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        await self._repository.commit()

    async def change_password(
        self,
        session: Session,
        *,
        current_password: str,
        new_password: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> PasswordChangeResult:
        """Set a new password and end every other session.

        The device making the change stays signed in: the person just proved who
        they are, and signing them out of the device they are holding would be
        hostile. Every other session ends, which is what "all other devices" has
        to mean to be useful.
        """
        account = await self._repository.get(session.user_id)
        if account is None:
            raise DomainError(
                AccountErrorCode.ACCOUNT_NOT_FOUND, detail="session references no account"
            )

        stored_hash = await self._repository.get_password_hash(account.id)
        if stored_hash is None or not verify_password(stored_hash, current_password):
            raise DomainError(
                AccountErrorCode.ACCOUNT_INVALID_CREDENTIALS, detail="current password mismatch"
            )

        if verify_password(stored_hash, new_password):
            raise DomainError(
                AccountErrorCode.ACCOUNT_PASSWORD_REUSED,
                detail="the new password is the current one",
            )

        violations = password_policy_violations(new_password)
        if violations:
            # Every broken rule is named, so the form can show one complete
            # message instead of making the person fix them one at a time.
            raise DomainError(
                AccountErrorCode.ACCOUNT_PASSWORD_POLICY,
                detail="policy violations: " + ", ".join(violations),
            )

        epoch = await self._repository.bump_session_epoch(account.id)
        updated = await self._repository.set_password(
            account.id, password_hash=hash_password(new_password), must_change=False
        )
        # Every existing session, the current one included: the current one is
        # replaced below rather than kept, because it carries the old epoch.
        revoked = await self._sessions.destroy_user_sessions(account.id)

        replacement = await self._sessions.create(
            user_id=account.id,
            epoch=epoch,
            ip_address=ip_address,
            user_agent=user_agent,
        )

        await record(
            self._db,
            action=AuditAction.PASSWORD_CHANGED,
            entity_type="user",
            entity_id=account.id,
            actor_user_id=account.id,
            before={"must_change_password": account.must_change_password},
            after={
                "must_change_password": False,
                "sessions_ended": revoked,
                "session_epoch": epoch,
            },
            ip_address=ip_address,
            user_agent=user_agent,
        )
        await self._repository.commit()
        return PasswordChangeResult(
            account=updated, session=replacement, other_sessions_ended=revoked
        )

    async def lockout_status(self, username: str) -> LockoutStatus:
        locked = await self._throttle.is_locked(username)
        remaining = await self._throttle.seconds_until_unlock(username) if locked else 0
        return LockoutStatus(locked=locked, seconds_remaining=remaining)

    async def _register_failure(
        self,
        username: str,
        *,
        reason: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        """Count the failure and record it.

        The audit record is committed here rather than left to the caller: the
        caller raises immediately afterwards, and an uncommitted record would be
        rolled back — leaving a trail that shows only successful logins.
        """
        await self._throttle.register_failure(username)
        await record(
            self._db,
            action=AuditAction.LOGIN_FAILED,
            entity_type="user",
            entity_id=None,
            after={"username": username, "reason": reason},
            ip_address=ip_address,
            user_agent=user_agent,
        )
        await self._db.commit()
