"""Answer endpoints: the streamed grounded answer, and reading one back.

**`POST /answers` streams.** §5.2 is 「流式 SSE」 and the ticket's first checklist line asks
for the answer to reach the client incrementally, so the response is a `StreamingResponse`
of `event:`/`data:` frames with `Cache-Control: no-store` and `X-Accel-Buffering: no`. The
three headers are all load-bearing: the media type is what makes a browser's `EventSource`
work, `no-store` is what stops an intermediary caching one person's answer for another,
and `no-cache`/`no-transform` are what stop a proxy buffering the whole body and handing it
over at the end — which would look exactly like the ticket's twenty-second blank screen.

**The route owns no rule.** Which documents may ground an answer is
`domain/retrieval/filtering.py::answer_filter_for`; whether the answer is a refusal is
`SearchOutcome.insufficient_evidence`; what the model is told is
`domain/answer/prompts.py`. This module translates HTTP into those calls and the events
back out, which is the whole of its job — and it is why the permission filter is produced
by a named helper rather than spelled here: a route that built a `FilterSpec` would be the
second place §4.2 is decided.

**Why the permission guard is `document.read`.** The same action
`/retrieval/search` and the document list use, and for the same reason: an answer is
grounded in documents the caller may open, and a citation that cannot be opened is not a
citation. *Which* documents is §4.2's question, answered by the filter — not a second
action here, which would be a second rule to keep in step.

**Why `POST` and not `GET`.** §5.2 describes a question and its answer, and a streaming
`GET` is the classic way to get an answer cached by something that should not have it.
The body is the question, which fits `AskRequest`.

**`GET /answers/conversations/{id}` reads one back**, and is guarded by the same ownership
the stream writes under: D18 gives a conversation to its user, and a conversation that is
somebody else's answers 404 — the same answer as one that does not exist — because telling
the two apart would make this endpoint an existence oracle over other people's questions.
"""

from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import current_principal, db_session, require
from app.api.v1.schemas.answer import (
    SSE_MEDIA_TYPE,
    AskRequest,
    CitationRead,
    ConversationRead,
    MessageRead,
    source_notice_read,
    sse_frame,
)
from app.core.errors import AppError, ErrorCode
from app.domain.access import Action, Principal
from app.domain.access.kernel import ResourceKind
from app.domain.answer.chat import build_chat_model
from app.domain.answer.driver import AnswerService
from app.domain.answer.repository import ConversationRead as StoredConversation
from app.domain.answer.repository import MessageRead as StoredMessage
from app.domain.answer.repository import PostgresAnswerRepository
from app.domain.retrieval.filtering import ASK_ACTION, answer_filter_for
from app.domain.retrieval.service import RetrievalService
from app.repositories.retrieval import PostgresChunkSearchRepository

router = APIRouter(prefix="/answers", tags=["answers"])

#: Everyone who may read a document may ask about it. The *reach* is the `FilterSpec` the
#: helper produces, never a second rule here — and the helper asks this same action again
#: one level down, so a caller that reached the answer path by another route is refused
#: identically.
ask_questions = require(ASK_ACTION, ResourceKind.DOCUMENT)

#: Whose conversation it is, decided by the kernel rather than by a comparison in the
#: handler: every role may read its own, and `session.read_own` is the catalogued way of
#: saying so for a surface that answers about the caller.
read_own_conversation = require(Action.SESSION_READ_OWN, ResourceKind.ACCOUNT)


def _service(session: AsyncSession, principal: Principal) -> AnswerService:
    """The answer module, wired to retrieval, the chat adapter and the repository.

    Built here rather than inside the service for the reason `retrieval.py::_service`
    gives: the adapter comes from settings and is the seam a test replaces to record
    whether the model was called. Everything else — the filter, the prompt, the
    persistence — is the module's.
    """
    from app.config import get_settings
    from app.domain.document.embeddings import build_embedder
    from app.domain.retrieval.rerank import build_reranker

    settings = get_settings()
    return AnswerService(
        PostgresAnswerRepository(session),
        RetrievalService(
            PostgresChunkSearchRepository(session),
            embedder=build_embedder(
                settings.embeddings_provider,
                api_key=settings.openai_api_key,
                base_url=settings.openai_base_url,
            ),
            fusion_k=settings.retrieval_fusion_k,
            min_score=settings.retrieval_min_score,
            leg_limit=settings.retrieval_leg_limit,
            reranker=build_reranker(settings.retrieval_reranker),
        ),
        build_chat_model(
            settings.chat_provider_name,
            api_key=settings.openai_api_key,
            model=settings.chat_model,
            base_url=settings.openai_base_url,
            timeout=settings.chat_timeout_seconds,
        ),
        session=session,
    )


