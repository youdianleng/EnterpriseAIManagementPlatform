"""The `agent_actions` table, read and written as the draft path needs it.

Ticket 34's arrangement, for the same reasons: the repository is in the domain package
(`app/domain/answer/repository.py` is its precedent), it speaks SQL rather than the ORM
because two of these statements are about the *database's own clock*, and the service
above it owns the transaction.

**Two statements compute their own instants, and that is the whole reason this file is
not a pair of ORM calls.** `insert` asks PostgreSQL for `now()` and writes
`expires_at = now() + make_interval(hours => :ttl)` in the same statement, returning both
— so the draft's birthday and its expiry come from one clock, and a reader later compares
against the *same* clock. `newest_for_conversation` answers `expires_at <= now()` in the
SELECT, so the row that comes back carries the database's verdict rather than the
process's opinion.

**Nothing here reads a row after committing.** The request's permission context is
published with `set_config(..., is_local => true)` (`app/api/v1/deps.py`), which is
transaction-scoped: ticket 34 recorded a bug where a commit in the middle of the answer
path ended the context and a following read found nothing. Both writes below therefore use
`RETURNING`, and the service commits once, after everything it needs has come back.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.agent.models import AgentAction, DraftStatus, PrefillForm
from app.logging import get_logger

logger = get_logger(__name__)

#: The table's columns, in one place so the two SELECTs cannot disagree about the shape.
COLUMNS = """
    id, conversation_id, user_id, thread_id, tool_name, tool_input, tool_output,
    produced_prefill_form, status, created_at, expires_at, confirmed_at,
    resulting_entity_type, resulting_entity_id
"""


@dataclass(frozen=True, slots=True)
class StoredAgentAction:
    """One row, plus the database's answer about whether it has lapsed.

    `expired` is not in the row and is not derivable from it alone: it is the result of
    `expires_at <= now()`, evaluated by PostgreSQL. Keeping it beside the row rather than
    inside `AgentAction` is what stops a reader from believing it can compute a status the
    database owns.
    """

    action: AgentAction
    expired: bool

    @property
    def status(self) -> DraftStatus:
        """The status to act on: the row's, unless the row has lapsed."""
        return self.action.effective_status(expired=self.expired)


class PostgresAgentActionRepository:
    """§3.6's `agent_actions`, as the draft path uses it."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert(
        self,
        *,
        conversation_id: UUID,
        user_id: UUID,
        tool_name: str,
        tool_input: Mapping[str, Any],
        tool_output: Mapping[str, Any],
        form: PrefillForm | None,
        ttl_hours: int,
        thread_id: str | None = None,
    ) -> AgentAction:
        """Write one proposed draft, with both instants from the database's clock.

        `ttl_hours` arrives as a number rather than as a pre-computed instant so that the
        arithmetic happens where the clock is. `make_interval` is used rather than string
        concatenation because the setting is an integer a deployment types by hand, and
        building an interval out of it by hand is how `AGENT_DRAFT_TTL_HOURS='24h'` becomes
        a syntax error inside a write path.
        """
        row = (
            await self._session.execute(
                text(
                    f"""
                    INSERT INTO agent_actions
                        (id, conversation_id, user_id, thread_id, tool_name, tool_input,
                         tool_output, produced_prefill_form, status, expires_at)
                    VALUES
                        (gen_random_uuid(), :conversation_id, :user_id, :thread_id,
                         :tool_name, CAST(:tool_input AS jsonb),
                         CAST(:tool_output AS jsonb), CAST(:form AS jsonb), :status,
                         now() + make_interval(hours => :ttl_hours))
                    RETURNING {COLUMNS}
                    """
                ),
                {
                    "conversation_id": conversation_id,
                    "user_id": user_id,
                    "thread_id": thread_id,
                    "tool_name": tool_name,
                    "tool_input": _json(tool_input),
                    "tool_output": _json(tool_output),
                    "form": None if form is None else _json(form.as_dict()),
                    "status": str(DraftStatus.PROPOSED),
                    "ttl_hours": ttl_hours,
                },
            )
        ).mappings().one()
        return _action(row)

    async def newest_for_conversation(self, conversation_id: UUID) -> StoredAgentAction | None:
        """The conversation's most recent draft, with the database's verdict on its age.

        Newest rather than "the pending one": an employee who let a draft lapse and asked
        again has two rows, and the newer one is the one to act on — while the older row
        must stay readable as the evidence that a lapsed draft was proposed and never
        confirmed.
        """
        row = (
            await self._session.execute(
                text(
                    f"""
                    SELECT {COLUMNS}, (status = 'proposed' AND expires_at <= now()) AS expired
                      FROM agent_actions
                     WHERE conversation_id = :conversation_id
                     ORDER BY created_at DESC, id DESC
                     LIMIT 1
                    """
                ),
                {"conversation_id": conversation_id},
            )
        ).mappings().one_or_none()
        if row is None:
            return None
        return StoredAgentAction(action=_action(row), expired=bool(row["expired"]))

    async def mark_expired(self, action_id: UUID) -> bool:
        """Record that a proposed draft has lapsed. Idempotent, and guarded in SQL.

        The guard is the point: `status = 'proposed' AND expires_at <= now()` means this
        statement cannot expire something that was confirmed a moment ago, however the two
        requests interleave. Returning whether it moved anything lets a caller say so.
        """
        moved = await self._session.scalar(
            text(
                """
                UPDATE agent_actions
                   SET status = 'expired'
                 WHERE id = :id AND status = 'proposed' AND expires_at <= now()
                RETURNING id
                """
            ),
            {"id": action_id},
        )
        return moved is not None

    async def commit(self) -> None:
        await self._session.commit()

    async def rollback(self) -> None:
        await self._session.rollback()


def _json(value: Mapping[str, Any]) -> str:
    import json

    return json.dumps(dict(value), ensure_ascii=False, default=str)


def _action(row: Mapping[str, Any]) -> AgentAction:
    return AgentAction(
        id=row["id"],
        conversation_id=row["conversation_id"],
        user_id=row["user_id"],
        tool_name=row["tool_name"],
        status=DraftStatus(row["status"]),
        created_at=_instant(row["created_at"]),
        expires_at=_instant(row["expires_at"]),
        tool_input=dict(row["tool_input"] or {}),
        tool_output=dict(row["tool_output"] or {}),
        form=PrefillForm.from_stored(row["produced_prefill_form"]),
        thread_id=row["thread_id"],
        confirmed_at=row["confirmed_at"],
        resulting_entity_type=row["resulting_entity_type"],
        resulting_entity_id=row["resulting_entity_id"],
    )


def _instant(value: datetime) -> datetime:
    """The instant PostgreSQL returned, unchanged.

    A `str` would mean the driver handed back something the column is not; `datetime` is
    what a `timestamptz` is, and asserting it here is cheaper than a comparison two layers
    up that silently compares a string with a datetime.
    """
    assert isinstance(value, datetime), value
    return value


__all__ = ["PostgresAgentActionRepository", "StoredAgentAction"]
