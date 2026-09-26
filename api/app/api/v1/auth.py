"""Authentication endpoints and the request guard.

Two things here are worth reading closely.

**The forced-change gate.** An account flagged `must_change_password` may reach
nothing but the change-password endpoint and the two endpoints that report *why*
it is being refused. The gate is applied by the server on every request, not by
the client choosing where to navigate, so a hand-written request cannot skip it.

**The cookie.** The session id travels in an httpOnly cookie, so browser script
cannot read it. Nothing about the account is encoded in the token — it is an
opaque pointer to server-side state, which is what makes revocation immediate
rather than something that waits for an expiry.
"""

from dataclasses import dataclass

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import db_session
from app.api.v1.schemas.auth import (
    LoginRequest,
    PasswordChangeRequest,
    PasswordPolicyRead,
    SessionRead,
)
from app.audit import AuditAction, record
from app.cache import get_redis
from app.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.core.security import MINIMUM_PASSWORD_LENGTH, REQUIRED_CHARACTER_CLASSES
from app.domain.account.models import UserAccount
from app.domain.auth.service import AuthService
from app.repositories.account import PostgresAccountRepository
from app.sessions import SESSION_TTL_SECONDS, RedisSessionStore, Session
from app.throttle import LoginThrottle, NullThrottle

router = APIRouter(prefix="/auth", tags=["authentication"])

SESSION_COOKIE = "eam_session"


@dataclass(slots=True, frozen=True)
class ResolvedSession:
    session: Session
    account: UserAccount


def store() -> RedisSessionStore:
    return RedisSessionStore()


async def reachable_throttle() -> LoginThrottle | NullThrottle:
    """Use Redis when it answers, otherwise stop throttling rather than stop logins.

    Failing open is deliberate: a cache outage must not lock every account out of
    the system. The password check still runs either way.
    """
    try:
        await get_redis().ping()
    except Exception:
        return NullThrottle()
    return LoginThrottle()


def build_service(session: AsyncSession, throttle) -> AuthService:  # noqa: ANN001
    return AuthService(
        repository=PostgresAccountRepository(session),
        session=session,
        session_store=store(),
        throttle=throttle,
    )


async def resolve_session(
    request: Request,
    session: AsyncSession,
    *,
    enforce_password_change: bool = True,
) -> ResolvedSession:
    """Load and validate the caller's session.

    Validation compares the session's epoch against the account's current epoch.
    That is what makes a password change or an administrative kick take effect on
    the very next request rather than whenever the cookie happens to expire.
    """
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        raise AppError(ErrorCode.SESSION_INVALID, detail="no session cookie")

    sessions = store()
    stored = await sessions.get(raw)
    if stored is None:
        raise AppError(ErrorCode.SESSION_INVALID, detail="unknown or expired session")

    account = await PostgresAccountRepository(session).get(stored.user_id)
    if account is None or not account.is_active:
        # The account went away or was disabled: drop the session rather than
        # leaving a key that will be checked again on every request.
        await sessions.destroy(stored.id)
        raise AppError(ErrorCode.SESSION_INVALID, detail="account missing or disabled")

    if stored.epoch < account.session_epoch:
        await sessions.destroy(stored.id)
        raise AppError(
            ErrorCode.SESSION_INVALID,
            detail=f"session epoch {stored.epoch} predates {account.session_epoch}",
        )

    if enforce_password_change and account.must_change_password:
        raise AppError(
            ErrorCode.PASSWORD_CHANGE_REQUIRED,
            detail=f"account {account.username} must change its password",
        )

    await sessions.touch(stored.id)
    return ResolvedSession(session=stored, account=account)


async def current_session(
    request: Request,
    session: AsyncSession = Depends(db_session),
) -> Session:
    """Guard for any endpoint that requires a fully usable session."""
    resolved = await resolve_session(request, session)
    return resolved.session


async def current_account(
    request: Request,
    session: AsyncSession = Depends(db_session),
) -> UserAccount:
    resolved = await resolve_session(request, session)
    return resolved.account


def _session_read(account: UserAccount) -> SessionRead:
    return SessionRead(
        user_id=account.id,
        username=account.username,
        employee_id=account.employee_id,
        employee_full_name=account.employee_full_name,
        must_change_password=account.must_change_password,
    )


def _set_session_cookie(response: Response, session: Session) -> None:
    settings = get_settings()
    response.set_cookie(
        key=SESSION_COOKIE,
        value=session.id,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,  # browser script cannot read it
        samesite="lax",
        # Off over plain HTTP on a LAN, on as soon as the deployment has TLS.
        secure=not settings.is_development,
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.post("/login", response_model=SessionRead, summary="Sign in")
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(db_session),
) -> SessionRead:
    result = await build_service(session, await reachable_throttle()).login(
        username=payload.username,
        password=payload.password,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    _set_session_cookie(response, result.session)
    return _session_read(result.account)


@router.post("/logout", status_code=204, summary="Sign out")
async def logout(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(db_session),
) -> None:
    """Ends the current session only.

    Deliberately reachable without a valid session: signing out when the session
    has already expired should be a no-op, not an error.
    """
    raw = request.cookies.get(SESSION_COOKIE)
    if raw:
        stored = await store().get(raw)
        if stored is not None:
            await build_service(session, await reachable_throttle()).logout(
                stored,
                ip_address=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent"),
            )
    _clear_session_cookie(response)


@router.get("/session", response_model=SessionRead, summary="Who am I")
async def read_session(
    request: Request,
    session: AsyncSession = Depends(db_session),
) -> SessionRead:
    """Reports identity *and* whether a password change is pending.

    The gate is deliberately not applied here: this is the call that tells the
    client why everything else is being refused, so it has to answer in that state.
    """
    resolved = await resolve_session(request, session, enforce_password_change=False)
    return _session_read(resolved.account)


@router.get(
    "/password-policy", response_model=PasswordPolicyRead, summary="The password rule"
)
async def read_password_policy() -> PasswordPolicyRead:
    return PasswordPolicyRead(
        minimum_length=MINIMUM_PASSWORD_LENGTH,
        required_classes=list(REQUIRED_CHARACTER_CLASSES),
    )


@router.post(
    "/change-password",
    response_model=SessionRead,
    summary="Set a new password",
)
async def change_password(
    payload: PasswordChangeRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(db_session),
) -> SessionRead:
    """The one state-changing endpoint an account in the forced-change state may reach."""
    resolved = await resolve_session(request, session, enforce_password_change=False)
    result = await build_service(session, await reachable_throttle()).change_password(
        resolved.session,
        current_password=payload.current_password,
        new_password=payload.new_password,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    # New session, new epoch: the cookie has to be replaced, because the check
    # that just ran invalidated the session this request arrived on.
    _set_session_cookie(response, result.session)
    return _session_read(result.account)


@router.post(
    "/sessions/end-all",
    status_code=204,
    summary="End every session for the caller",
)
async def end_all_sessions(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(db_session),
) -> None:
    """Used when someone suspects their account is in use elsewhere.

    Also the mechanism the administrative "force out" will call in ticket 11.
    """
    resolved = await resolve_session(request, session, enforce_password_change=False)
    epoch = await PostgresAccountRepository(session).bump_session_epoch(resolved.account.id)
    await store().destroy_user_sessions(resolved.account.id)
    await record(
        session,
        action=AuditAction.SESSIONS_FORCED_OUT,
        entity_type="user",
        entity_id=resolved.account.id,
        actor_user_id=resolved.account.id,
        after={"session_epoch": epoch},
        ip_address=request.client.host if request.client else None,
    )
    await session.commit()
    _clear_session_cookie(response)
