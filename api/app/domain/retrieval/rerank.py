"""The rerank stage: a sealed seam with a lexical implementation behind it.

`docs/DESIGN.md` §5.2 puts a rerank between the fusion and the top five ("RRF 融合 →
rerank → top 5"). What the design means by rerank is a cross-encoder — a model that
reads the query and one passage *together* and scores their pair, which is strictly
more than either leg can do alone, because a bi-encoder never sees the two at once.

**That model is not available offline, and pretending otherwise would be worse than
not having it.** So this module ships `LexicalReranker`, which is honest about what it
is: a re-scoring over surface features of the query and the passage, with the fusion
score as a prior. It is a *seam* — `Reranker` is a Protocol and the service takes one —
so a deployment that has a cross-encoder endpoint replaces the adapter and nothing else
changes. `tests/test_retrieval.py` asserts the seam is used, because a reranker that is
constructed and never called is the failure mode of every "future extension point" in a
codebase.

**What `LexicalReranker` can do.**

* **Prefer the passage that uses the query's own words.** `term_coverage` is the
  fraction of the query's distinct terms present in the passage. It is the same signal
  the text leg has, applied to the *parent* — so a chunk whose parent section is about
  the question ranks above a chunk that merely sat near a match, which is a thing
  neither the vector leg (it embedded the child) nor the text leg (it matched the child)
  can see.
* **Prefer the passage where those words are close together.** `proximity` is the mean
  `1 / (1 + gap)` over adjacent query terms that both occur. This is the one genuinely
  new signal here: "vacaciones" in the heading and "días" three paragraphs down is not
  the same passage as the sentence that contains both.
* **Prefer the passage whose heading names the query.** `heading_match` catches the
  structural case — a section titled `## 3. Vacaciones anuales` answering a question
  about annual leave — which is a strong signal in a policy manual and one that raw term
  frequency dilutes.
* **Keep the fusion order when nothing lexical distinguishes the candidates.** `prior`
  is the normalised fusion score, so a reranker that finds no term overlap at all still
  returns the fusion's order rather than an arbitrary one. That is what makes this stage
  a *rerank* and not a second retrieval.

**What it cannot do, and the two consequences that matter.**

* **It has no notion of meaning.** "¿Cuántos días libres tengo?" and a passage about
  "vacaciones anuales" share no term, so a paraphrase is invisible to it. Where the
  embedding model is real, the vector leg supplies that recall and this reranker cannot
  undo it — but it also cannot *reward* it: a semantically right passage that shares no
  surface form with the question gets none of the lexical bonus and is ranked only by
  the prior. A real cross-encoder is the fix, and that is the seam.
* **It is asymmetric across languages.** Spanish stemmed by the text leg is not the same
  as the query's surface forms here, so `solicitud` vs `solicitar` counts as a miss at
  this stage even though the GIN index matched them. Deliberately not fixed by stemming
  here: a second stemmer in Python would be the second place the corpus's language is
  configured (§10.3 keeps that in one literal in the migration), and the recall it would
  add is the text leg's job, which has already done it.

The score is a weighted sum of four components each bounded in `[0, 1]`, so the result
is in `[0, 1]` and the threshold is a number a person can reason about — which is why
the weights are named constants here rather than literals in the expression.
"""

from collections.abc import Sequence
from typing import Protocol

from app.domain.document.embeddings import fold_accents
from app.domain.retrieval.models import (
    MAX_FUSION_SCORE,
    FusedCandidate,
    RankedHit,
    RerankBreakdown,
)

#: How much of the final score the fusion's own order is worth. Below the three lexical
#: parts combined, so the reranker can actually move candidates; and large enough that a
#: passage sharing no term with the question still ranks by the retrieval's opinion of
#: it. The four weights sum to 1.
PRIOR_WEIGHT = 0.35
COVERAGE_WEIGHT = 0.30
PROXIMITY_WEIGHT = 0.20
HEADING_WEIGHT = 0.15

#: Where a token is too short to be a signal. `vs`, `art`, `n` and the interrogatives
#: are the words that make a Spanish question a question, and every one of them is noise
#: in a policy search. Two characters is the cut, which keeps `irpf`, `iva` and `b3`.
MIN_TERM_LENGTH = 3

#: The window inside which two query terms count as adjacent. Ten tokens is about a
#: sentence's worth of Spanish; beyond it the two terms are in different paragraphs and
#: "close together" stops meaning anything.
PROXIMITY_WINDOW = 10


class Reranker(Protocol):
    """The seam. One verb, and a name for the log.

    `name` is not decoration: the same argument `Embedder.name` makes. A search whose
    answer changed because the reranker changed has to be able to say *which* reranker
    produced it, and an answer that does not record that is an answer nobody can compare
    with last week's.
    """

    @property
    def name(self) -> str: ...

    def rerank(
        self, query: str, candidates: Sequence[FusedCandidate]
    ) -> list[RankedHit]: ...


def terms_of(text: str) -> list[str]:
    """The query's terms, accent-folded and de-duplicated, order preserved.

    `document.embeddings.fold_accents` is reused rather than re-implemented: the corpus
    is Spanish and arrives with and without accents from the same organisation, and the
    fake embedder already needed exactly this. Order is preserved because proximity is
    about *where the terms are relative to each other*, and a set would throw that away.
    """
    kept: list[str] = []
    for term in _tokens_of(text):
        if len(term) >= MIN_TERM_LENGTH and term not in kept:
            kept.append(term)
    return kept


def _tokens_of(text: str) -> list[str]:
    """Every token of a passage, folded, unfiltered — positions matter here."""
    found: list[str] = []
    current: list[str] = []
    for char in fold_accents(text).lower():
        if char.isalnum():
            current.append(char)
        elif current:
            found.append("".join(current))
            current = []
    if current:
        found.append("".join(current))
    return found


