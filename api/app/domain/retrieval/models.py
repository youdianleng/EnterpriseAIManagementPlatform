"""What a hybrid search is made of, as values.

Seven decisions, and every one of them is about making the pipeline readable from
its own output rather than from its code:

* **A leg is named by an enum, not by a string or an integer.** A result says *which
  half* found it — `VECTOR`, `TEXT` or both — and the debug view renders exactly
  that. A bare number would be a second place to get the order of the two wrong.

* **A rank is 1-based and starts at 1, deliberately.** Reciprocal-rank fusion is
  `1 / (k + rank)`, and an off-by-one here is a silent, uniform shift of every score
  — the kind of mistake that leaves a ranking plausible and the arithmetic wrong.
  `RrfLeg` therefore also carries `contribution`, so the fusion test can assert the
  term each leg put in rather than re-deriving `1 / (k + rank)` and agreeing with
  itself.

* **The candidate carries the raw score each leg produced as well as its rank**: the
  cosine distance, the `ts_rank_cd`. The rank is what the fusion uses; the raw score
  is what a reader of the debug view needs to see *why* the rank came out that way,
  and what ticket 34's "no basis" answer quotes.

* **The parent chunk is what gets quoted and the child is what was matched.** So the
  hit carries both, and `parent_chunk_id is None` is a real case rather than a
  defect: a chunk the structural split never gave a parent to is its own context.
  `quote` and `page_from`/`page_to` resolve that in one place so no caller has to.

* **`context_scope` names which of the two a result is quoting.** "The parent is the
  context" is true of most rows and false of the ones above, and a client that
  assumed it would show an empty quote.

* **`document` is a projection, not the `Document` value object.** A citation needs
  the id, the title and the file name, and nothing here needs the hash, the storage
  key or the chunk count. A full `Document` would drag the ingestion module's shape
  into the retrieval surface and make every citation test build one.

* **`filtered` is a fact on the outcome, not an assumption at the call site.** A
  search that ran with no `FilterSpec` is *unfiltered*, and the outcome says so out
  loud. Ticket 35 pushes a real spec in; until then a silent unfiltered search is
  the exact failure that ticket exists to prevent, so it must be visible in the
  answer rather than inferable from a signature nobody reads.
"""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

#: The reciprocal-rank constant the literature uses (Cormack et al., 2009). `k`
#: damps the influence of the very top ranks so that one leg cannot dominate on a
#: single lucky first place; a deployment can lower it to trust the first places
#: more, which is what `retrieval_fusion_k` is for.
DEFAULT_FUSION_K = 60

#: How many candidates each leg contributes before fusion. The design's §5.2 says
#: twenty for each half, and the number is a *recall* budget rather than a result
#: size: the top five come out of the fusion, and a document the fusion never saw
#: cannot be reranked into it.
DEFAULT_LEG_LIMIT = 20

#: How many results a search returns by default (`fusion → rerank → top 5`).
DEFAULT_LIMIT = 5

#: The ceiling on what a caller may ask for. A retrieval endpoint is on the request
#: path of a chat answer; fifty parents of ~1500 tokens is already more context than
#: a model window wants, and an unbounded `limit` is a denial-of-service a query
#: parameter should not offer.
MAX_LIMIT = 50

#: The fusion score's own ceiling: both legs at rank 1 is the only way to reach it.
#: Written as an expression rather than a literal so it cannot drift from the
#: arithmetic it bounds — the reranker normalises against it.
MAX_FUSION_SCORE = 2.0 / (DEFAULT_FUSION_K + 1)


