"""Integration test for the Redis wiring.

Sessions and permission snapshots land here in later tickets, so this proves the
client works against the real server and that the keyspace survives a round trip
with the encoding the application expects.
"""

import redis.asyncio as redis


async def test_redis_round_trip_preserves_values(redis_client: redis.Redis) -> None:
    await redis_client.set("probe:greeting", "hola")

    assert await redis_client.get("probe:greeting") == "hola"


async def test_redis_client_decodes_responses_to_str(redis_client: redis.Redis) -> None:
    """Sessions and permission snapshots are JSON text; bytes would need decoding
    at every call site."""
    await redis_client.set("probe:json", '{"role": "hr"}')

    value = await redis_client.get("probe:json")

    assert isinstance(value, str)
    assert value == '{"role": "hr"}'


async def test_redis_ttl_expiry_is_settable(redis_client: redis.Redis) -> None:
    """Permission snapshots depend on TTL, so it must actually apply."""
    await redis_client.set("probe:ttl", "1", ex=60)

    ttl = await redis_client.ttl("probe:ttl")

    assert 0 < ttl <= 60


async def test_redis_ping_reports_health(redis_client: redis.Redis) -> None:
    assert await redis_client.ping() is True


async def test_the_fixture_clears_volatile_keys(redis_client: redis.Redis) -> None:
    """Guards the fixture itself: a leaked key would make later tests order-dependent."""
    assert await redis_client.keys("probe:*") == []

    await redis_client.set("probe:leak", "x")


async def test_the_fixture_leaves_sessions_alone(redis_client: redis.Redis) -> None:
    """Sessions are app state, not test pollution: a cookie held by a test must
    survive the fixture, or every signed-in test becomes order-dependent."""
    await redis_client.set("session:example", "{}")

    assert await redis_client.get("session:example") == "{}"
