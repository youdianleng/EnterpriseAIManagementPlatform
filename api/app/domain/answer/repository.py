"""Persistence for conversations and messages, as the answer path needs it.

The repository is the boundary that owns SQL, so the driver above it deals in values and
never in `INSERT` statements. Four things it is responsible for, and each is a decision:

* **Two writes per message.** The row is created in `pending` *before* the model is
  called, and completed afterwards. That is not an optimisation: the message's id has to
  exist before the stream's first byte carries it, and a row that is only written on
  success makes a dropped connection — the exact case the ticket's 「超时」 clause is about —
  leave nothing behind. A `pending` row from yesterday is a fact; an absent row is not.
* **The completion is one statement, conditional on `pending`.** A retried request that
  raced the first one's completion must not overwrite a finished answer with a second
  model's, so the `UPDATE` is `WHERE id = :id AND completed_at IS NULL` and the row count
  is returned. Nothing here reads and then writes.
* **The reads are the conversation's owner's, always, and the filter is in the SQL.**
  D18 gives a conversation to its user, and `load_for` therefore takes the user id rather
  than testing ownership in Python. It is the same shape as the document repository's
  `get_for`: an unfiltered lookup is not expressible through this interface.
* **A refused conversation is still a conversation.** `touch` moves `last_message_at` for
  every terminal state, so a question that was refused keeps its place in the list — D20's
  refusal is an answer, and hiding it would make the list disagree with the transcript.
"""

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.answer.models import (
    AskOutcome,
    RetrievalDebug,
    citations_json,
    title_for,
)
from app.models.answer import RETENTION_DAYS


