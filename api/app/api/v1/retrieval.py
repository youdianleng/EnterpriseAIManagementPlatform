"""Retrieval endpoints: the search, and the debug view.

Two routes, and they answer two different people:

**`GET /retrieval/search`** is what a question runs. It is guarded by
`document.read`, which is the action the caller needs in order to read a document at
all — *which* documents is §4.2's question, and this ticket deliberately does not answer
it: the service takes a `FilterSpec` and applies it in the same SQL, and until ticket 35
pushes a real one the response says `filtered: false` out loud. That is the shape the
ticket asks for (「本工单只保证接口留出了过滤入口」) with the one addition that makes it
safe to ship: a caller cannot miss the fact that no filter was applied.

**`GET /retrieval/debug`** is the authorised view the ticket asks for
(「仅授权角色可见」), and "authorised" is a catalogue entry rather than a hard-coded
role list: `retrieval.debug`, held by administration and HR — the two roles that own
the knowledge base and can act on "why did this not come back". It shows both legs'
top twenty, the fusion's arithmetic, the reranker's contribution and every candidate
that was dropped, with the reason. It is the *same* run the ordinary search makes, so
the view cannot disagree with what a user's question returned.

**`legs_used` is always reported, and `filtered` always too.** A deployment with no
embedding provider answers from full text alone; a caller that did not push a filter
searches the whole corpus. Both are legitimate states and neither may be left for the
reader to infer from a signature, which is why they are response fields.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import current_principal, db_session, require
from app.api.v1.schemas.retrieval import SearchRead, debug_read, search_read
from app.domain.access import Action, Principal
from app.domain.access.kernel import ResourceKind
from app.domain.retrieval.models import DEFAULT_LIMIT, MAX_LIMIT
from app.domain.retrieval.service import RetrievalService
from app.repositories.retrieval import PostgresChunkSearchRepository

router = APIRouter(prefix="/retrieval", tags=["retrieval"])

#: Everyone who may read a document may search. The role guard is the same action the
#: document list uses, and the *reach* is the `FilterSpec` the service applies — never a
#: second rule here.
search_documents = require(Action.DOCUMENT_READ, ResourceKind.DOCUMENT)

#: The debug view, administration's and HR's. Its own action so the catalogue says who
#: may read the corpus's internals, rather than a role test in this file.
debug_retrieval = require(Action.RETRIEVAL_DEBUG, ResourceKind.ACCOUNT)


def _service(session: AsyncSession) -> RetrievalService:
    """The module, wired to its repository, its embedder and its reranker.

    The embedder comes from settings and is the same seam ticket 32 built — `fake`
    outside production, `openai` with a key, `none` for a deployment whose vectors are
    not ready. A `none` deployment is not a broken one: the text leg answers, and
    `legs_used` reports `["text"]`.

    The `FilterSpec` is deliberately *not* built here. Ticket 35 owns the principal-to-
    spec translation for retrieval, and a route that built one now would be the second
    place §4.2 is decided.
    """
    from app.config import get_settings
    from app.domain.document.embeddings import build_embedder
    from app.domain.retrieval.rerank import build_reranker

    settings = get_settings()
    return RetrievalService(
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
    )


@router.get(
    "/search",
    response_model=SearchRead,
    summary="Hybrid search: vector and full text, fused, reranked",
    dependencies=[Depends(search_documents)],
)
async def search_route(
    q: str = Query(min_length=1, max_length=2000, description="The question, in any language"),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> SearchRead:
    """The fused, reranked top `limit`, or an explicit "no basis".

    A 200 with `insufficient_evidence: true` when the best score is below the
    threshold: §5.2/D20's refusal is the honest answer to a question the corpus does
    not cover, and it is not an error — nothing about the request was wrong. The
    scores travel with it so the caller can see how close it was.
    """
    return search_read(await _service(session).search(q, limit=limit))


@router.get(
    "/debug",
    summary="Why a question retrieved what it did, and what it missed",
    dependencies=[Depends(debug_retrieval)],
)
async def debug_route(
    q: str = Query(min_length=1, max_length=2000),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> dict:
    """Both legs' top twenty, the fusion, the rerank and the five, for one query.

    Returns a plain dict rather than a response model because the body is the debug
    view's own shape and it is read by an operator rather than parsed by a client; the
    envelope and the permission are what this route owes, and `debug_read` owns the
    shape.
    """
    trace = await _service(session).explain(q, limit=limit)
    return debug_read(trace).model_dump(mode="json")


__all__ = ["router"]
