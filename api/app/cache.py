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
