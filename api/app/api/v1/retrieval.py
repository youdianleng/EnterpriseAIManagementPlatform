"""Retrieval endpoints: the search, and the debug view.

Two routes, and they answer two different people:

**`GET /retrieval/search`** is what a question runs. It is guarded by
`document.read`, which is the action the caller needs in order to read a document at
all, and *which* documents is §4.2's question — answered by
`domain/retrieval/filtering.py::answer_filter_for`, the shared helper ticket 34 built
and ticket 35 pushes here. The spec travels into `RetrievalService.search` and is
rendered into the `WHERE` of **both** legs of the *same* SQL statement, so a document
this caller may not open is never ranked, never fetched and never counted. That is
`docs/architecture/codebase-design.md` constraint C, and it is why the response's
`filtered` is `true` on this path and never `false`: **there is no request that
searches the whole corpus any more.** Ticket 33 shipped this route with
`filtered: false` out loud (「本工单只保证接口留出了过滤入口」) so the gap could not be
overlooked; ticket 35 closes it.

**`GET /retrieval/debug`** is the authorised view the ticket asks for
(「仅授权角色可见」), and "authorised" is a catalogue entry rather than a hard-coded
role list: `retrieval.debug`, held by administration and HR — the two roles that own
the knowledge base and can act on "why did this not come back". It shows both legs'
top twenty, the fusion's arithmetic, the reranker's contribution and every candidate
that was dropped, with the reason. It is the *same* run the ordinary search makes, so
the view cannot disagree with what a user's question returned — including about the
filter: it pushes the *same* spec, through the same helper, and reports the predicate
the database ran (「调试视图中显示本次生效的权限条件」).

**`legs_used` is always reported, and `filtered` always too.** A deployment with no
embedding provider answers from full text alone; that is a legitimate state and it is
not left for the reader to infer from a signature. `filtered` is kept on the response
for the same reason the outcome carries it: the *service* can still be driven
unfiltered — the offline evaluation does, deliberately, through the named
`unfiltered()` — and a field that says whether a predicate was pushed is what makes
that difference visible rather than a matter of which call site ran.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import current_principal, db_session, require
from app.api.v1.schemas.retrieval import SearchRead, debug_read, search_read
from app.domain.access import Action, Principal
from app.domain.access.kernel import ResourceKind
from app.domain.retrieval.filtering import answer_filter_for
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

    The `FilterSpec` is deliberately *not* built here either, and ticket 35 does not
    build it at the route: it comes from `answer_filter_for(principal)`, the one
    principal-to-spec translation, so this module and the answer path cannot decide
    §4.2 differently. The route passes what the helper produced straight through.
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

    **The permission condition is a parameter of the search, not a step after it.**
    `answer_filter_for` is asked once here and handed to the service, which pushes it
    into both legs of the one statement that ranks. A route that called `search()`
    without it would be a route that searched the whole corpus — which is the failure
    this line exists to make impossible to write by accident, and the escalation suite
    pins it from the hit set.
    """
    spec = answer_filter_for(principal)
    return search_read(await _service(session).search(q, filter_spec=spec, limit=limit))


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

    **It runs the same filtered search, on purpose.** `retrieval.debug` is
    administration's and HR's, and neither role reaches another department's documents
    by being privileged: §4.2's clauses are the whole rule for a document, so the view
    pushes the *caller's* spec through the same helper the search uses. A view that
    searched unfiltered would show a reviewer passages the search cannot return, and
    「为什么没检索到」 would then have two different answers depending on who asked. The
    predicate it used travels back as `filter_explanation`, which is the review the
    ticket asks for.
    """
    spec = answer_filter_for(principal)
    trace = await _service(session).explain(q, filter_spec=spec, limit=limit)
    return debug_read(trace).model_dump(mode="json")


__all__ = ["router"]