class PostgresAnswerRepository:
    """The two tables, written and read as the driver needs them."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def ensure_conversation(
        self, *, user_id: UUID, question: str, conversation_id: UUID | None = None
    ) -> UUID:
        """The conversation to append to: the one named, or a new one.

        A named conversation that belongs to somebody else is refused rather than created
        — `CONVERSATION_NOT_YOURS`'s real enforcement, since the lookup carries the user
        id and a miss is indistinguishable from a conversation that does not exist. That
        is deliberate: telling the two apart would make this endpoint an oracle over
        other people's conversations, which is the same reasoning `status_of` gives for
        documents.
        """
        if conversation_id is not None:
            owned = await self._session.scalar(
                text(
                    """
                    SELECT id FROM rag_conversations
                     WHERE id = :id AND user_id = :user_id AND NOT deleted_by_user
                    """
                ),
                {"id": conversation_id, "user_id": user_id},
            )
            if owned is None:
                raise ConversationNotFound(conversation_id)
            return owned

        now = datetime.now(UTC)
        conversation_id = await self._session.scalar(
            text(
                """
                INSERT INTO rag_conversations
                    (id, user_id, title, created_at, last_message_at, expires_at)
                VALUES
                    (gen_random_uuid(), :user_id, :title, :now, :now, :expires_at)
                RETURNING id
                """
            ),
            {
                "user_id": user_id,
                "title": title_for(question),
                "now": now,
                "expires_at": now + timedelta(days=RETENTION_DAYS),
            },
        )
        return conversation_id

    async def open_message(self, *, conversation_id: UUID, question: str) -> UUID:
        """The message row, in `pending`, before anything is generated."""
        return await self._session.scalar(
            text(
                """
                INSERT INTO rag_messages
                    (id, conversation_id, question, content, citations, retrieval_debug)
                VALUES
                    (gen_random_uuid(), :conversation_id, :question, '', '[]'::jsonb,
                     '{}'::jsonb)
                RETURNING id
                """
            ),
            {"conversation_id": conversation_id, "question": question},
        )

    async def complete_message(
        self,
        message_id: UUID,
        *,
        outcome: AskOutcome,
        debug: RetrievalDebug,
        filter_explanation: str,
    ) -> bool:
        """Write the answer, its citations and its accounting. `False` if already done.

        Conditioned on `completed_at IS NULL` for the reason the module docstring gives:
        two requests that raced must not both write, and the loser's `False` is the fact
        the driver needs in order to say so rather than to overwrite a finished answer.
        """
        written = await self._session.execute(
            text(
                """
                UPDATE rag_messages
                   SET content = :content,
                       citations = CAST(:citations AS jsonb),
                       retrieval_debug = CAST(:debug AS jsonb),
                       retrieval_filter = :filter_explanation,
                       source_notice = CAST(:source_notice AS jsonb),
                       model_used = :model,
                       provider_used = :provider,
                       token_in = :token_in,
                       token_out = :token_out,
                       latency_ms = :latency_ms,
                       is_refusal = :is_refusal,
                       error_key = :error_key,
                       completed_at = now()
                 WHERE id = :id AND completed_at IS NULL
                """
            ),
            {
                "id": message_id,
                "content": outcome.content,
                "citations": _json(citations_json(outcome.citations)),
                "debug": _json(debug.as_json()),
                "filter_explanation": filter_explanation,
                # `None` binds as SQL NULL through the cast, which is exactly the
                # ordinary case: an answer grounded only in the company knowledge base
                # has no marker to store. See `SourceNotice`.
                "source_notice": (
                    _json(outcome.source_notice.as_json())
                    if outcome.source_notice is not None
                    else None
                ),
                "model": outcome.model,
                "provider": outcome.provider,
                "token_in": outcome.token_in,
                "token_out": outcome.token_out,
                "latency_ms": outcome.latency_ms,
                "is_refusal": outcome.refusal,
                "error_key": (
                    outcome.failure.code.value if outcome.failure is not None else None
                ),
            },
        )
        return written.rowcount == 1

    async def touch_conversation(self, conversation_id: UUID) -> None:
        """`last_message_at` moves for every terminal state, refusals included."""
        await self._session.execute(
            text("UPDATE rag_conversations SET last_message_at = now() WHERE id = :id"),
            {"id": conversation_id},
        )

    async def commit(self) -> None:
        """End the transaction. **The only transaction boundary on the answer path.**

        Public rather than the driver reaching for the session, because *when* the writes
        become durable is the repository's decision — and the answer is "once, at the end".
        Committing earlier would produce a `pending` row that other connections could read,
        and it would also end the transaction-scoped permission context the retrieval needs:
        `driver.stream` records what that cost when it was done.
        """
        await self._session.commit()

    async def load_for(self, user_id: UUID, conversation_id: UUID) -> "ConversationRead | None":
        """One conversation and its messages, when it is this user's.

        One query plus one, rather than a join: the messages are a list on the response and
        a join would multiply the conversation's row, so `_messages` below is a second
        statement. Both are bounded by the conversation, which is the point.
        """
        row = (
            await self._session.execute(
                text(
                    """
                    SELECT id, title, created_at, last_message_at, expires_at
                      FROM rag_conversations
                     WHERE id = :id AND user_id = :user_id AND NOT deleted_by_user
                    """
                ),
                {"id": conversation_id, "user_id": user_id},
            )
        ).first()
        if row is None:
            return None
        return ConversationRead(
            id=row[0],
            title=row[1],
            created_at=row[2],
            last_message_at=row[3],
            expires_at=row[4],
            messages=await self.messages_of(conversation_id),
        )

    async def messages_of(self, conversation_id: UUID) -> tuple["MessageRead", ...]:
        """Every message in a conversation, oldest first, with its accounting."""
        rows = (
            await self._session.execute(
                text(
                    """
                    SELECT id, question, content, citations, model_used, provider_used,
                           token_in, token_out, latency_ms, is_refusal, error_key, status,
                           retrieval_filter, created_at, source_notice
                      FROM rag_messages
                     WHERE conversation_id = :id
                     ORDER BY created_at, id
                    """
                ),
                {"id": conversation_id},
            )
        ).all()
        return tuple(
            MessageRead(
                id=row[0],
                question=row[1],
                content=row[2],
                citations=tuple(row[3] or ()),
                model_used=row[4],
                provider_used=row[5],
                token_in=row[6],
                token_out=row[7],
                latency_ms=row[8],
                is_refusal=row[9],
                error_key=row[10],
                status=row[11],
                retrieval_filter=row[12],
                created_at=row[13],
                # Appended to the column list rather than inserted beside `citations`, so
                # the positional contract above stays legible: a new column is a new
                # index at the end and nothing before it moves — the same convention
                # `repositories/retrieval.py::_candidate` records for `is_company_kb`.
                source_notice=row[14],
            )
            for row in rows
        )

    async def own_conversation_ids(self, user_id: UUID, *, limit: int) -> tuple[UUID, ...]:
        """The user's conversations, newest first. Used by the list endpoint and tests."""
        rows = (
            await self._session.execute(
                text(
                    """
                    SELECT id FROM rag_conversations
                     WHERE user_id = :user_id AND NOT deleted_by_user
                     ORDER BY last_message_at DESC
                     LIMIT :limit
                    """
                ),
                {"user_id": user_id, "limit": limit},
            )
        ).scalars()
        return tuple(rows)


