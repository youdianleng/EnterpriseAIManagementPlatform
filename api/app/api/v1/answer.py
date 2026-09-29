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

**The other three verbs are the sidebar's** (ticket 37): `GET /answers/conversations`
lists the caller's conversations, `PATCH /answers/conversations/{id}` renames one and
`DELETE /answers/conversations/{id}` removes one from the caller's view. All four carry the
same `user_id` in the `WHERE`, so "which conversations exist for this caller" is answered
in one place — the repository — rather than four times here.

**Deleting is a flag, and the interface says so rather than implying the row is gone.**
`deleted_by_user` is §3.6's column and D18's 「用户可自行删除」: the conversation stops being
listed, stops being readable and stops being appendable *immediately*, and the row itself is
removed later by the 90-day sweep (ticket 51, which does not exist yet). The route therefore
answers 204 and promises nothing about the bytes on disk; the ticket file records what
"deleted" means until the sweep ships.

**Why `session.read_own` guards the writes too.** The catalogue has one action for a
conversation, because a conversation is a surface that answers about the caller: D18 gives
it to its user and to nobody else, and every role may ask questions. A `session.manage_own`
would be a second catalogue entry whose role list is *identical* to this one's — which is a
rule duplicated rather than a rule expressed. The ownership refusal is the repository's
`WHERE` clause, and `tests/test_answer.py` asserts it by name.
"""

from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import current_principal, db_session, require
from app.api.v1.schemas.answer import (
    SSE_MEDIA_TYPE,
    AskRequest,
    CitationRead,
    ConversationPageRead,
    ConversationRead,
    ConversationRenameRequest,
    ConversationSummaryRead,
    DraftRead,
    MessageRead,
    PrefillFieldRead,
    PrefillFormRead,
    source_notice_read,
    sse_frame,
)
from app.audit import AuditAction, record
from app.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.domain.access import Action, Principal
from app.domain.access.kernel import ResourceKind
from app.domain.agent.models import AgentAction
from app.domain.agent.service import service_for
from app.domain.answer.chat import build_chat_model
from app.domain.answer.driver import AnswerService
from app.domain.answer.repository import ConversationRead as StoredConversation
from app.domain.answer.repository import ConversationSummary as StoredSummary
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

    # And asked once more for the conversation being continued, for the same reason and
    # with the same shape. A conversation that is somebody else's — or one its owner has
    # deleted, which ticket 37 made an ordinary state — is a 404 here, where it can still
    # be a status code. Inside the generator it could only be an `error` frame after a 200,
    # which is a worse answer to the same question. `driver.stream` still handles the race
    # (the row can be deleted between this check and the insert) by yielding that frame.
    if body.conversation_id is not None:
        repository = PostgresAnswerRepository(session)
        if not await repository.owns(principal.user_id, body.conversation_id):
            raise AppError(
                ErrorCode.NOT_FOUND,
                detail=f"no conversation {body.conversation_id} for this caller",
            )

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

    **And the conversation's newest draft, if it has one** (ticket 40). A draft the assistant
    proposed is *part of* what the thread holds: §6.3 requires it to be found again after a
    refresh or a restart, and the client that draws the transcript is the client that draws
    the form. Reading it here rather than through an endpoint of its own keeps one ownership
    rule — the same `session.read_own` guard, the same `WHERE user_id` — and one request.
    `latest_draft` also records a lapsed draft as `expired` before answering, which is
    §6.3's 「过期后 `status=expired`」 happening where the fact is first observed.
    """
    repository = PostgresAnswerRepository(session)
    conversation = await repository.load_for(principal.user_id, conversation_id)
    if conversation is None:
        raise AppError(
            ErrorCode.NOT_FOUND,
            detail=f"no conversation {conversation_id} for this caller",
        )
    draft = await service_for(
        session, ttl_hours=get_settings().agent_draft_ttl_hours
    ).latest_draft(user_id=principal.user_id, conversation_id=conversation_id)
    return _conversation_read(conversation, draft)


