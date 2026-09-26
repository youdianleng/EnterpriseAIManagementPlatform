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
