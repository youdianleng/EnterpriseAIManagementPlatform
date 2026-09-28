"""Hybrid retrieval: two legs, reciprocal-rank fusion, a rerank, and a threshold.

`search(query, *, filter_spec=None, limit=5)` is the single public verb; everything
else in the package is a part of it — the two legs, the fusion rule, the reranker
seam, the "no basis" state — and each is importable because a test asserts it
directly, not because a caller is expected to reach for it.

    service.search("¿Cuántos días de vacaciones?", filter_spec=spec)

**Where it lives, and why not `ai/rag/`.** `docs/architecture/codebase-design.md` §1
sketches retrieval under `ai/rag/`, and that sketch predates the module it has
actually become: this is SQL over `document_chunks`, which is ticket 31/32's table and
`domain/document`'s module, and constraint A forbids `domain/` from importing `ai/`,
not the reverse. Filing it in `domain/retrieval/` keeps the retrieval a caller of the
document module and of the kernel — both of which it must be, in the same query —
rather than a layer above them. It holds no LLM library and no write repository, so
constraints A and B are satisfied where the code is.

**What this module deliberately does not do.** It does not decide who the caller is.
It takes a `FilterSpec` when one is given and applies it inside the same SQL as the
retrieval; when none is given the search runs unfiltered and the outcome carries
`filtered=False`, so a caller cannot mistake an unfiltered search for a filtered one.
Ticket 35 pushes a spec produced by `access.kernel.filter_for`; this module is the
entry point that ticket needs, and inventing a permission rule here would be the
second implementation of §4.2 the design forbids.
"""

from app.domain.retrieval.errors import require_limit, require_query
from app.domain.retrieval.fusion import fusion_rank_of, reciprocal_rank_fusion
from app.domain.retrieval.models import (
    DEFAULT_FUSION_K,
    DEFAULT_LEG_LIMIT,
    DEFAULT_LIMIT,
    CandidateTrace,
    DocumentRef,
    FusedCandidate,
    RankedCandidate,
    RankedHit,
    RerankBreakdown,
    RetrievalLeg,
    RetrievalTrace,
    RrfLeg,
    SearchHit,
    SearchOutcome,
)
from app.domain.retrieval.repository import ChunkSearchRepository
from app.domain.retrieval.rerank import LexicalReranker, Reranker, build_reranker
from app.domain.retrieval.service import RetrievalService

__all__ = [
    "DEFAULT_FUSION_K",
    "DEFAULT_LEG_LIMIT",
    "DEFAULT_LIMIT",
    "CandidateTrace",
    "ChunkSearchRepository",
    "DocumentRef",
    "FusedCandidate",
    "LexicalReranker",
    "RankedCandidate",
    "RankedHit",
    "RerankBreakdown",
    "Reranker",
    "RetrievalLeg",
    "RetrievalService",
    "RetrievalTrace",
    "RrfLeg",
    "SearchHit",
    "SearchOutcome",
    "build_reranker",
    "fusion_rank_of",
    "reciprocal_rank_fusion",
    "require_limit",
    "require_query",
]