@router.post(
    "",
    summary="Ask a question; the answer streams, with citations",
    dependencies=[Depends(ask_questions)],
    response_class=StreamingResponse,
)
async def ask_route(
    body: AskRequest,
    request: Request,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> StreamingResponse:
    """Ask, and receive `text/event-stream`. See `schemas/answer.py` for the frames.

    Nothing is awaited here: the service is an async generator and the response begins as
    soon as its first event exists, which is the `start` frame written *before* retrieval
    runs. That ordering is deliberate and is the first-character-time budget: a client
    knows the message id and can render its own "searching the knowledge base" state while
    the hybrid search is still running, rather than waiting for a body that only begins
    once everything else has finished.
    """
    service = _service(session, principal)
    # Asked once, before the generator starts, so a caller who may not read documents is
    # refused with a status code rather than with a 200 whose stream immediately errors.
    # The service asks the same question again through the same helper; this is the edge
    # making the refusal an HTTP one.
    answer_filter_for(principal)

    events = service.stream(body.question, principal, conversation_id=body.conversation_id)

    async def frames() -> AsyncIterator[str]:
        async for event in events:
            if await request.is_disconnected():  # pragma: no cover - client-dependent
                break
            yield sse_frame(event)

    return StreamingResponse(
        frames(),
        media_type=SSE_MEDIA_TYPE,
        headers={
            # An answer is one person's, grounded in documents they may read. Caching it
            # anywhere shared is how the next reader sees a citation they cannot open.
            "Cache-Control": "no-store, no-cache, must-revalidate",
            # Nginx buffers a proxied response by default, which turns a stream into a
            # twenty-second blank screen. The header is the documented opt-out.
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationRead,
    summary="Read one of your conversations back, with its citations",
    dependencies=[Depends(read_own_conversation)],
)
async def read_conversation_route(
    conversation_id: UUID,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> ConversationRead:
    """The conversation and every message in it, oldest first.

    A conversation that is not the caller's is `ERR_RESOURCE_001` — not a 403 — because
    the two answers are the same to a client that should not be able to tell "not yours"
    from "does not exist". The lookup carries the user id, so ownership is a `WHERE`
    clause rather than a comparison in Python, and there is no spelling of this query that
    omits it.
    """
    repository = PostgresAnswerRepository(session)
    conversation = await repository.load_for(principal.user_id, conversation_id)
    if conversation is None:
        raise AppError(
            ErrorCode.NOT_FOUND,
            detail=f"no conversation {conversation_id} for this caller",
        )
    return _conversation_read(conversation)


def _conversation_read(conversation: StoredConversation) -> ConversationRead:
    return ConversationRead(
        id=conversation.id,
        title=conversation.title,
        created_at=conversation.created_at,
        last_message_at=conversation.last_message_at,
        expires_at=conversation.expires_at,
        messages=[_message_read(message) for message in conversation.messages],
    )


def _message_read(message: StoredMessage) -> MessageRead:
    """One stored message as the API answers it, citations and all.

    The stored citation dicts are re-validated through `CitationRead` rather than returned
    as the raw JSONB: the column is the record, and this is the contract — a shape that
    drifted from the model would otherwise only show up in the client.
    """
    return MessageRead(
        id=message.id,
        question=message.question,
        content=message.content,
        citations=[CitationRead.model_validate(stored) for stored in message.citations],
        model_used=message.model_used,
        provider_used=message.provider_used,
        token_in=message.token_in,
        token_out=message.token_out,
        latency_ms=message.latency_ms,
        is_refusal=message.is_refusal,
        error_key=message.error_key,
        status=message.status,
        retrieval_filter=message.retrieval_filter,
        # §5.2/Q29's personal-document marker travels back with the message (ticket 36),
        # so a conversation read next week renders the banner the stream carried.
        source_notice=source_notice_read(message.source_notice),
        created_at=message.created_at,
    )


__all__ = ["router"]