@router.get(
    "/conversations",
    response_model=ConversationPageRead,
    summary="List your conversations, most recently used first",
    dependencies=[Depends(read_own_conversation)],
)
async def list_conversations_route(
    limit: int = Query(default=50, ge=1, le=200),
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> ConversationPageRead:
    """The caller's own conversations, without the ones they deleted.

    No parameter can widen it. The user id comes from the session and goes into the
    statement; there is no `user_id` in the query string, and no role reads somebody
    else's list — §5.3 gives the transcript to compliance, and that surface is ticket 48's.
    That is also the checklist line this endpoint answers in the negative: there is no HR
    entry here, and this route's response for an HR reader is their *own* conversations
    and nothing else.
    """
    repository = PostgresAnswerRepository(session)
    rows = await repository.list_for(principal.user_id, limit=limit)
    return ConversationPageRead(
        items=[_summary_read(row) for row in rows],
        total=await repository.count_for(principal.user_id),
    )


@router.patch(
    "/conversations/{conversation_id}",
    response_model=ConversationSummaryRead,
    summary="Rename one of your conversations",
    dependencies=[Depends(read_own_conversation)],
)
async def rename_conversation_route(
    conversation_id: UUID,
    body: ConversationRenameRequest,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> ConversationSummaryRead:
    """A new title, or `ERR_RESOURCE_001` when the conversation is not the caller's.

    The same 404 as the read, for the same reason: a distinct "that one is somebody
    else's" would let this endpoint be asked which conversation ids exist. The `UPDATE`
    carries the user id and returns what it stored, so the two cases are decided by the
    database rather than by a comparison here — and the response describes the title that
    was actually written, not the one the request asked for.

    **`commit` after the statement is the whole of the durability**, and the row is not
    read a second time: `RETURNING` already produced it. A commit before a read-back would
    end the transaction-scoped permission context and the read would find nothing — the
    symptom ticket 34 recorded when it committed in the middle of the answer path.
    """
    repository = PostgresAnswerRepository(session)
    summary = await repository.rename_for(principal.user_id, conversation_id, body.title)
    if summary is None:
        raise AppError(
            ErrorCode.NOT_FOUND,
            detail=f"no conversation {conversation_id} for this caller",
        )
    await repository.commit()
    return _summary_read(summary)


@router.delete(
    "/conversations/{conversation_id}",
    status_code=204,
    summary="Delete one of your conversations; it disappears from your view at once",
    dependencies=[Depends(read_own_conversation)],
)
async def delete_conversation_route(
    conversation_id: UUID,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> Response:
    """Set `deleted_by_user`, which is immediate and one-way from the caller's side.

    **204 with no body**, because there is nothing to describe: the conversation is gone
    from this caller's list, its messages are unreachable through it, and a later question
    naming it is refused like anybody else's. The row survives for D18's retention and for
    the compliance read of §5.3 — see the module docstring, and ticket 51 for the sweep
    that eventually removes it.

    A conversation that is not the caller's, or that they have already deleted, is
    `ERR_RESOURCE_001`: the second call comes from a client with a stale list, and
    answering 204 would agree that something happened when nothing did.

    **The act is audited, and the rename is not.** A rename changes a label its owner alone
    reads; a delete changes what the stored row *means* — a compliance reader looking at it
    afterwards has to be able to tell "the owner removed this from their list" from "nobody
    ever touched it". The entry records who and when, no title and no text, in the same
    transaction as the flag, exactly as `conversation.asked` does for a question.
    """
    repository = PostgresAnswerRepository(session)
    removed = await repository.delete_for(principal.user_id, conversation_id)
    if not removed:
        raise AppError(
            ErrorCode.NOT_FOUND,
            detail=f"no conversation {conversation_id} for this caller",
        )
    await record(
        session,
        action=AuditAction.CONVERSATION_DELETED,
        entity_type="rag_conversation",
        entity_id=conversation_id,
        after={"deleted_by_user": True},
        reason="the owner removed the conversation from their own view (§3.6)",
    )
    await repository.commit()
    return Response(status_code=204)


def _summary_read(summary: StoredSummary) -> ConversationSummaryRead:
    return ConversationSummaryRead(
        id=summary.id,
        title=summary.title,
        created_at=summary.created_at,
        last_message_at=summary.last_message_at,
        expires_at=summary.expires_at,
    )


def _conversation_read(
    conversation: StoredConversation, draft: AgentAction | None = None
) -> ConversationRead:
    return ConversationRead(
        id=conversation.id,
        title=conversation.title,
        created_at=conversation.created_at,
        last_message_at=conversation.last_message_at,
        expires_at=conversation.expires_at,
        messages=[_message_read(message) for message in conversation.messages],
        draft=None if draft is None else _draft_read(draft),
    )


def _draft_read(draft: AgentAction) -> DraftRead:
    """One recorded draft as the API answers it: the form, its dates, and what became of it.

    `PrefillForm.from_stored` rather than the raw JSONB, for the reason `_message_read`
    re-validates a stored citation: the column is the record and this is the contract, so a
    shape that drifted fails in the API rather than in a browser that cannot draw a field.

    The three outcome fields come straight off the row ticket 41 writes: `confirmed_at` is the
    instant a person answered (either way), and `resulting_entity_type` / `_id` are the
    document a confirmation created — the pair the migration's 「要么都有要么都没有」 constraint
    keeps together. They are what lets the card say "this is now a leave request" and link to
    it without a second request or a client-side guess.
    """
    form = draft.form
    return DraftRead(
        id=draft.id,
        tool_name=draft.tool_name,
        status=str(draft.status),
        created_at=draft.created_at,
        expires_at=draft.expires_at,
        confirmed_at=draft.confirmed_at,
        resulting_entity_type=draft.resulting_entity_type,
        resulting_entity_id=draft.resulting_entity_id,
        prefill_form=(
            None
            if form is None
            else PrefillFormRead(
                tool=form.tool,
                entity=str(form.entity),
                title_key=form.title_key,
                title_es=form.title_es,
                title_en=form.title_en,
                submit_path=form.submit_path,
                fields=[
                    PrefillFieldRead(**field.as_dict()) for field in form.fields
                ],
                facts=dict(form.facts),
            )
        ),
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
