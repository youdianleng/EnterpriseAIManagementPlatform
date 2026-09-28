"""The retrieval service: one query in, five cited passages or an explicit "no basis".

`search()` is the only public verb. What it does, in the design's order (§5.2):

    query → embed → both legs (top 20 each) → RRF → rerank → threshold → top 5

Five decisions worth reading before the code:

* **The threshold is on the reranked score, not on the fusion score.** §5.2 says
  「阈值判定：最高分 < threshold → 返回未找到依据」 without saying which score, and the
  reranked one is the honest reading: the fused top has already been reordered by the
  stage that decides what the top five are, so gating on the pre-rerank score would let
  a candidate the reranker demoted to last place pass a threshold the winner failed.
  `rerank.rerank` states the consequences of that choice; `DEFAULT_MIN_SCORE` is a
  setting because the right number is a corpus's, not this module's.

* **"No basis" is a state on a successful outcome, not an exception.** `hits` is empty
  and `insufficient_evidence` is true, with the scores still attached, so a caller
  renders the ordinary answer (ticket 34's refusal) without a `try`, and a test can pin
  the boundary from both sides. The alternative — returning the weak top five and
  letting the generation step notice — is exactly the failure the ticket forbids.

* **An unfiltered search is possible and it says so.** `filter_spec=None` runs with no
  permission predicate at all, and since ticket 35 that is *only* right for the offline
  evaluation and the system's own pass, both of which mean it and say so through
  `filtering.unfiltered()`. Every request path pushes the caller's spec — the two
  retrieval routes and the answer path all go through
  `filtering.answer_filter_for` — so `None` here is a deliberate act rather than a
  default anybody can reach by omission. Rather than making it impossible, the outcome
  carries `filtered`, because a retrieval which quietly forgot the filter must not be
  able to look like one that applied it.

* **An embedding failure loses the vector leg; it does not fail the search.**
  `EmbeddingUnavailable` — no key, a revoked one, a rate limit — is caught here and the
  text leg answers alone, with `legs_used == (TEXT,)`. The design's §5.3 degradation
  chain is the reason: "only errors and timeouts trigger degradation", and a hybrid
  search whose vector half is missing still answers better than a 503. What it must not
  do is *pretend*: `legs_used` and `embedder` record exactly what ran.

* **The debug view is the same run.** `explain()` calls `search()` once and annotates
  its result; it does not search again. A second search could return a different top
  five — another writer, a different plan — and a "why was this not retrieved?" answer
  that disagrees with what was retrieved is worse than no debug view at all.
"""

from uuid import UUID

from app.domain.access.kernel import FilterSpec
from app.domain.document.embeddings import Embedder, EmbeddingUnavailable
from app.domain.retrieval import fusion as fusion_module
from app.domain.retrieval.errors import require_limit, require_query
from app.domain.retrieval.models import (
    DEFAULT_FUSION_K,
    DEFAULT_LEG_LIMIT,
    DEFAULT_LIMIT,
    CandidateTrace,
    DocumentRef,
    RankedCandidate,
    RankedHit,
    RetrievalLeg,
    RetrievalTrace,
    SearchHit,
    SearchOutcome,
)
from app.domain.retrieval.repository import ChunkSearchRepository
from app.domain.retrieval.rerank import LexicalReranker, Reranker
from app.logging import get_logger

logger = get_logger(__name__)

#: How sure a search has to be before its answer is worth generating from. 0.35 on
#: the reranker's `[0, 1]` scale is a defensible starting point rather than a measured
#: one: a passage that shares no term with the question and is not in the fusion's top
#: places scores below it, and one that leads a leg *and* answers the question scores
#: well above. The right value for a corpus is measured with
#: `tests/tools/eval_retrieval.py`, which reports hit-rate@5 per mode; the setting is
#: `retrieval_min_score` so that measurement can move it without a code change.
DEFAULT_MIN_SCORE = 0.35


