"""Server-side sessions in Redis.

The browser holds an opaque session id in an httpOnly cookie and nothing else.
Everything that decides access — who the session belongs to, which epoch it was
issued under — lives here, on the server, where browser script cannot reach it.

Two keys per session:

* `session:<id>` — the session record, with a sliding TTL.
* `session:user:<user_id>` — a set of that user's live session ids.

The set is the reason a password change can end sessions on *other* devices
without scanning the keyspace: `KEYS`/`SCAN` over a shared Redis would be both
slow and racy, while removing exactly the ids in the set is a single DEL.
"""

import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.cache import get_redis

#: Sliding window. A session that is used stays alive; one that is not expires.
SESSION_TTL_SECONDS = 8 * 60 * 60

SESSION_KEY = "session:{session_id}"
USER_SESSIONS_KEY = "session:user:{user_id}"

#: Opaque and unguessable. 32 bytes of entropy, URL-safe.
SESSION_ID_BYTES = 32


@dataclass(slots=True, frozen=True)
class Session:
    id: str
    user_id: UUID
    #: The epoch this session was issued under. When the account's epoch moves
    #: past this value, the session is refused.
    epoch: int
    issued_at: datetime
    last_seen_at: datetime
    ip_address: str | None
    user_agent: str | None


def _now() -> datetime:
    return datetime.now(UTC)


def _to_json(session: Session) -> str:
    return json.dumps(
        {
            "user_id": str(session.user_id),
            "epoch": session.epoch,
            "issued_at": session.issued_at.isoformat(),
            "last_seen_at": session.last_seen_at.isoformat(),
            "ip_address": session.ip_address,
            "user_agent": session.user_agent,
        }
    )


def _from_json(session_id: str, raw: str) -> Session:
    payload: dict[str, Any] = json.loads(raw)
    return Session(
        id=session_id,
        user_id=UUID(payload["user_id"]),
        epoch=int(payload["epoch"]),
        issued_at=datetime.fromisoformat(payload["issued_at"]),
        last_seen_at=datetime.fromisoformat(payload["last_seen_at"]),
        ip_address=payload.get("ip_address"),
        user_agent=payload.get("user_agent"),
    )


class RedisSessionStore:
    """Implements the auth module's session seam."""

    async def create(
        self,
        *,
        user_id: UUID,
        epoch: int,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> Session:
        session = Session(
            id=secrets.token_urlsafe(SESSION_ID_BYTES),
            user_id=user_id,
            epoch=epoch,
            issued_at=_now(),
            last_seen_at=_now(),
            ip_address=ip_address,
            user_agent=user_agent,
        )
        client = get_redis()
        pipe = client.pipeline()
        pipe.set(
            SESSION_KEY.format(session_id=session.id),
            _to_json(session),
            ex=SESSION_TTL_SECONDS,
        )
        pipe.sadd(USER_SESSIONS_KEY.format(user_id=user_id), session.id)
        # The index outlives any individual session only slightly; the session
        # keys are what carry the real deadline, and stale ids are pruned on read.
        pipe.expire(USER_SESSIONS_KEY.format(user_id=user_id), SESSION_TTL_SECONDS * 2)
        await pipe.execute()
        return session

    async def get(self, session_id: str) -> Session | None:
        raw = await get_redis().get(SESSION_KEY.format(session_id=session_id))
        return _from_json(session_id, raw) if raw else None

    async def touch(self, session_id: str) -> None:
        """Extend the window; called on each authenticated request."""
        client = get_redis()
        key = SESSION_KEY.format(session_id=session_id)
        raw = await client.get(key)
        if not raw:
            return
        session = _from_json(session_id, raw)
        refreshed = Session(
            id=session.id,
            user_id=session.user_id,
            epoch=session.epoch,
            issued_at=session.issued_at,
            last_seen_at=_now(),
            ip_address=session.ip_address,
            user_agent=session.user_agent,
        )
        await client.set(key, _to_json(refreshed), ex=SESSION_TTL_SECONDS)

    async def destroy(self, session_id: str) -> None:
        client = get_redis()
        key = SESSION_KEY.format(session_id=session_id)
        raw = await client.get(key)
        if raw:
            session = _from_json(session_id, raw)
            await client.srem(USER_SESSIONS_KEY.format(user_id=session.user_id), session_id)
        await client.delete(key)

    async def destroy_user_sessions(
        self, user_id: UUID, *, keep_session_id: str | None = None
    ) -> int:
        """End every session for a user, optionally sparing the current one.

        Sparing the current one is what lets a password change keep the person
        signed in on the device they are using while logging out everywhere else —
        which is what "all other devices" has to mean to be useful.
        """
        client = get_redis()
        index_key = USER_SESSIONS_KEY.format(user_id=user_id)
        session_ids = await client.smembers(index_key)
        doomed = [sid for sid in session_ids if sid != keep_session_id]
        if doomed:
            await client.delete(*[SESSION_KEY.format(session_id=sid) for sid in doomed])

        # Rebuild the index from whatever survived, so it never accumulates ids
        # whose session key has already expired.
        await client.delete(index_key)
        survivors = [sid for sid in session_ids if sid == keep_session_id]
        if survivors:
            await client.sadd(index_key, *survivors)
            await client.expire(index_key, SESSION_TTL_SECONDS * 2)
        return len(doomed)

    async def count_user_sessions(self, user_id: UUID) -> int:
        """Live sessions for one user; stale index entries are ignored."""
        client = get_redis()
        session_ids = await client.smembers(USER_SESSIONS_KEY.format(user_id=user_id))
        if not session_ids:
            return 0
        values = await client.mget(*[SESSION_KEY.format(session_id=sid) for sid in session_ids])
        return sum(1 for value in values if value)
