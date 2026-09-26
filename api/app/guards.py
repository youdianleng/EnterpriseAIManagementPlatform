"""Cross-cutting request guards applied to the whole API.

`enforce_password_change` runs on every request rather than being attached to
each protected route. Attaching it per route means the rule holds only where
somebody remembered to add it, and a new endpoint added later would silently be
exempt — which is exactly the failure mode the requirement exists to prevent.

It is a no-op when no session cookie is present, so the transitional
header-based authorisation used by the v1 routers keeps working until ticket 11
moves everything onto sessions.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

#: Paths that must keep working while a password change is pending, or a caller
#: could never learn why it is being refused or complete the change.
PASSWORD_CHANGE_EXEMPT_PREFIXES = (
    "/api/v1/auth/login",
    "/api/v1/auth/logout",
    "/api/v1/auth/session",
    "/api/v1/auth/change-password",
    "/api/v1/auth/password-policy",
    # Health and readiness are not user-facing and must answer regardless.
    "/health",
    "/ready",
)


def _is_exempt(path: str) -> bool:
    return any(
        path == prefix or path.startswith(f"{prefix}/")
        for prefix in PASSWORD_CHANGE_EXEMPT_PREFIXES
    )


@asynccontextmanager
async def _session_for(request: Request) -> AsyncIterator[AsyncSession]:
    """A session for the guard, honouring an overridden session dependency.

    Reuses the same override the routes use. Opening a session straight from the
    factory instead would read from a different transaction, which in tests means
    the guard cannot see rows the test created — and, more importantly, would mean
    the guard and the route could disagree about what exists.

    Both keys are checked because the routers depend on the `db_session` wrapper
    while other callers may override the underlying `get_session`.
    """
    from app.api.v1.deps import db_session
    from app.db import get_session

    overrides = request.app.dependency_overrides
    override = overrides.get(db_session) or overrides.get(get_session)
    if override is not None:
        async for session in override():
            yield session
        return

    factory = request.app.state.session_factory
    async with factory() as session:
        yield session


async def enforce_password_change(request: Request) -> None:
    """Refuse everything but the change-password flow while one is pending."""
    from app.api.v1.auth import SESSION_COOKIE, resolve_session

    if _is_exempt(request.url.path):
        return

    if SESSION_COOKIE not in request.cookies:
        # Transitional: routes not yet on sessions. Ticket 11 removes this branch
        # by making every protected route require a session.
        return

    async with _session_for(request) as session:
        await resolve_session(request, session)
