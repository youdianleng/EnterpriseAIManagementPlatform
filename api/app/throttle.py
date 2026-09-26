"""Login throttling.

Five failures lock the account for fifteen minutes. Counting is per account
rather than per source address: an attacker who moves between addresses would
slip past an address-keyed counter, while the person being attacked cannot.

Redis carries a TTL, so the window closes on its own and no cleanup job is needed.
"""

from app.cache import get_redis

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_SECONDS = 15 * 60

FAILED_ATTEMPTS_KEY = "auth:failures:{username}"


def normalise(username: str) -> str:
    """Usernames are case-insensitive, so the counter must be too.

    Otherwise `Ana` and `ana` would each get five attempts.
    """
    return username.strip().lower()


class LoginThrottle:
    async def failed_attempts(self, username: str) -> int:
        raw = await get_redis().get(FAILED_ATTEMPTS_KEY.format(username=normalise(username)))
        return int(raw) if raw else 0

    async def register_failure(self, username: str) -> int:
        """Count a failure and return the new total.

        The TTL is set on every increment, so the window is "fifteen minutes
        since the last failure" rather than a fixed window from the first one.
        """
        client = get_redis()
        key = FAILED_ATTEMPTS_KEY.format(username=normalise(username))
        count = await client.incr(key)
        await client.expire(key, LOCKOUT_SECONDS)
        return int(count)

    async def is_locked(self, username: str) -> bool:
        return await self.failed_attempts(username) >= MAX_FAILED_ATTEMPTS

    async def seconds_until_unlock(self, username: str) -> int:
        ttl = await get_redis().ttl(FAILED_ATTEMPTS_KEY.format(username=normalise(username)))
        return max(int(ttl), 0)

    async def clear(self, username: str) -> None:
        """Called on a successful login, so a good password resets the count."""
        await get_redis().delete(FAILED_ATTEMPTS_KEY.format(username=normalise(username)))


class NullThrottle:
    """Used when Redis is unreachable.

    Fails open on purpose: locking every account out because the cache is down
    would turn a cache outage into a total outage. The password check still runs.
    """

    async def failed_attempts(self, username: str) -> int:
        return 0

    async def register_failure(self, username: str) -> int:
        return 0

    async def is_locked(self, username: str) -> bool:
        return False

    async def seconds_until_unlock(self, username: str) -> int:
        return 0

    async def clear(self, username: str) -> None:
        return None