class RetrievalLeg(StrEnum):
    """Which half of the hybrid found a candidate.

    Three values rather than two, because "both legs found it" is the observation
    the whole ticket is about: a candidate only the text leg caught is exactly the
    policy-code-and-acronym case §5.2 names ("纯向量检索对制度编号、缩写、专有名词不敏感"),
    and the debug view has to show it without the reader intersecting two lists.
    """

    VECTOR = "vector"
    TEXT = "text"
    BOTH = "both"


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    """One row as a single leg ranked it, before fusion.

    `rank` is 1-based. `vector_distance` is the cosine *distance* pgvector's `<=>`
    returns (lower is better, 0 is identical) and is `None` for a row the vector leg
    did not return; `text_rank` is `ts_rank_cd` (higher is better) and is `None` for
    the text leg. The two are not comparable, which is the reason fusion works on
    ranks and not on these.
    """

    chunk_id: UUID
    document_id: UUID
    document_title: str
    filename: str
    content: str
    parent_chunk_id: UUID | None
    parent_content: str | None
    heading_path: str | None
    page_from: int | None
    page_to: int | None
    rank: int
    vector_distance: float | None
    text_rank: float | None
    #: Whether the document is the company's knowledge base rather than somebody's own
    #: upload (ticket 34's addition, read from `documents.is_company_kb`). The answer
    #: path needs it: §5.2/Q29 requires a citation from a personal document to carry the
    #: 「以下内容来自个人文档（非公司知识库）」 banner, and the flag is the only thing that
    #: decides it. Defaulted so the fusion's own unit tests, which build a candidate from
    #: a handful of fields and have no document, keep saying only what they mean.
    is_company_kb: bool = False

    @property
    def quote(self) -> str:
        """What a citation quotes: the parent's context, or this chunk's own text.

        A child with no parent row is its own context — the structural split gives a
        parent to the children of a section, and a document whose blocks never
        produced one is retrieved and quoted as itself. Returning the child instead
        of an empty string is what keeps a citation non-empty in that case.
        """
        return self.parent_content if self.parent_content is not None else self.content

    @property
    def context_scope(self) -> str:
        """`"parent"` or `"child"` — which of the two `quote` came from.

        Named so a client can say "the enclosing section" rather than "the passage",
        and so a test can assert the design's "父块内容 + 子块定位" without reading
        two nullable columns.
        """
        return "parent" if self.parent_content is not None else "child"


@dataclass(frozen=True, slots=True)
class RrfLeg:
    """One leg's contribution to a fused candidate's score.

    `contribution` is `1 / (k + rank)` — carried rather than recomputed so the
    arithmetic is assertable by value and the debug view can show the sum of exactly
    the terms that produced `FusedCandidate.fusion_score`.
    """

    leg: RetrievalLeg
    rank: int
    contribution: float


@dataclass(frozen=True, slots=True)
class FusedCandidate:
    """One candidate after reciprocal-rank fusion, with where it came from.

    `legs` holds one entry per leg that returned the candidate, in the canonical
    order (`VECTOR`, then `TEXT`), and `legs[0].contribution + legs[1].contribution`
    *is* `fusion_score` — asserted by the unit test that proves the arithmetic.
    """

    candidate: RankedCandidate
    fusion_score: float
    legs: tuple[RrfLeg, ...]

    @property
    def from_legs(self) -> RetrievalLeg:
        """One value saying which halves found it, for a compact view."""
        if len(self.legs) > 1:
            return RetrievalLeg.BOTH
        return self.legs[0].leg

    def rank_in(self, leg: RetrievalLeg) -> int | None:
        """This candidate's rank in one leg, or `None` when that leg missed it."""
        for entry in self.legs:
            if entry.leg is leg:
                return entry.rank
        return None

    def contribution_of(self, leg: RetrievalLeg) -> float:
        for entry in self.legs:
            if entry.leg is leg:
                return entry.contribution
        return 0.0


@dataclass(frozen=True, slots=True)
class RerankBreakdown:
    """Why the reranker scored a candidate the way it did.

    Every component is bounded in `[0, 1]` so that the weighted sum is too, and
    `prior` is the only one that is not lexical — which is what makes "the reranker
    moved this down despite the fusion" a fact the view can state rather than a
    number somebody has to interpret.
    """

    prior: float
    term_coverage: float
    proximity: float
    heading_match: float
    score: float


@dataclass(frozen=True, slots=True)
class RankedHit:
    """A fused candidate the reranker scored, in reranked order."""

    fused: FusedCandidate
    breakdown: RerankBreakdown

    @property
    def candidate(self) -> RankedCandidate:
        return self.fused.candidate

    @property
    def score(self) -> float:
        return self.breakdown.score


