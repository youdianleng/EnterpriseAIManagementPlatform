"""Search request and response shapes.

Three conventions, and the first is the ticket's own line about citations:

* **Every hit carries a document, a page range and the text to quote.** §5.2's citation
  format is `《文件名》第 N 页`, so `filename` and `page_from`/`page_to` are response
  fields and not something a client reconstructs. `quote` resolves the parent/child
  question — the child is what matched, the parent is what gets quoted — on the server,
  because a client that had to choose between two nullable columns would eventually
  choose the empty one.

* **The scores are on every hit, in the units they were computed in.** `vector_distance`
  is pgvector's cosine *distance* (lower is better) and `text_rank` is `ts_rank_cd`
  (higher is better); they are deliberately not both called "score", because a client
  that sorted by the wrong direction would look correct and rank backwards.
  `fusion_score` and `rerank_score` are the two the ordering actually came from.

* **`insufficient_evidence` is a 200 with an explicit state**, not an error. §5.2/D20's
  refusal is the honest answer to a question the corpus does not cover, and the scores
  travel with it so the refusal can quote them; the frontend renders its own copy from
  `message_key`, and the API does not invent a sentence in the corpus's language.
"""

from uuid import UUID

from pydantic import BaseModel

from app.domain.retrieval.models import (
    CandidateTrace,
    RankedCandidate,
    RetrievalTrace,
    SearchHit,
    SearchOutcome,
)

#: What a citation's page reads when a chunk names none. A text file or a spreadsheet
#: has no pages, and `null` is the honest value — a client prints "p. ?" or omits the
#: page rather than the server inventing "p. 1".
NO_PAGE = None


class DocumentRefRead(BaseModel):
    """The document a citation points at: `file name + page` needs nothing more.

    `is_company_kb` is carried so the answer's citation can be marked as coming from a
    personal document (Q29's banner). It says which side a document is on, not whether
    the caller may see it — that was decided before the search ran.
    """

    id: UUID
    title: str
    filename: str
    is_company_kb: bool


class SearchHitRead(BaseModel):
    document: DocumentRefRead
    chunk_id: UUID
    page_from: int | None
    page_to: int | None
    heading_path: str | None
    #: `"parent"` or `"child"` — which of the two the quote came from.
    context_scope: str
    #: The child's own text: what the ranking matched.
    content: str
    parent_content: str | None
    #: What a citation shows: the parent's context when there is one.
    quote: str
    vector_distance: float | None
    text_rank: float | None
    vector_rank: int | None
    text_rank_position: int | None
    fusion_score: float
    rerank_score: float


class SearchRead(BaseModel):
    """One search's answer, evidence and all.

    `filtered` is not a diagnostic and is always present: a `false` here means the
    search applied no permission filter, which is the fact ticket 35 exists to make
    impossible to overlook.
    """

    query: str
    hits: list[SearchHitRead]
    insufficient_evidence: bool
    best_score: float
    threshold: float
    filtered: bool
    #: Which halves ran: `["vector", "text"]`, or `["text"]` when the deployment has
    #: no embedding provider — the degradation §5.3 allows, stated rather than implied.
    legs_used: list[str]
    embedder: str | None
    fusion_k: int
    vector_candidates: int
    text_candidates: int


class CandidateTraceRead(BaseModel):
    """One candidate's journey through the pipeline, for the debug view."""

    chunk_id: UUID
    document_id: UUID
    document_title: str
    filename: str
    heading_path: str | None
    page_from: int | None
    page_to: int | None
    content: str
    #: The passage that would have been quoted, so the view shows what the reranker saw.
    quote: str
    vector_rank: int | None
    text_rank_position: int | None
    fusion_score: float
    fusion_rank: int
    rerank_score: float | None
    rerank_rank: int | None
    #: `kept`, `below_threshold` or `outranked`.
    outcome: str
    reason: str


class RetrievalDebugRead(BaseModel):
    """The debug view: both legs, the fusion, the rerank and the answer.

    It is the ordinary search's own result (`outcome`) plus the per-candidate trace, so
    the view cannot disagree with what a user's question returned. What it adds is the
    two lists the request never sees: each leg's top twenty, and *why* every candidate
    that did not make the five did not.
    """

    query: str
    leg_limit: int
    outcome: SearchRead
    #: The vector leg's ranking, best first — the ones the fusion considered.
    vector_leg: list[CandidateTraceRead]
    #: The text leg's ranking, best first.
    text_leg: list[CandidateTraceRead]
    candidates: list[CandidateTraceRead]
    kept: list[CandidateTraceRead]
    dropped: list[CandidateTraceRead]
    filtered: bool
    #: What the permission condition actually was, in the query's own words — ticket 35's
    #: 「检索调试视图中显示本次生效的权限条件」, and the reason it is a string rather than a
    #: nested structure: it is read by a human checking why a document did or did not come
    #: back, and it has to be the *statement* the database ran, not a re-description of
    #: it. `None` means no filter was pushed and the whole corpus was searched.
    filter_explanation: str | None


def _document_read(hit: SearchHit) -> DocumentRefRead:
    return DocumentRefRead(
        id=hit.document.id,
        title=hit.document.title,
        filename=hit.document.filename,
        is_company_kb=hit.document.is_company_kb,
    )


