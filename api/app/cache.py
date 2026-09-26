"""Redis connections.

Redis holds sessions, permission snapshots, the ingestion queue and rate-limit
counters. All of it is reconstructible, so nothing here is load-bearing for
durability — but a missing Redis must not take the API down, and a missing
database must.
"""

from functools import lru_cache

import redis.asyncio as redis

from app.config import get_settings

# Reconnecting quickly beats waiting on a dead socket.
SOCKET_TIMEOUT_SECONDS = 5


@lru_cache(maxsize=1)
def get_redis() -> redis.Redis:
    return redis.from_url(
        get_settings().redis_url,
        decode_responses=True,
        socket_connect_timeout=SOCKET_TIMEOUT_SECONDS,
        socket_timeout=SOCKET_TIMEOUT_SECONDS,
        health_check_interval=30,
    )


async def close_redis() -> None:
    if get_redis.cache_info().currsize:
        await get_redis().aclose()
        get_redis.cache_clear()


async def ping() -> bool:
    try:
        return bool(await get_redis().ping())
    except Exception:
        return False


# --- cache invalidation ----------------------------------------------------

ORG_TREE_VERSION_KEY = "org:tree:version"


async def current_org_tree_version() -> int:
    """Version stamp for organisation-structure caches.

    Structure is read on nearly every request, so it is cached; a version stamp
    lets a write invalidate every derived entry at once without scanning keys,
    and without waiting for a TTL to expire. The permission kernel in ticket 11
    reuses this pattern.
    """
    try:
        value = await get_redis().get(ORG_TREE_VERSION_KEY)
    except Exception:
        # Cache unavailable: report a version that is never cached, so callers
        # fall back to reading the database rather than serving stale structure.
        return -1
    return int(value or 0)


async def invalidate_org_tree() -> None:
    """Called by every write that changes the department structure."""
    try:
        await get_redis().incr(ORG_TREE_VERSION_KEY)
    except Exception:
        # Losing one invalidation is survivable because reads fall back to the
        # database when the stamp cannot be read; failing the write is not.
        return


# --- session revocation ----------------------------------------------------

SESSION_EPOCH_KEY = "session:epoch:{user_id}"


async def current_session_epoch(user_id) -> int | None:  # noqa: ANN001 - UUID
    """Highest epoch issued for this user, or None when nothing is recorded.

    `None` means "no revocation has ever happened", which callers treat as
    "every session is fine" rather than "reject everything": a cache that has
    never been written must not lock people out.
    """
    try:
        value = await get_redis().get(SESSION_EPOCH_KEY.format(user_id=user_id))
    except Exception:
        return None
    return int(value) if value else None


class RedisSessionRevoker:
    """Implements the account module's `SessionRevoker` seam.

    Revocation is recorded as a number rather than by deleting session keys: the
    sessions live under unrelated keys, and scanning for them on every password
    reset would be both slow and racy. A session carries the epoch it was issued
    under, so validation is one comparison.
    """

    async def revoke_all(self, user_id, *, epoch: int) -> None:  # noqa: ANN001 - UUID
        try:
            await get_redis().set(SESSION_EPOCH_KEY.format(user_id=user_id), epoch)
        except Exception:
            # The database row already carries the new epoch, so a later
            # validation still rejects the stale session once the cache recovers.
            return