class ConversationNotFound(Exception):
    """The conversation is not this user's — or does not exist.

    One exception for the two, because the two answers are the same to the caller: a
    distinct "that one is somebody else's" would make this module an existence oracle over
    other people's conversations, exactly as `DocumentService.status_of` explains for
    documents.
    """

    def __init__(self, conversation_id: UUID) -> None:
        super().__init__(f"no conversation {conversation_id} for this caller")
        self.conversation_id = conversation_id


class MessageRead:
    """One stored message, as a reader sees it. A value, not an ORM row."""

    __slots__ = (
        "id",
        "question",
        "content",
        "citations",
        "model_used",
        "provider_used",
        "token_in",
        "token_out",
        "latency_ms",
        "is_refusal",
        "error_key",
        "status",
        "retrieval_filter",
        "created_at",
        "source_notice",
    )

    def __init__(  # noqa: PLR0913 - one row's columns, named; a dict would hide them
        self,
        *,
        id: UUID,
        question: str,
        content: str,
        citations: tuple,
        model_used: str | None,
        provider_used: str | None,
        token_in: int,
        token_out: int,
        latency_ms: int,
        is_refusal: bool,
        error_key: str | None,
        status: str,
        retrieval_filter: str | None,
        created_at: datetime,
        source_notice: dict | None = None,
    ) -> None:
        self.id = id
        self.question = question
        self.content = content
        self.citations = citations
        self.model_used = model_used
        self.provider_used = provider_used
        self.token_in = token_in
        self.token_out = token_out
        self.latency_ms = latency_ms
        self.is_refusal = is_refusal
        self.error_key = error_key
        self.status = status
        self.retrieval_filter = retrieval_filter
        self.created_at = created_at
        #: §5.2/Q29's marker, as the JSONB column holds it, or `None` for an answer
        #: grounded only in the company knowledge base (ticket 36).
        self.source_notice = source_notice


class ConversationRead:
    """One conversation with its messages."""

    __slots__ = ("id", "title", "created_at", "last_message_at", "expires_at", "messages")

    def __init__(  # noqa: PLR0913 - one row's columns, named
        self,
        *,
        id: UUID,
        title: str,
        created_at: datetime,
        last_message_at: datetime,
        expires_at: datetime,
        messages: tuple[MessageRead, ...],
    ) -> None:
        self.id = id
        self.title = title
        self.created_at = created_at
        self.last_message_at = last_message_at
        self.expires_at = expires_at
        self.messages = messages


def _json(value: object) -> str:
    """A Python value as the JSON text a `CAST(:param AS jsonb)` binding needs.

    psycopg serialises a dict into a *Postgres* composite by default, which a `jsonb`
    column refuses. Sending the text and casting it is the one spelling that works for
    every shape without registering an adapter at import time.
    """
    return json.dumps(value, ensure_ascii=False)


__all__ = [
    "ConversationNotFound",
    "ConversationRead",
    "MessageRead",
    "PostgresAnswerRepository",
]