def _hit_read(hit: SearchHit) -> SearchHitRead:
    return SearchHitRead(
        document=_document_read(hit),
        chunk_id=hit.chunk_id,
        page_from=hit.page_from,
        page_to=hit.page_to,
        heading_path=hit.heading_path,
        context_scope=hit.context_scope,
        content=hit.content,
        parent_content=hit.parent_content,
        quote=hit.quote,
        vector_distance=hit.vector_distance,
        text_rank=hit.text_rank,
        vector_rank=hit.vector_rank,
        text_rank_position=hit.text_rank_position,
        fusion_score=hit.fusion_score,
        rerank_score=hit.rerank_score,
    )


def search_read(outcome: SearchOutcome) -> SearchRead:
    """The outcome as the API answers it. The public search's whole response."""
    return SearchRead(
        query=outcome.query,
        hits=[_hit_read(hit) for hit in outcome.hits],
        insufficient_evidence=outcome.insufficient_evidence,
        best_score=outcome.best_score,
        threshold=outcome.threshold,
        filtered=outcome.filtered,
        legs_used=[str(leg) for leg in outcome.legs_used],
        embedder=outcome.embedder,
        fusion_k=outcome.fusion_k,
        vector_candidates=len(outcome.vector_candidates),
        text_candidates=len(outcome.text_candidates),
    )


def _candidate_read(row: CandidateTrace) -> CandidateTraceRead:
    return CandidateTraceRead(
        chunk_id=row.candidate.chunk_id,
        document_id=row.candidate.document_id,
        document_title=row.candidate.document_title,
        filename=row.candidate.filename,
        heading_path=row.candidate.heading_path,
        page_from=row.candidate.page_from,
        page_to=row.candidate.page_to,
        content=row.candidate.content,
        quote=row.candidate.quote,
        vector_rank=row.vector_rank,
        text_rank_position=row.text_rank_position,
        fusion_score=row.fusion_score,
        fusion_rank=row.fusion_rank,
        rerank_score=row.rerank_score,
        rerank_rank=row.rerank_rank,
        outcome=row.outcome,
        reason=row.reason,
    )


def _leg_read(
    leg: tuple[RankedCandidate, ...],
    other: tuple[RankedCandidate, ...],
    reranked: tuple,
    *,
    vector_leg: bool,
    fusion_k: int,
) -> list[CandidateTraceRead]:
    """One leg's top twenty, annotated with the fusion and rerank facts we know.

    The fusion score is recomputed from *this leg's* rank plus the candidate's rank in
    the other leg — the same arithmetic `reciprocal_rank_fusion` performs, applied to
    one row — because the leg listing is the pre-fusion view and a candidate that the
    other leg also found only has its whole score once both ranks are known. A row that
    did not survive the fusion has no rerank score, and the `None` is the view's answer
    for "the fusion dropped it": stated by the absence, not by a zero that looks like a
    bad score.
    """
    ranks_there = {item.chunk_id: item.rank for item in other}
    kept = {row.candidate.chunk_id: row for row in reranked}
    rows: list[CandidateTraceRead] = []
    for item in leg:
        score = 1.0 / (fusion_k + item.rank)
        if item.chunk_id in ranks_there:
            score += 1.0 / (fusion_k + ranks_there[item.chunk_id])
        reranked_row = kept.get(item.chunk_id)
        fate = "kept for reranking" if reranked_row is not None else "outside the reranked window"
        rows.append(
            CandidateTraceRead(
                chunk_id=item.chunk_id,
                document_id=item.document_id,
                document_title=item.document_title,
                filename=item.filename,
                heading_path=item.heading_path,
                page_from=item.page_from,
                page_to=item.page_to,
                content=item.content,
                quote=item.quote,
                vector_rank=item.rank if vector_leg else ranks_there.get(item.chunk_id),
                text_rank_position=item.rank if not vector_leg else ranks_there.get(item.chunk_id),
                fusion_score=score,
                # 0 is a placeholder here and only here: the leg listing is pre-fusion,
                # so a row's *fused* position is not a fact this view has. `candidates`
                # below is where the fused order is reported.
                fusion_rank=0,
                rerank_score=reranked_row.score if reranked_row is not None else None,
                rerank_rank=None,
                outcome="kept" if reranked_row is not None else "below_top_n",
                reason=f"rank {item.rank} in this leg; the fusion {fate}",
            )
        )
    return rows


def debug_read(trace: RetrievalTrace) -> RetrievalDebugRead:
    """The trace as the API answers it."""
    outcome = trace.outcome
    reranked = outcome.reranked
    return RetrievalDebugRead(
        query=outcome.query,
        leg_limit=trace.leg_limit,
        outcome=search_read(outcome),
        vector_leg=_leg_read(
            outcome.vector_candidates,
            outcome.text_candidates,
            reranked,
            vector_leg=True,
            fusion_k=outcome.fusion_k,
        ),
        text_leg=_leg_read(
            outcome.text_candidates,
            outcome.vector_candidates,
            reranked,
            vector_leg=False,
            fusion_k=outcome.fusion_k,
        ),
        candidates=[_candidate_read(row) for row in trace.candidates],
        kept=[_candidate_read(row) for row in trace.kept],
        dropped=[_candidate_read(row) for row in trace.dropped],
        filtered=outcome.filtered,
        filter_explanation=trace.filter_explanation,
    )


__all__ = [
    "CandidateTraceRead",
    "DocumentRefRead",
    "RetrievalDebugRead",
    "SearchHitRead",
    "SearchRead",
    "debug_read",
    "search_read",
]