@dataclass(frozen=True, slots=True)
class DocumentRef:
    """The document a hit cites: enough for "file name + page", and no more.

    `is_company_kb` is the one addition ticket 34 made, and it is a fact about the
    document rather than about the ranking: the answer path needs it to mark a citation
    that came from somebody's personal upload (Q29). It defaults to the company's,
    because that is what every caller that builds a `DocumentRef` by hand is modelling.
    """

    id: UUID
    title: str
    filename: str
    is_company_kb: bool = True


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One of the results a search answers with.

    Built from a `RankedHit`, which is why every score is carried: the client shows
    a citation, and the *operator* reading a log — or ticket 34 deciding whether to
    answer at all — needs the three numbers that produced the order.
    """

    document: DocumentRef
    chunk_id: UUID
    page_from: int | None
    page_to: int | None
    heading_path: str | None
    context_scope: str
    content: str
    parent_content: str | None
    quote: str
    vector_distance: float | None
    text_rank: float | None
    vector_rank: int | None
    text_rank_position: int | None
    fusion_score: float
    rerank_score: float


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    """What one search found, and what it is sure about.

    **`insufficient_evidence` is a first-class state and not an error.** §5.2's
    threshold rule is "最高分 < threshold → 直接返回知识库中未找到依据，不调用生成模型",
    so the "no basis" answer is a *successful* search whose best score is too low;
    a 404 or a 500 would make every caller of this module catch something in order to
    render the ordinary honest answer. `hits` is empty exactly when this is true,
    which is what stops "a weak top five" from being returned at all.

    The scores travel with the state so the answer can quote them (ticket 34's
    refusal names what it found) and so a test can pin the boundary from both sides.
    """

    query: str
    hits: tuple[SearchHit, ...]
    vector_candidates: tuple[RankedCandidate, ...]
    text_candidates: tuple[RankedCandidate, ...]
    fused: tuple[FusedCandidate, ...]
    reranked: tuple[RankedHit, ...]
    threshold: float
    best_score: float
    insufficient_evidence: bool
    #: Whether a `FilterSpec` was applied. False means this search saw every ready
    #: document in the corpus, which is a fact the caller is entitled to know.
    filtered: bool
    #: The legs that could run at all. An embedding provider that is switched off
    #: leaves `("text",)`, which is the honest description of that search rather
    #: than a hybrid that silently lost half its recall.
    legs_used: tuple[RetrievalLeg, ...]
    embedder: str | None
    fusion_k: int


@dataclass(frozen=True, slots=True)
class CandidateTrace:
    """One candidate's whole journey, for the debug view.

    `outcome` is one of `kept`, `below_top_n`, `below_threshold`, `outranked` — see
    `retrieval.service.explain`. It is a string rather than an enum because it is a
    *sentence about a run* (which of several reasons applied) and not a domain fact
    anything branches on.
    """

    candidate: RankedCandidate
    vector_rank: int | None
    text_rank_position: int | None
    fusion_score: float
    fusion_rank: int
    rerank_score: float | None
    rerank_rank: int | None
    outcome: str
    reason: str


@dataclass(frozen=True, slots=True)
class RetrievalTrace:
    """Everything the debug view shows about one search.

    The same `SearchOutcome` the ordinary call produced — not a second search — so
    the trace cannot disagree with what a user's question actually returned. That is
    the whole point of the view: "why was this not retrieved" is answered by the run
    that did not retrieve it.
    """

    outcome: SearchOutcome
    candidates: tuple[CandidateTrace, ...]
    leg_limit: int
    #: The permission predicate this run applied, as the statement the database ran.
    #: `None` means the run was unfiltered. Carried on the trace rather than recomputed
    #: by whoever renders it, because a second rendering is a second thing that can
    #: disagree with the query that produced the answer.
    filter_explanation: str | None = None

    @property
    def kept(self) -> tuple[CandidateTrace, ...]:
        return tuple(row for row in self.candidates if row.outcome == "kept")

    @property
    def dropped(self) -> tuple[CandidateTrace, ...]:
        return tuple(row for row in self.candidates if row.outcome != "kept")


__all__ = [
    "DEFAULT_FUSION_K",
    "DEFAULT_LEG_LIMIT",
    "DEFAULT_LIMIT",
    "MAX_FUSION_SCORE",
    "MAX_LIMIT",
    "CandidateTrace",
    "DocumentRef",
    "FusedCandidate",
    "RankedCandidate",
    "RankedHit",
    "RerankBreakdown",
    "RetrievalLeg",
    "RetrievalTrace",
    "RrfLeg",
    "SearchHit",
    "SearchOutcome",
]
