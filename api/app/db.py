"""Database engine and session wiring.

Two rules shape this module:

* One engine per process, created lazily. An engine per request would exhaust
  connections; an engine at import time would make every test that imports the
  app depend on a reachable database.
* `get_session` is the only way application code obtains a session. That keeps
  the connect/rollback/close lifecycle in one place, which is what makes the
  row-level-security context introduced in ticket 13 a single insertion point
  rather than something every call site has to remember.
"""

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings, get_settings


def build_engine(settings: Settings, url: str | None = None) -> AsyncEngine:
    """Create an engine for `url`, or the configured runtime connection.

    The default is `runtime_database_url` — the restricted role — not the owner.
    Anything that needs owner rights (migrations, fixtures that set up state)
    passes its URL explicitly, so the privileged connection is always a visible
    choice at the call site rather than something a helper does quietly.
    """
    return create_async_engine(
        url or settings.runtime_database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_recycle=settings.db_pool_recycle_seconds,
        # Surfaces stale connections as errors instead of silent retries.
        pool_pre_ping=True,
        echo=False,
    )


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    return build_engine(get_settings())


@lru_cache(maxsize=1)
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=get_engine(),
        expire_on_commit=False,
        autoflush=False,
    )


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session that always closes.

    The transaction boundary is left to the caller (service or route), so a
    request that fails part-way leaves nothing half-committed by accident.
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise

async def dispose_engine() -> None:
    """Close pooled connections; called on application shutdown."""
    if get_engine.cache_info().currsize:
        await get_engine().dispose()
        get_engine.cache_clear()
        get_session_factory.cache_clear()