class RetrievalService:
    """Hybrid search for one caller's corpus.

    No `Principal`, deliberately. This module is the *entry point* ticket 35 pushes a
    filter into, and a service built around a principal would have to decide what a
    principal may reach — the kernel's job, in the kernel, once. What the service owes
    the caller instead is `filtered` on every outcome.
    """

    def __init__(
        self,
        repository: ChunkSearchRepository,
        *,
        embedder: Embedder | None = None,
        fusion_k: int = DEFAULT_FUSION_K,
        min_score: float = DEFAULT_MIN_SCORE,
        leg_limit: int = DEFAULT_LEG_LIMIT,
        reranker: Reranker | None = None,
    ) -> None:
        self._repository = repository
        self._embedder = embedder
        self._fusion_k = fusion_k
        self._min_score = min_score
        self._leg_limit = leg_limit
        # A `Reranker` is required and a default is supplied, rather than the parameter
        # being optional and the stage being skipped: a search that could run without a
        # rerank would make the §5.2 pipeline a configuration, and the seam a thing
        # nobody exercises. A test passes its own to prove this one is used.
        self._reranker: Reranker = reranker if reranker is not None else LexicalReranker()

    @property
    def reranker(self) -> Reranker:
        """The adapter in use, for the debug view and for the seam's test."""
        return self._reranker

    async def search(
        self,
        query: str,
        *,
        filter_spec: FilterSpec | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> SearchOutcome:
        """Both legs, fused, reranked, thresholded. See the module docstring."""
        cleaned = require_query(query)
        wanted = require_limit(limit)

        vector, text, embedding, embedder_name = await self._legs(cleaned, filter_spec)
        fused = fusion_module.reciprocal_rank_fusion(
            fusion_module.by_chunk(vector), fusion_module.by_chunk(text), k=self._fusion_k
        )
        reranked = self._reranker.rerank(cleaned, fused[: self._leg_limit])
        best = reranked[0].score if reranked else 0.0
        below = best < self._min_score

        return SearchOutcome(
            query=cleaned,
            hits=() if below else tuple(self._hit(item) for item in reranked[:wanted]),
            vector_candidates=tuple(vector),
            text_candidates=tuple(text),
            fused=tuple(fused),
            reranked=tuple(reranked),
            threshold=self._min_score,
            best_score=best,
            insufficient_evidence=below,
            filtered=filter_spec is not None,
            legs_used=_legs_used(embedding),
            embedder=embedder_name,
            fusion_k=self._fusion_k,
        )

    async def explain(
        self,
        query: str,
        *,
        filter_spec: FilterSpec | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> RetrievalTrace:
        """The same search, with every candidate's journey recorded.

        For the authorised debug view, which exists to answer 「为什么没检索到」: a
        candidate's outcome says which of the four things happened to it — it was kept,
        the fusion never put it in the reranked window, the reranker pushed it out, or
        the whole search came back below the threshold. Those are the four, and a view
        that could only say "not in the results" would send the reader to the logs.
        """
        outcome = await self.search(query, filter_spec=filter_spec, limit=limit)
        kept = {hit.chunk_id for hit in outcome.hits}
        return RetrievalTrace(
            outcome=outcome,
            candidates=tuple(
                self._trace(item, position, outcome, kept)
                for position, item in enumerate(outcome.reranked, start=1)
            ),
            leg_limit=self._leg_limit,
            # The predicate the run actually applied, rendered from the same spec the
            # repository rendered, so the view says what happened rather than what was
            # meant — which is the point of a view for 「为什么没检索到」.
            filter_explanation=_filter_explanation(filter_spec),
        )

    # --- internals ----------------------------------------------------------

    async def _legs(
        self, query: str, filter_spec: FilterSpec | None
    ) -> tuple[list[RankedCandidate], list[RankedCandidate], list[float] | None, str | None]:
        """Embed the query, then ask for both legs in one repository call.

        The embedding is the one thing that cannot be deferred into SQL — a query
        vector has to come from a model — so it is the one thing that can fail on its
        own. Its failure is caught here and turned into "there is no vector leg",
        which is the degradation §5.3 asks for rather than an error.
        """
        embedding: list[float] | None = None
        embedder_name: str | None = None
        if self._embedder is not None:
            embedder_name = self._embedder.name
            try:
                vectors = await self._embedder.embed([query])
            except EmbeddingUnavailable as error:
                logger.warning(
                    "retrieval_embedding_unavailable",
                    embedder=embedder_name,
                    detail=str(error),
                )
            else:
                embedding = vectors[0] if vectors else None

        vector, text = await self._repository.search_legs(
            query,
            embedding=embedding,
            leg_limit=self._leg_limit,
            filter_spec=filter_spec,
        )
        return vector, text, embedding, embedder_name

    def _hit(self, item: RankedHit) -> SearchHit:
        candidate = item.candidate
        return SearchHit(
            document=DocumentRef(
                id=candidate.document_id,
                title=candidate.document_title,
                filename=candidate.filename,
                is_company_kb=candidate.is_company_kb,
            ),
            chunk_id=candidate.chunk_id,
            page_from=candidate.page_from,
            page_to=candidate.page_to,
            heading_path=candidate.heading_path,
            context_scope=candidate.context_scope,
            content=candidate.content,
            parent_content=candidate.parent_content,
            quote=candidate.quote,
            vector_distance=candidate.vector_distance,
            text_rank=candidate.text_rank,
            vector_rank=item.fused.rank_in(RetrievalLeg.VECTOR),
            text_rank_position=item.fused.rank_in(RetrievalLeg.TEXT),
            fusion_score=item.fused.fusion_score,
            rerank_score=item.score,
        )

    def _trace(
        self,
        item: RankedHit,
        position: int,
        outcome: SearchOutcome,
        kept: set[UUID],
    ) -> CandidateTrace:
        candidate = item.candidate
        chunk_id = candidate.chunk_id
        in_fused = fusion_module.fusion_rank_of(outcome.fused, chunk_id)
        return CandidateTrace(
            candidate=candidate,
            vector_rank=item.fused.rank_in(RetrievalLeg.VECTOR),
            text_rank_position=item.fused.rank_in(RetrievalLeg.TEXT),
            fusion_score=item.fused.fusion_score,
            fusion_rank=in_fused or position,
            rerank_score=item.score,
            rerank_rank=position,
            outcome=_outcome_of(chunk_id, kept, outcome),
            reason=_reason_of(
                chunk_id,
                position,
                kept,
                outcome,
                self._leg_limit,
                in_fused,
            ),
        )


def _outcome_of(chunk_id: UUID, kept: set[UUID], outcome: SearchOutcome) -> str:
    """Which of the four things happened to a candidate. See `RetrievalTrace`.

    `below_top_n` is the fusion's doing — the candidate was reranked, but the fusion
    had already put it past the window the reranker was given, so no rerank score
    could have saved it. `outranked` is the reranker's: it saw the candidate and
    scored it below the ones that were returned. The two want different investigations.
    """
    if chunk_id in kept:
        return "kept"
    if outcome.insufficient_evidence:
        return "below_threshold"
    return "outranked"


def _reason_of(
    chunk_id: UUID,
    position: int,
    kept: set[UUID],
    outcome: SearchOutcome,
    leg_limit: int,
    fusion_rank: int | None,
) -> str:
    if chunk_id in kept:
        return f"selected at position {position} of {len(outcome.hits)}"
    if outcome.insufficient_evidence:
        return (
            f"the best score {outcome.best_score:.3f} is below the threshold "
            f"{outcome.threshold:.3f}, so nothing was returned at all (D20)"
        )
    if fusion_rank is None or fusion_rank > leg_limit:
        return (
            f"fused at {fusion_rank}, outside the top {leg_limit} the reranker was "
            "given; no rerank score could have recovered it"
        )
    if not outcome.vector_candidates:
        return (
            f"reranked to {position}, outside the returned {len(outcome.hits)}; the "
            "vector leg did not run, so this candidate came from full text alone"
        )
    return (
        f"reranked to {position}, outside the returned {len(outcome.hits)}: the "
        "reranker scored it below the passages it kept (fusion prior "
        f"{outcome.reranked[position - 1].breakdown.prior:.2f}, lexical overlap "
        f"{outcome.reranked[position - 1].breakdown.term_coverage:.2f})"
    )


def _legs_used(embedding: list[float] | None) -> tuple[RetrievalLeg, ...]:
    """Which halves of the hybrid could run. Two values, never one of them missing.

    Written as a tuple rather than two booleans because it is what the answer reports
    and what a test asserts on: `(VECTOR, TEXT)` is a hybrid, `(TEXT,)` is the
    degradation, and a search that silently reported "hybrid" while running one leg
    would be the kind of claim this ticket exists to make checkable.
    """
    return (RetrievalLeg.VECTOR, RetrievalLeg.TEXT) if embedding else (RetrievalLeg.TEXT,)


def _filter_explanation(spec: FilterSpec | None) -> str | None:
    """The spec as the sentence a reviewer reads, or `None` for an unfiltered run.

    Rendered by the repository's own translation rather than by a second description
    here: the point of showing it is that it is *the* predicate, and a paraphrase that
    drifted from the SQL would send a reviewer to the wrong place. Ticket 35 decides
    which spec a request pushes; this is where that spec becomes visible.
    """
    from app.repositories.retrieval import visible_document_predicate

    if spec is None:
        return None
    predicate, parameters = visible_document_predicate(spec)
    if not predicate:  # pragma: no cover - `visible_document_predicate` never omits one
        return None
    binds = ", ".join(
        f"{name}={value!r}" for name, value in sorted(parameters.items())
    )
    return f"{predicate}  --  {binds}"


__all__ = ["DEFAULT_MIN_SCORE", "RetrievalService"]
