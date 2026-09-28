"""Reciprocal-rank fusion, as a pure function over two ranked lists.

**Why fusion is a function and not a query.** The fusion is the one piece of this
ticket whose correctness is arithmetic rather than SQL: `1 / (k + rank)` summed over
the legs that found a candidate, sorted descending. Written inside the search statement
it could only be asserted by building a corpus, reading the answer and believing it;
written here it is asserted by handing it two lists and comparing the numbers. That is
the test that proves the ranking rule, and it needs no database.

**Why RRF at all, and not a weighted sum of the scores.** The two legs' scores are not
commensurable — a cosine distance in `[0, 2]` and a `ts_rank_cd` in roughly `[0, 1]`
with no shared scale — so any weighted sum needs a normalisation that is itself a tuning
decision nobody can defend. Ranks are already commensurable: "first in its own leg"
means the same thing on both sides. RRF also needs no training data and is stable when
one leg returns nothing, which is the ordinary case for a query whose words share no
stem with the corpus.

**Why the inputs are keyed by chunk id.** The first draft of this signature took two
sequences and read each candidate's `rank` field. That is one refactor away from a
fusion that silently reads the *wrong leg's* rank — pass the lists in the other order and
every score is plausible and wrong — which the unit test caught the moment it was written
against the wrong parameter order. Keyed by chunk id, "which leg is this rank from" is
the dictionary's name and not the argument's position, so that mistake is not
expressible.

**Why `k` is a parameter.** `k` damps the top ranks: with `k = 60` the first place is
worth `1/61` and the twentieth `1/80`, so a candidate both legs put in the top five beats
one leg's first place. Lowering it (10 is the usual alternative) makes a single leg's top
hit dominate, which is the right choice for a corpus where one leg is known to be much
the better; `retrieval_fusion_k` is the setting, and 60 is the literature's value.
"""

from collections.abc import Mapping, Sequence
from uuid import UUID

from app.domain.retrieval.models import (
    DEFAULT_FUSION_K,
    FusedCandidate,
    RankedCandidate,
    RetrievalLeg,
    RrfLeg,
)


def by_chunk(candidates: Sequence[RankedCandidate]) -> dict[UUID, RankedCandidate]:
    """A leg's ranked list, keyed by chunk id — what `reciprocal_rank_fusion` takes.

    A helper rather than a requirement on callers, because building this dictionary is
    the one mechanical step between "the repository returned two ordered lists" and "the
    fusion wants two mappings", and a caller that wrote it by hand twice would be a
    caller who could write it differently once.
    """
    return {candidate.chunk_id: candidate for candidate in candidates}


def reciprocal_rank_fusion(
    vector: Mapping[UUID, RankedCandidate],
    text: Mapping[UUID, RankedCandidate],
    *,
    k: int = DEFAULT_FUSION_K,
) -> list[FusedCandidate]:
    """Merge two ranked legs into one candidate list, best fusion score first.

    Each mapping is keyed by chunk id and each value carries its own leg's 1-based
    `rank`. This function does not re-derive a rank from a position, because a leg that
    returned rows out of order, with gaps, or starting at something other than 1 would
    then be fused as if it had not: the ranks a leg states are the ranks that are fused.

    **Both mappings are consulted, and neither may be empty.** `reciprocal_rank_fusion(
    {}, text)` and the reverse each return the surviving leg's candidates, because that
    is what a deployment with no embedding key does on every query. A version that took
    "the first non-empty mapping" would be right by accident and would fuse nothing the
    moment both legs answered — which is the case the ticket is about.

    Ties are broken by `(document_id, chunk_id)` so the order is total and stable: two
    candidates with the same fusion score — the common case when both legs agree on the
    same two rows — must not come back in whichever order the dictionary happened to
    iterate, or the top five is not reproducible between two runs of one query.
    """
    if k < 0:
        raise ValueError(f"the fusion constant must not be negative: {k}")

    fused: list[FusedCandidate] = []
    for chunk_id in set(vector) | set(text):
        # The candidate object carried forward is the vector leg's when both legs found
        # the row — they describe the same chunk and differ only in which raw score is
        # set, and the vector leg's is the one the HNSW index produced.
        candidate = vector.get(chunk_id) or text[chunk_id]

        legs: list[RrfLeg] = []
        for leg, source in ((RetrievalLeg.VECTOR, vector), (RetrievalLeg.TEXT, text)):
            found = source.get(chunk_id)
            if found is None:
                continue
            legs.append(RrfLeg(leg=leg, rank=found.rank, contribution=1.0 / (k + found.rank)))

        fused.append(
            FusedCandidate(
                candidate=candidate,
                fusion_score=sum(entry.contribution for entry in legs),
                legs=tuple(legs),
            )
        )

    fused.sort(
        key=lambda item: (
            -item.fusion_score,
            str(item.candidate.document_id),
            str(item.candidate.chunk_id),
        )
    )
    return fused


def fusion_rank_of(fused: Sequence[FusedCandidate], chunk_id: UUID) -> int | None:
    """Where a fused candidate landed, 1-based, or `None` when it is not there.

    The debug view needs this to say "fused at 7, which is not in the top five" — which
    is a different sentence from "the reranker moved it out", and those are the two
    reasons a retrieved candidate does not reach a user.
    """
    for position, item in enumerate(fused, start=1):
        if item.candidate.chunk_id == chunk_id:
            return position
    return None


__all__ = ["by_chunk", "fusion_rank_of", "reciprocal_rank_fusion"]