def _coverage_of(terms: Sequence[str], present: set[str]) -> float:
    if not terms:
        return 0.0
    return sum(1 for term in terms if term in present) / len(terms)


def _proximity_of(terms: Sequence[str], tokens: Sequence[str]) -> float:
    """How close the query's terms come to each other in the passage.

    Each adjacent pair of query terms that both occur contributes `1 / (1 + gap)` where
    `gap` is the smallest number of tokens between occurrences of the two. A pair where
    only one term occurs contributes nothing, so a passage that hits three isolated query
    words scores below one that contains a single tight pair — which is the trade this
    component exists to make.
    """
    if len(terms) < 2 or not tokens:
        return 0.0
    positions: dict[str, list[int]] = {}
    for index, token in enumerate(tokens):
        if token in terms:
            positions.setdefault(token, []).append(index)
    if len(positions) < 2:
        return 0.0

    pairs: list[float] = []
    for first, second in zip(terms, terms[1:], strict=False):
        left = positions.get(first)
        right = positions.get(second)
        if not left or not right:
            continue
        gap = min(abs(a - b) for a in left for b in right) - 1
        if gap <= PROXIMITY_WINDOW:
            pairs.append(1.0 / (1.0 + max(0, gap)))
    return sum(pairs) / len(pairs) if pairs else 0.0


def _heading_of(terms: Sequence[str], heading_path: str | None) -> float:
    if not heading_path or not terms:
        return 0.0
    return _coverage_of(terms, set(_tokens_of(heading_path)))


def score_of(
    terms: Sequence[str],
    fused: FusedCandidate,
    *,
    prior_weight: float = PRIOR_WEIGHT,
    coverage_weight: float = COVERAGE_WEIGHT,
    proximity_weight: float = PROXIMITY_WEIGHT,
    heading_weight: float = HEADING_WEIGHT,
) -> RerankBreakdown:
    """One candidate's breakdown, from the query's terms and the fused candidate.

    A module-level function rather than a method, and public, because it is what a
    *second* adapter needs: a reranker that replaces one component — or a test that wants
    to record what the pipeline saw — should be able to reuse the arithmetic instead of
    copying it. `LexicalReranker.rerank` is a thin loop over this.
    """
    tokens = _tokens_of(fused.candidate.quote)
    prior = min(1.0, fused.fusion_score / MAX_FUSION_SCORE) if MAX_FUSION_SCORE else 0.0
    coverage = _coverage_of(terms, set(tokens))
    proximity = _proximity_of(terms, tokens)
    heading = _heading_of(terms, fused.candidate.heading_path)
    return RerankBreakdown(
        prior=prior,
        term_coverage=coverage,
        proximity=proximity,
        heading_match=heading,
        score=(
            prior_weight * prior
            + coverage_weight * coverage
            + proximity_weight * proximity
            + heading_weight * heading
        ),
    )


class LexicalReranker:
    """Surface-feature re-scoring with the fusion score as a prior. See the module.

    Stateless and pure, which is what makes it unit-testable without a database and what
    keeps a rerank from becoming a second place that remembers something. The arithmetic
    lives in `score_of`, so a second adapter can reuse it.
    """

    name = "lexical-structural-v1"

    def __init__(
        self,
        *,
        prior_weight: float = PRIOR_WEIGHT,
        coverage_weight: float = COVERAGE_WEIGHT,
        proximity_weight: float = PROXIMITY_WEIGHT,
        heading_weight: float = HEADING_WEIGHT,
    ) -> None:
        self._prior = prior_weight
        self._coverage = coverage_weight
        self._proximity = proximity_weight
        self._heading = heading_weight

    def rerank(
        self, query: str, candidates: Sequence[FusedCandidate]
    ) -> list[RankedHit]:
        """Score every candidate and order them, best first.

        The sort is total — ties fall back to `(document_id, chunk_id)` — for the reason
        the fusion's is: two candidates that are lexically indistinguishable must not
        come back in dictionary order, or the top five changes between two runs of one
        question.
        """
        terms = terms_of(query)
        ranked = [
            RankedHit(
                fused=item,
                breakdown=score_of(
                    terms,
                    item,
                    prior_weight=self._prior,
                    coverage_weight=self._coverage,
                    proximity_weight=self._proximity,
                    heading_weight=self._heading,
                ),
            )
            for item in candidates
        ]
        ranked.sort(
            key=lambda hit: (
                -hit.score,
                str(hit.candidate.document_id),
                str(hit.candidate.chunk_id),
            )
        )
        return ranked


def build_reranker(name: str | None = None) -> Reranker:
    """The adapter a deployment asked for.

    One value today; a function rather than a bare class because the *shape* of the
    choice is the point of the seam — a deployment that sets `RETRIEVAL_RERANKER` to a
    cross-encoder's name gets that adapter without a code change anywhere the reranker
    is used.
    """
    if name in (None, "", "lexical", LexicalReranker.name):
        return LexicalReranker()
    raise ValueError(
        f"unknown reranker {name!r}; this build ships {LexicalReranker.name!r}. A "
        "cross-encoder adapter belongs in this module beside it."
    )


__all__ = [
    "COVERAGE_WEIGHT",
    "HEADING_WEIGHT",
    "MIN_TERM_LENGTH",
    "PRIOR_WEIGHT",
    "PROXIMITY_WINDOW",
    "PROXIMITY_WEIGHT",
    "LexicalReranker",
    "Reranker",
    "build_reranker",
    "score_of",
    "terms_of",
]
